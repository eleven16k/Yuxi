"""写通道测试：对账、回执（C-26）、写工具流与审批扩展。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.tools import ToolException

from yuxi.agents.tool_approval import (
    SENSITIVE_BACKEND_TOOLS,
    create_tool_approval_middleware,
)
from yuxi.agents.toolkits.registry import get_all_tool_instances
from yuxi.agents.toolkits.spe import write_tools
from yuxi.agents.toolkits.spe.catalog import SPE_BIZ_WRITE_TOOLS
from yuxi.agents.toolkits.spe.contract_names import SPE_WRITE_GATE_TOOLS
from yuxi.agents.toolkits.spe.receipts import (
    DUPLICATE_NOTE,
    WriteReceipts,
    operation_key,
)

_SNAPSHOT = Path(__file__).resolve().parent / "fixtures" / "spe_contract_snapshot.json"


def _runtime(uid: str = "u1"):
    return SimpleNamespace(context=SimpleNamespace(uid=uid))


class _FakeOpts:
    async def get(self) -> dict:
        return {"base_url": "http://spe.test", "service_token": "svc"}


class _FakeRepo:
    def __init__(self, jwt: str | None = "jwt-u1") -> None:
        self.jwt = jwt

    async def get_user_jwt(self, uid: str) -> str | None:
        return self.jwt


# ---------- 对账 ----------


def test_write_specs_reconcile_with_snapshot() -> None:
    snapshot = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    biz_writes = [t for t in snapshot["tools"] if t["channel"] == "biz-post"]
    assert len(biz_writes) == len(SPE_BIZ_WRITE_TOOLS) == 10
    by_name = {t["name"]: t for t in biz_writes}
    for spec in SPE_BIZ_WRITE_TOOLS:
        assert spec.description == by_name[spec.name]["description"]
        assert spec.card == by_name[spec.name]["card_type"]
        assert [(p["name"], p["required"]) for p in spec.params] == [
            (p["name"], bool(p.get("required"))) for p in by_name[spec.name]["params"]
        ]
    assert {t.name for t in get_all_tool_instances()} >= set(write_tools.SPE_WRITE_TOOL_NAMES)


def test_approval_inline_names_match_contract_source() -> None:
    """tool_approval 内联名单与契约单一来源恒等（防两处漂移）。"""
    from yuxi.agents import tool_approval

    assert tool_approval.SENSITIVE_BACKEND_TOOLS >= SPE_WRITE_GATE_TOOLS


def test_gate_names_match_snapshot() -> None:
    """冻结快照闸（14）⊆ 全量闸；差集 = 订单写工具扩展面（P1#06，快照外）。"""
    snapshot = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    gated = {t["name"] for t in snapshot["tools"] if t["write_gate"]}
    from yuxi.agents.toolkits.spe.catalog import SPE_ORDER_WRITE_TOOLS

    extension = {spec.name for spec in SPE_ORDER_WRITE_TOOLS}
    assert gated <= SPE_WRITE_GATE_TOOLS and len(gated) == 14
    assert SPE_WRITE_GATE_TOOLS - gated == extension


# ---------- 回执（C-26） ----------


async def _noop_summary() -> tuple[str, dict]:
    return "已执行", {"k": 1}


async def test_receipt_duplicate_replays_first_execution() -> None:
    clock = {"t": 0.0}
    receipts = WriteReceipts(now=lambda: clock["t"])
    runs = []

    async def run() -> tuple[str, dict]:
        runs.append(1)
        return "已应用动态推荐价（价格记录 #7）", {"priceId": 7}

    first = await receipts.exec_tracked(uid="u1", tool="apply_dynamic_price", args={"priceId": 7}, run=run)
    clock["t"] = 60.0
    second = await receipts.exec_tracked(uid="u1", tool="apply_dynamic_price", args={"priceId": 7}, run=run)

    assert len(runs) == 1  # 未再次执行后端写
    assert second["operationId"] == first["operationId"]  # 同一操作不是新操作
    assert second["duplicate"] is True
    assert second["summary"].endswith(DUPLICATE_NOTE)
    assert first["duplicate"] is False and DUPLICATE_NOTE not in first["summary"]


async def test_receipt_window_expiry_reruns() -> None:
    clock = {"t": 0.0}
    receipts = WriteReceipts(now=lambda: clock["t"])
    runs = []

    async def run() -> tuple[str, dict]:
        runs.append(1)
        return "s", {}

    await receipts.exec_tracked(uid="u1", tool="t", args={}, run=run)
    clock["t"] = 121.0  # 窗口外
    again = await receipts.exec_tracked(uid="u1", tool="t", args={}, run=run)
    assert len(runs) == 2 and again["duplicate"] is False


async def test_receipt_failure_not_cached() -> None:
    receipts = WriteReceipts()
    attempts = []

    async def failing() -> tuple[str, dict]:
        attempts.append(1)
        raise RuntimeError("后端拒绝")

    with pytest.raises(RuntimeError):
        await receipts.exec_tracked(uid="u1", tool="t", args={}, run=failing)
    ok = await receipts.exec_tracked(uid="u1", tool="t", args={}, run=_noop_summary)
    assert len(attempts) == 1 and ok["duplicate"] is False  # 重试真正重跑


async def test_receipt_uid_isolates_windows() -> None:
    receipts = WriteReceipts()
    runs = []

    async def run() -> tuple[str, dict]:
        runs.append(1)
        return "s", {}

    await receipts.exec_tracked(uid="u1", tool="t", args={"a": 1}, run=run)
    other = await receipts.exec_tracked(uid="u2", tool="t", args={"a": 1}, run=run)
    assert len(runs) == 2 and other["duplicate"] is False


def test_operation_key_stable_across_key_order() -> None:
    assert operation_key("u1", "t", {"a": 1, "b": 2}) == operation_key("u1", "t", {"b": 2, "a": 1})
    assert operation_key("u1", "t", {"a": 1}) != operation_key("u2", "t", {"a": 1})


async def test_receipt_inflight_shares_single_run() -> None:
    import asyncio

    receipts = WriteReceipts()
    runs = []

    async def slow() -> tuple[str, dict]:
        runs.append(1)
        await asyncio.sleep(0.02)
        return "s", {}

    results = await asyncio.gather(
        receipts.exec_tracked(uid="u1", tool="t", args={}, run=slow),
        receipts.exec_tracked(uid="u1", tool="t", args={}, run=slow),
    )
    assert len(runs) == 1
    assert results[0]["operationId"] == results[1]["operationId"]


# ---------- 写工具流（假出站 + 真回执） ----------


@pytest.fixture
def backend(monkeypatch):
    calls: list[dict] = []
    routes: dict[str, object] = {}
    receipts = WriteReceipts()

    async def fake_mutate(**kwargs):
        calls.append(kwargs)
        handler = routes.get(kwargs["path"])
        if isinstance(handler, Exception):
            raise handler
        return handler

    monkeypatch.setattr(write_tools, "call_spe_biz_mutate", fake_mutate)
    monkeypatch.setattr(write_tools, "write_receipts", receipts)
    monkeypatch.setattr(write_tools, "spe_agent_opts", _FakeOpts())
    monkeypatch.setattr(write_tools, "spe_credential_repository", _FakeRepo())
    return SimpleNamespace(calls=calls, routes=routes, receipts=receipts)


async def test_apply_dynamic_price_write_and_duplicate(backend) -> None:
    backend.routes["/api/v1/pricing/dynamic/7/apply"] = None
    first = await write_tools.apply_dynamic_price.coroutine(runtime=_runtime(), priceId=7)
    assert first["ui"] == "confirm-result"
    assert first["data"]["summary"] == "已应用动态推荐价（价格记录 #7）"
    assert first["data"]["operationId"].startswith("op_apply_dynamic_price_")

    second = await write_tools.apply_dynamic_price.coroutine(runtime=_runtime(), priceId=7)
    assert len(backend.calls) == 1  # duplicate 未再次出站
    assert second["data"]["duplicate"] is True
    assert second["data"]["summary"].endswith(DUPLICATE_NOTE)
    assert second["data"]["operationId"] == first["data"]["operationId"]


async def test_write_validation_errors(backend) -> None:
    cases = [
        (write_tools.restock_inventory, {"inventoryId": 1, "quantity": 0, "unitCost": 1.0}),
        (write_tools.gift_credits, {"tenantId": 1, "amount": 1, "remark": "x" * 201}),
        (write_tools.generate_ozon_images, {"sourceProductId": 1, "count": 0}),
        (write_tools.generate_listing, {"sourceProductId": 0}),
        (write_tools.preview_dynamic_price, {"productId": "p" * 65}),
        (write_tools.collect_products, {"skus": [""]}),
    ]
    for tool_obj, kwargs in cases:
        with pytest.raises(ToolException) as info:
            await tool_obj.coroutine(runtime=_runtime(), **kwargs)
        assert "INVALID_ARGS" in str(info.value), tool_obj.name
    assert backend.calls == []


async def test_gift_credits_body_and_balance(backend) -> None:
    backend.routes["/api/v1/credit/admin/gift"] = {"balance": 88}
    result = await write_tools.gift_credits.coroutine(runtime=_runtime(), tenantId=5, amount=10)
    call = backend.calls[0]
    assert call["body"] == {"tenantId": 5, "amount": 10}  # remark 缺省不带键
    assert "余额 88" in result["data"]["summary"]
    assert result["data"]["details"]["balance"] == 88


async def test_generate_listing_store_resolution_and_timeout(backend, monkeypatch) -> None:
    resolved: list[str] = []

    async def fake_store_id(*, base_url, jwt):
        resolved.append("default")
        return 42

    monkeypatch.setattr(write_tools, "resolve_store_id", fake_store_id)
    backend.routes["/api/v1/source-products/3/generate-listing"] = {
        "id": 901,
        "titleRu": "Куртка",
        "sourceProductId": 3,
    }
    result = await write_tools.generate_listing.coroutine(runtime=_runtime(), sourceProductId=3)
    call = backend.calls[0]
    assert call["query"] == {"storeId": 42}
    assert call["timeout"] == 300.0  # 同步 AI 加工覆写
    assert "店铺 默认" in result["data"]["summary"] and "Куртка" in result["data"]["summary"]
    assert result["data"]["details"]["draftId"] == 901


async def test_collect_products_preview_not_idempotent(backend) -> None:
    backend.routes["/api/v1/products/collect/sku"] = [{"name": "jacket"}]
    await write_tools.collect_products.coroutine(runtime=_runtime(), skus=["SKU1"])
    await write_tools.collect_products.coroutine(runtime=_runtime(), skus=["SKU1"])
    assert len(backend.calls) == 2  # 预览类不做幂等去重
    envelope = await write_tools.collect_products.coroutine(runtime=_runtime(), skus=["SKU1", "SKU2"])
    assert envelope["ui"] == "panel" and envelope["data"]["totalElements"] == 1


async def test_write_error_maps_to_guidance(backend) -> None:
    from yuxi.agents.toolkits.spe.client import SpeToolError

    backend.routes["/api/v1/inventory/9/restock"] = SpeToolError(
        "BIZ_INSUFFICIENT_CREDIT", "积分不足（HTTP 402）：余额 0"
    )
    with pytest.raises(ToolException) as info:
        await write_tools.restock_inventory.coroutine(runtime=_runtime(), inventoryId=9, quantity=1, unitCost=1.0)
    assert "BIZ_INSUFFICIENT_CREDIT" in str(info.value)
    assert "充值" in str(info.value)  # guidance 随错误附出


# ---------- 审批扩展（T04） ----------


def test_approval_covers_spe_write_gate_tools() -> None:
    middleware = create_tool_approval_middleware("default")
    config = middleware.interrupt_on
    for name in SPE_WRITE_GATE_TOOLS:
        assert name in config
        assert config[name]["allowed_decisions"] == ["approve", "reject"]
        assert "when" not in config[name]  # 任何参数组合都需确认
    # 沙盒工具的原有谓词语义保持不变
    assert "when" in config["write_file"]


def test_approval_always_trust_returns_none() -> None:
    assert create_tool_approval_middleware("always_trust") is None


def test_sensitive_set_hides_write_tools_from_subagents() -> None:
    assert SPE_WRITE_GATE_TOOLS <= SENSITIVE_BACKEND_TOOLS


# ---------- 订单状态机写工具（P1#06，扩展面：冻结快照之外） ----------


def test_order_write_tools_registered_and_gated() -> None:
    """4 个订单写工具已注册、入审批闸、且不在冻结快照内（扩展面边界）。"""
    import json
    from pathlib import Path

    from yuxi.agents.toolkits.spe.contract_names import SPE_WRITE_GATE_TOOLS

    names = ["confirm_order", "ship_order", "cancel_order", "refund_order"]
    for name in names:
        tool_obj = getattr(write_tools, name, None)
        assert tool_obj is not None, f"{name} 未注册"
    for name in names:
        assert name in SPE_WRITE_GATE_TOOLS, f"{name} 不在审批闸"

    snapshot = json.loads(
        (Path(__file__).parent / "fixtures" / "spe_contract_snapshot.json").read_text()
    )
    frozen = {t["name"] for t in snapshot["tools"]}
    for name in names:
        assert name not in frozen, f"{name} 不应出现在冻结快照（扩展面）"


async def test_confirm_order_write_and_duplicate(backend) -> None:
    backend.routes["/api/v1/orders/5/confirm"] = None
    first = await write_tools.confirm_order.coroutine(runtime=_runtime(), orderId=5)
    assert first["ui"] == "confirm-result"
    assert "订单 #5" in first["data"]["summary"] and "确认" in first["data"]["summary"]
    assert first["data"]["operationId"].startswith("op_confirm_order_")

    second = await write_tools.confirm_order.coroutine(runtime=_runtime(), orderId=5)
    assert len(backend.calls) == 1
    assert second["data"]["duplicate"] is True


async def test_ship_order_query_params(backend) -> None:
    backend.routes["/api/v1/orders/7/ship"] = None
    result = await write_tools.ship_order.coroutine(
        runtime=_runtime(), orderId=7, trackingNumber="RF123456789", carrier="CDEK"
    )
    call = backend.calls[0]
    assert call["path"] == "/api/v1/orders/7/ship"
    assert call["query"] == {"trackingNumber": "RF123456789", "carrier": "CDEK"}
    assert "发货" in result["data"]["summary"]


async def test_cancel_and_refund_order(backend) -> None:
    backend.routes["/api/v1/orders/8/cancel"] = None
    backend.routes["/api/v1/orders/9/refund"] = None
    cancel = await write_tools.cancel_order.coroutine(runtime=_runtime(), orderId=8, reason="买家申请")
    refund = await write_tools.refund_order.coroutine(runtime=_runtime(), orderId=9, reason="缺货")
    assert "取消" in cancel["data"]["summary"]
    assert "退款" in refund["data"]["summary"]
    assert cancel["data"]["details"]["reason"] == "买家申请"


async def test_order_write_validation_errors(backend) -> None:
    cases = [
        (write_tools.confirm_order, {"orderId": 0}),
        (write_tools.ship_order, {"orderId": 7, "trackingNumber": "", "carrier": "CDEK"}),
        (write_tools.cancel_order, {"orderId": 8, "reason": ""}),
        (write_tools.refund_order, {"orderId": 9, "reason": "r" * 201}),
    ]
    for tool_obj, kwargs in cases:
        with pytest.raises(ToolException) as info:
            await tool_obj.coroutine(runtime=_runtime(), **kwargs)
        assert "INVALID_ARGS" in str(info.value), tool_obj.name
    assert backend.calls == []
