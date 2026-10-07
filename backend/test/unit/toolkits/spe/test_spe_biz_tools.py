"""biz 读工具层测试：快照对账、注册、管线与各 kind 构建器（假后端零出站）。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.tools import ToolException

from yuxi.agents.toolkits.registry import get_all_tool_instances, get_extra_metadata
from yuxi.agents.toolkits.spe import biz
from yuxi.agents.toolkits.spe.catalog import SPE_BIZ_READ_TOOLS
from yuxi.agents.toolkits.spe.client import SpeToolError
from yuxi.agents.toolkits.spe import resolvers as spe_resolvers

_SNAPSHOT = Path(__file__).resolve().parent / "fixtures" / "spe_contract_snapshot.json"
_INTERNALIZED = {"kb_list", "kb_read", "yuxi_kb_search"}  # 批次 2C 内化，不进本目录

BASE = "http://spe.test"
STORES_PATH = "/api/v1/integration/ozon/stores"
AUTH_ME_PATH = "/api/v1/auth/me"


def _runtime(uid: str = "u1"):
    return SimpleNamespace(context=SimpleNamespace(uid=uid))


# ---------- 对账：catalog 与冻结快照 ----------


def test_catalog_reconciles_with_snapshot() -> None:
    """16 工具的 name/description/参数面/卡面逐字等于快照；biz-get 19 恰好.partition。"""
    snapshot = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    biz_reads = [t for t in snapshot["tools"] if t["channel"] == "biz-get"]
    ported = [t for t in biz_reads if t["name"] not in _INTERNALIZED]
    assert len(biz_reads) == 19 and len(ported) == len(SPE_BIZ_READ_TOOLS) == 16

    by_name = {t["name"]: t for t in ported}
    assert {s.name for s in SPE_BIZ_READ_TOOLS} == set(by_name)
    for spec in SPE_BIZ_READ_TOOLS:
        snap = by_name[spec.name]
        assert spec.description == snap["description"]
        assert biz.BIZ_CARD_TYPES[spec.name] == snap["card_type"]
        assert [(p["name"], p["required"]) for p in spec.params] == [
            (p["name"], bool(p.get("required"))) for p in snap["params"]
        ]


def test_all_biz_tools_registered() -> None:
    """16 工具全部进入全局注册表，category=spe，名字唯一。"""
    instances = {t.name for t in get_all_tool_instances()}
    assert set(biz.SPE_BIZ_READ_TOOL_NAMES) <= instances
    assert len(biz.SPE_BIZ_READ_TOOL_NAMES) == len(set(biz.SPE_BIZ_READ_TOOL_NAMES)) == 16
    for name in biz.SPE_BIZ_READ_TOOL_NAMES:
        assert get_extra_metadata(name).category == "spe"


# ---------- 管线（假后端按路径路由，biz 与 resolvers 两处出站同桩） ----------


class _FakeOpts:
    def __init__(self, base_url: str | None = BASE) -> None:
        self.base_url = base_url

    async def get(self) -> dict:
        return {"base_url": self.base_url, "service_token": "svc"}


class _FakeRepo:
    def __init__(self, jwt: str | None = "jwt-u1") -> None:
        self.jwt = jwt

    async def get_user_jwt(self, uid: str) -> str | None:
        return self.jwt


@pytest.fixture
def backend(monkeypatch):
    """按路径路由的假后端：记录出站，返回预置 payload 或抛预置错误。"""
    calls: list[tuple[str, dict]] = []
    routes: dict[str, object] = {}

    async def fake_biz_get(*, base_url, jwt, path, query, timeout=120.0):
        calls.append((path, dict(query)))
        handler = routes.get(path, {"ok": True, "default": True})
        if isinstance(handler, Exception):
            raise handler
        return handler

    monkeypatch.setattr(biz, "call_spe_biz_get", fake_biz_get)
    monkeypatch.setattr(spe_resolvers, "call_spe_biz_get", fake_biz_get)
    monkeypatch.setattr(biz, "spe_agent_opts", _FakeOpts())
    monkeypatch.setattr(biz, "spe_credential_repository", _FakeRepo())
    return SimpleNamespace(calls=calls, routes=routes)


async def test_not_logged_in_fails_closed(backend, monkeypatch) -> None:
    monkeypatch.setattr(biz, "spe_credential_repository", _FakeRepo(jwt=None))
    with pytest.raises(ToolException) as info:
        await biz.list_orders.coroutine(runtime=_runtime())
    assert "BIZ_AUTH_FAILED" in str(info.value)
    assert "login" in str(info.value)  # guidance 引导重新登录
    assert backend.calls == []  # 零出站


async def test_missing_uid_reports_error(backend) -> None:
    result = await biz.list_orders.coroutine(runtime=_runtime(""))
    assert result == {"ok": False, "error": "无法获取当前用户身份"}
    assert backend.calls == []


async def test_missing_base_url_fails_closed(backend, monkeypatch) -> None:
    monkeypatch.setattr(biz, "spe_agent_opts", _FakeOpts(base_url=None))
    with pytest.raises(ToolException) as info:
        await biz.list_orders.coroutine(runtime=_runtime())
    assert "SPE_NOT_CONFIGURED" in str(info.value)
    assert backend.calls == []


async def test_list_orders_query_assembly(backend) -> None:
    backend.routes[STORES_PATH] = [
        {"id": 101, "storeName": "主店", "isActive": True},
        {"id": 102, "storeName": "副店", "isActive": False},
    ]
    backend.routes["/api/v1/orders"] = {"total": 0, "list": []}
    result = await biz.list_orders.coroutine(
        runtime=_runtime(),
        status="delivered",
        keyword="к" * 150,
        store="副店",
        page=-3,
        size=500,
    )
    orders_call = next(c for c in backend.calls if c[0] == "/api/v1/orders")
    assert orders_call[1] == {
        "page": 0,  # 负数回落 0
        "size": 100,  # 越界夹紧
        "status": "delivered",
        "keyword": "к" * 100,  # 截断 100
        "storeId": 102,  # 店铺名全等匹配
    }
    assert result["ui"] == "panel" and result["v"] == 1 and result["data"]["total"] == 0
    assert result["blockId"].startswith("blk_")


async def test_store_ref_miss_lists_candidates(backend) -> None:
    backend.routes[STORES_PATH] = [{"id": 1, "storeName": "主店", "isActive": True}]
    with pytest.raises(ToolException) as info:
        await biz.dashboard_stats.coroutine(runtime=_runtime(), store="不存在店")
    assert "BIZ_STORE_NOT_FOUND" in str(info.value)
    assert "主店" in str(info.value)


async def test_monthly_report_validates_and_defaults_store(backend) -> None:
    backend.routes[STORES_PATH] = [
        {"id": 7, "storeName": "主店", "isActive": False},
        {"id": 8, "storeName": "启用店", "isActive": True},
    ]
    backend.routes["/api/v1/enhanced-reports/monthly"] = {"report": True}
    result = await biz.monthly_report.coroutine(runtime=_runtime(), year=2026, month=10)
    call = next(c for c in backend.calls if "monthly" in c[0])
    assert call[1] == {"storeId": 8, "year": 2026, "month": 10}  # 缺省=首个启用店铺
    assert result["ui"] == "score-report"  # 快照逐字卡面


async def test_monthly_report_rejects_bad_month(backend) -> None:
    with pytest.raises(ToolException) as info:
        await biz.monthly_report.coroutine(runtime=_runtime(), year=2026, month=13)
    assert "INVALID_ARGS" in str(info.value)
    assert backend.calls == []


async def test_no_active_store_maps_biz_no_store(backend) -> None:
    backend.routes[STORES_PATH] = [{"id": 7, "storeName": "停用店", "isActive": False}]
    with pytest.raises(ToolException) as info:
        await biz.monthly_report.coroutine(runtime=_runtime(), year=2026, month=10)
    assert "BIZ_NO_STORE" in str(info.value)


async def test_tenant_scoped_injects_tenant_id(backend) -> None:
    backend.routes[AUTH_ME_PATH] = {"tenantId": 5001, "username": "alice"}
    backend.routes["/api/v1/products"] = []
    await biz.list_products.coroutine(runtime=_runtime())
    products_call = next(c for c in backend.calls if c[0] == "/api/v1/products")
    assert products_call[1] == {"tenantId": 5001}


async def test_tenant_missing_maps_auth_failed(backend) -> None:
    backend.routes[AUTH_ME_PATH] = {"username": "无租户"}
    with pytest.raises(ToolException) as info:
        await biz.low_stock.coroutine(runtime=_runtime())
    assert "BIZ_AUTH_FAILED" in str(info.value)


async def test_ops_plans_status_falls_back_active(backend) -> None:
    backend.routes["/api/v1/opsplan/plans"] = {"list": []}
    await biz.ops_plans.coroutine(runtime=_runtime(), status="GARBAGE", page=0, size=20)
    call = next(c for c in backend.calls if "opsplan" in c[0])
    assert call[1] == {"status": "ACTIVE", "page": 0, "size": 20}


async def test_source_draft_validation_and_404_remap(backend) -> None:
    with pytest.raises(ToolException) as info:
        await biz.get_source_draft.coroutine(runtime=_runtime(), sourceProductId=0)
    assert "INVALID_ARGS" in str(info.value)

    backend.routes["/api/v1/source-products/9/draft"] = SpeToolError("BIZ_NOT_FOUND", "业务接口未找到（HTTP 404）")
    with pytest.raises(ToolException) as info:
        await biz.get_source_draft.coroutine(runtime=_runtime(), sourceProductId=9)
    assert "DRAFT_NOT_FOUND" in str(info.value)  # 换码为更可执行的指引


async def test_collection_status_two_calls(backend) -> None:
    backend.routes["/api/v1/products/collected/stats"] = {"total": 12}
    backend.routes["/api/v1/products/collected"] = {"list": []}
    result = await biz.collection_status.coroutine(runtime=_runtime())
    assert result["data"] == {"stats": {"total": 12}, "recent": {"list": []}}
    assert [c[0] for c in backend.calls] == [
        "/api/v1/products/collected/stats",
        "/api/v1/products/collected",
    ]
    assert backend.calls[1][1] == {"page": 0, "size": 5}


async def test_plain_tool_no_query(backend) -> None:
    backend.routes["/api/v1/inventory/management/health-report"] = {"score": 90}
    result = await biz.inventory_health.coroutine(runtime=_runtime())
    assert backend.calls == [("/api/v1/inventory/management/health-report", {})]
    assert result["data"] == {"score": 90}
