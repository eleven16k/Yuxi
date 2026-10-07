"""spe 工具面（spe_ping / login / logout）单测：全桩替换，零出站。

桩替换点都在 tools 模块命名空间：call_spe_login、spe_credential_repository、
spe_agent_opts、spe_session_registry。断言 login 是凭据唯一写入口、会话注册表
随身份变化失效、错误以带 guidance 的 ToolException 冒泡。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.tools import ToolException

from yuxi.agents.toolkits.spe import tools as spe_tools
from yuxi.agents.toolkits.spe.client import SpeToolError


def _runtime(uid: str = "u1"):
    return SimpleNamespace(context=SimpleNamespace(uid=uid))


class _FakeRegistry:
    def __init__(self) -> None:
        self.invalidated: list[str] = []

    def invalidate(self, key: str) -> None:
        self.invalidated.append(key)


class _FakeRepo:
    def __init__(self, existing_jwt: str | None = None) -> None:
        self.existing_jwt = existing_jwt
        self.stored: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    async def get_user_jwt(self, uid: str) -> str | None:
        return self.existing_jwt

    async def upsert_user_jwt(self, *, uid: str, user_jwt: str) -> None:
        self.stored.append((uid, user_jwt))

    async def delete_user_jwt(self, uid: str) -> None:
        self.deleted.append(uid)


class _FakeOpts:
    def __init__(self, base_url: str | None) -> None:
        self.base_url = base_url

    async def get(self) -> dict:
        return {"base_url": self.base_url, "service_token": "svc"}


LOGIN_RESPONSE = {"token": "jwt-new", "role": "ADMIN", "tenantId": 5001, "username": "alice"}


@pytest.fixture
def wiring(monkeypatch):
    """统一桩装配，返回可断言的假件与最近一次出站 kwargs。"""

    state = SimpleNamespace(kwargs=None)

    async def fake_login_call(**kwargs):
        state.kwargs = kwargs
        return LOGIN_RESPONSE

    repo = _FakeRepo()
    registry = _FakeRegistry()
    monkeypatch.setattr(spe_tools, "call_spe_login", fake_login_call)
    monkeypatch.setattr(spe_tools, "spe_credential_repository", repo)
    monkeypatch.setattr(spe_tools, "spe_session_registry", registry)
    monkeypatch.setattr(spe_tools, "spe_agent_opts", _FakeOpts("http://spe.test"))
    return SimpleNamespace(repo=repo, registry=registry, outbound=state)


async def test_ping_returns_ok(wiring) -> None:
    result = await spe_tools.spe_ping.coroutine(dummy="", runtime=_runtime())
    assert result == {"ok": True}


async def test_login_stores_jwt_and_invalidates_session(wiring) -> None:
    wiring.repo.existing_jwt = "jwt-old"
    result = await spe_tools.login.coroutine(username="alice", password="pw", runtime=_runtime("u1"))
    assert result == {
        "ok": True,
        "data": {
            "success": True,
            "role": "ADMIN",
            "tenantId": 5001,
            "username": "alice",
            "message": "登录成功",
        },
    }
    # 既有 JWT 作为 bearer 传递；新 JWT 覆盖存储；会话失效以重建
    assert wiring.outbound.kwargs["bearer"] == "jwt-old"
    assert wiring.repo.stored == [("u1", "jwt-new")]
    assert wiring.registry.invalidated == ["u1"]


async def test_login_without_prior_jwt_sends_no_bearer(wiring) -> None:
    await spe_tools.login.coroutine(username="alice", password="pw", runtime=_runtime())
    assert wiring.outbound.kwargs["bearer"] is None
    assert wiring.repo.stored == [("u1", "jwt-new")]


async def test_login_rejects_blank_args(wiring) -> None:
    with pytest.raises(ToolException) as info:
        await spe_tools.login.coroutine(username="  ", password="pw", runtime=_runtime())
    assert "INVALID_ARGS" in str(info.value)
    assert wiring.repo.stored == []


async def test_login_maps_auth_failed_with_guidance(wiring, monkeypatch) -> None:
    async def reject(**kwargs):
        raise SpeToolError("AUTH_FAILED", "用户名或密码错误")

    monkeypatch.setattr(spe_tools, "call_spe_login", reject)
    with pytest.raises(ToolException) as info:
        await spe_tools.login.coroutine(username="u", password="bad", runtime=_runtime())
    # AUTH_FAILED 是表外运行时码（桥同款）：走回落文案，原文因随错误附出
    assert "AUTH_FAILED: 用户名或密码错误" in str(info.value)
    assert "处置：" in str(info.value)
    assert wiring.repo.stored == []  # 失败绝不写凭据


async def test_login_fails_closed_without_base_url(monkeypatch) -> None:
    monkeypatch.setattr(spe_tools, "spe_agent_opts", _FakeOpts(None))
    with pytest.raises(ToolException) as info:
        await spe_tools.login.coroutine(username="u", password="p", runtime=_runtime())
    assert "SPE_NOT_CONFIGURED" in str(info.value)


async def test_login_without_uid_reports_error(wiring) -> None:
    result = await spe_tools.login.coroutine(username="u", password="p", runtime=_runtime(""))
    assert result == {"ok": False, "error": "无法获取当前用户身份"}
    assert wiring.repo.stored == []


async def test_logout_deletes_credential_and_session(wiring) -> None:
    result = await spe_tools.logout.coroutine(dummy="", runtime=_runtime("u1"))
    assert result == {"ok": True, "data": {"success": True, "message": "已退出登录"}}
    assert wiring.repo.deleted == ["u1"]
    assert wiring.registry.invalidated == ["u1"]


async def test_logout_without_uid_reports_error(wiring) -> None:
    result = await spe_tools.logout.coroutine(dummy="", runtime=_runtime(""))
    assert result == {"ok": False, "error": "无法获取当前用户身份"}
    assert wiring.repo.deleted == []
