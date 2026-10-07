"""agent-session 通道测试：快照对账、注册、会话管线与仅重试一次语义。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.tools import ToolException

from yuxi.agents.toolkits.registry import get_all_tool_instances
from yuxi.agents.toolkits.spe import agent_tools
from yuxi.agents.toolkits.spe.catalog import SPE_AGENT_SESSION_TOOLS
from yuxi.agents.toolkits.spe.client import SpeToolError
from yuxi.agents.toolkits.spe.sessions import SpeSessionRegistry

_SNAPSHOT = Path(__file__).resolve().parent / "fixtures" / "spe_contract_snapshot.json"


def _runtime(uid: str = "u1"):
    return SimpleNamespace(context=SimpleNamespace(uid=uid))


class _FakeOpts:
    def __init__(self, base_url: str | None = "http://spe.test", service_token: str | None = "svc") -> None:
        self.base_url = base_url
        self.service_token = service_token

    async def get(self) -> dict:
        return {"base_url": self.base_url, "service_token": self.service_token}


class _FakeRepo:
    def __init__(self, jwt: str | None = "jwt-u1") -> None:
        self.jwt = jwt

    async def get_user_jwt(self, uid: str) -> str | None:
        return self.jwt


def test_catalog_reconciles_with_snapshot() -> None:
    """6 工具 name/description/参数面逐字等于快照 agent-session 通道。"""
    snapshot = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    session_tools = [t for t in snapshot["tools"] if t["channel"] == "agent-session"]
    assert len(session_tools) == len(SPE_AGENT_SESSION_TOOLS) == 6
    by_name = {t["name"]: t for t in session_tools}
    assert {s.name for s in SPE_AGENT_SESSION_TOOLS} == set(by_name)
    for spec in SPE_AGENT_SESSION_TOOLS:
        assert spec.description == by_name[spec.name]["description"]
        assert spec.card == by_name[spec.name]["card_type"]
        assert [(p["name"], p["required"]) for p in spec.params] == [
            (p["name"], bool(p.get("required"))) for p in by_name[spec.name]["params"]
        ]


def test_all_agent_tools_registered() -> None:
    instances = {t.name for t in get_all_tool_instances()}
    assert set(agent_tools.SPE_AGENT_TOOL_NAMES) <= instances


@pytest.fixture
def backend(monkeypatch):
    """假会话/出站桩：记录创建与调用，可编排失败序列。"""
    creates: list[dict] = []
    calls: list[dict] = []
    state = SimpleNamespace(
        session_seq=0,
        call_results={},  # tool_name -> payload 或 Exception 序列耗尽后取 default
        default_result={"blockId": "blk_back", "ui": "product-table", "v": 1, "data": {"rows": []}},
    )

    async def fake_create_session(**kwargs):
        state.session_seq += 1
        creates.append(kwargs)
        return f"sess-{state.session_seq}"

    async def fake_call_tool(**kwargs):
        calls.append(kwargs)
        result = state.call_results.get(kwargs["tool_name"])
        if result is None:
            return state.default_result
        if isinstance(result, list):
            outcome = result.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(agent_tools, "create_spe_session", fake_create_session)
    monkeypatch.setattr(agent_tools, "call_spe_tool", fake_call_tool)
    monkeypatch.setattr(agent_tools, "spe_session_registry", SpeSessionRegistry())
    monkeypatch.setattr(agent_tools, "spe_agent_opts", _FakeOpts())
    monkeypatch.setattr(agent_tools, "spe_credential_repository", _FakeRepo())
    return SimpleNamespace(creates=creates, calls=calls, state=state)


async def test_happy_path_creates_session_and_passes_through_block(backend) -> None:
    result = await agent_tools.search_products.coroutine(runtime=_runtime(), keyword="保暖内衣", limit=20)
    assert result == backend.state.default_result  # 后端信封原样透传
    assert len(backend.creates) == 1
    assert backend.creates[0]["user_jwt"] == "jwt-u1"
    assert backend.creates[0]["title"].startswith("yuxi u1")
    call = backend.calls[0]
    assert call["service_token"] == "svc"
    assert call["session_id"] == "sess-1"
    assert call["tool_name"] == "search_products"
    assert call["args"] == {"keyword": "保暖内衣", "limit": 20}


async def test_session_reused_across_calls(backend) -> None:
    await agent_tools.search_products.coroutine(runtime=_runtime())
    await agent_tools.store_metrics.coroutine(runtime=_runtime(), days=30)
    assert len(backend.creates) == 1  # 同 uid 复用会话
    assert [c["tool_name"] for c in backend.calls] == ["search_products", "store_metrics"]


async def test_session_not_found_rebuilds_and_retries_once(backend) -> None:
    backend.state.call_results["store_metrics"] = [
        SpeToolError("SESSION_NOT_FOUND", "会话不存在"),
        backend.state.default_result,
    ]
    result = await agent_tools.store_metrics.coroutine(runtime=_runtime(), days=7)
    assert result == backend.state.default_result
    assert len(backend.creates) == 2  # 重建一次
    assert len(backend.calls) == 2  # 重试一次且成功
    assert backend.calls[1]["session_id"] == "sess-2"


async def test_retry_only_once(backend) -> None:
    backend.state.call_results["store_metrics"] = [
        SpeToolError("SESSION_NOT_ACTIVE", "会话不活跃"),
        SpeToolError("SESSION_NOT_ACTIVE", "重建后仍不活跃"),
    ]
    with pytest.raises(ToolException) as info:
        await agent_tools.store_metrics.coroutine(runtime=_runtime())
    assert "SESSION_NOT_ACTIVE" in str(info.value)
    assert len(backend.calls) == 2  # 绝无第三次


async def test_non_session_error_no_retry(backend) -> None:
    backend.state.call_results["keyword_hot_words"] = [SpeToolError("TOOL_BUDGET_EXCEEDED", "预算已用尽")]
    with pytest.raises(ToolException) as info:
        await agent_tools.keyword_hot_words.coroutine(runtime=_runtime())
    assert "TOOL_BUDGET_EXCEEDED" in str(info.value)
    assert "不要重试" in str(info.value)  # guidance 随错误附出
    assert len(backend.calls) == 1 and len(backend.creates) == 1


async def test_distinct_uids_get_distinct_sessions(backend) -> None:
    await agent_tools.search_products.coroutine(runtime=_runtime("u1"))
    await agent_tools.search_products.coroutine(runtime=_runtime("u2"))
    assert len(backend.creates) == 2


async def test_not_logged_in_fails_closed(backend, monkeypatch) -> None:
    monkeypatch.setattr(agent_tools, "spe_credential_repository", _FakeRepo(jwt=None))
    with pytest.raises(ToolException) as info:
        await agent_tools.search_products.coroutine(runtime=_runtime())
    assert "BIZ_AUTH_FAILED" in str(info.value)
    assert backend.creates == [] and backend.calls == []


async def test_missing_service_token_fails_closed(backend, monkeypatch) -> None:
    monkeypatch.setattr(agent_tools, "spe_agent_opts", _FakeOpts(service_token=None))
    with pytest.raises(ToolException) as info:
        await agent_tools.search_products.coroutine(runtime=_runtime())
    assert "SPE_NOT_CONFIGURED" in str(info.value)
    assert backend.creates == [] and backend.calls == []


async def test_missing_uid_reports_error(backend) -> None:
    result = await agent_tools.search_products.coroutine(runtime=_runtime(""))
    assert result == {"ok": False, "error": "无法获取当前用户身份"}
