"""SPE 账号联邦登录测试：角色映射、自动开户、角色同步与凭据绑定。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from yuxi.services import spe_identity_service as svc


def test_role_mapping_tightened_no_superadmin_for_tenants() -> None:
    """租户侧永不获得 superadmin（2026-10-07 收紧，spec §4.2）：
    SUPER_ADMIN 映射 user；仅 ADMIN→admin；未知/缺失 fail-safe 向下。"""
    assert svc.map_spe_role_to_yuxi("SUPER_ADMIN") == "user"
    assert svc.map_spe_role_to_yuxi("superadmin") == "user"
    assert svc.map_spe_role_to_yuxi("ADMIN") == "admin"
    assert svc.map_spe_role_to_yuxi("ROLE_ADMIN") == "admin"
    assert svc.map_spe_role_to_yuxi("USER") == "user"
    assert svc.map_spe_role_to_yuxi(None) == "user"
    assert svc.map_spe_role_to_yuxi("whatever") == "user"


def test_spe_uid_namespace() -> None:
    assert svc.spe_uid("alice") == "spe_alice"


class _FakeScalarSession:
    """按查询目标返回预置结果的假会话：None = 未命中（走创建分支）。"""

    def __init__(self, results: list) -> None:
        self._results = list(results)
        self.added: list = []
        self.flushed = 0
        self.committed = 0

    async def scalar(self, query):
        return self._results.pop(0) if self._results else None

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = 101

    async def refresh(self, obj) -> None:
        pass

    async def commit(self) -> None:
        self.committed += 1


class _FakeOpts:
    def __init__(self, base_url: str | None = "http://spe.test") -> None:
        self.base_url = base_url

    async def get(self) -> dict:
        return {"base_url": self.base_url, "service_token": "svc"}


class _FakeCredRepo:
    def __init__(self) -> None:
        self.bound: list[tuple[str, str, object]] = []

    async def upsert_user_jwt(self, *, uid, user_jwt, db=None) -> None:
        self.bound.append((uid, user_jwt, db))


@pytest.fixture
def wiring(monkeypatch):
    cred = _FakeCredRepo()
    monkeypatch.setattr(svc, "spe_agent_opts", _FakeOpts())
    monkeypatch.setattr(svc, "spe_credential_repository", cred)
    monkeypatch.setattr(svc, "call_spe_credit_balance", _credit_ok())
    return SimpleNamespace(cred=cred, monkeypatch=monkeypatch)


def _spe_ok(**profile):
    async def fake_login(**kwargs):
        return {"token": "jwt-spe", "role": "ADMIN", "username": "alice", "tenantId": 5, **profile}

    return fake_login


def _credit_ok(balance: int = 1000):
    async def fake_balance(**kwargs):
        return {"creditBalance": balance, "creditFrozen": 0}

    return fake_balance


async def test_first_login_provisions_user_and_binds_jwt(wiring) -> None:
    wiring.monkeypatch.setattr(svc, "call_spe_login", _spe_ok())
    db = _FakeScalarSession([])  # 部门未命中 → 创建；用户未命中 → 创建
    user, profile = await svc.login_with_spe(username="alice", password="pw", db=db)
    assert user.uid == "spe_alice" and user.username == "alice"
    assert user.role == "admin"  # SPE ADMIN → Yuxi admin
    assert user.department_id == 101
    assert wiring.cred.bound == [("spe_alice", "jwt-spe", db)]  # 登录即绑定（同事务会话）
    assert db.added and db.flushed >= 1


async def test_existing_user_role_synced_from_spe(wiring) -> None:
    wiring.monkeypatch.setattr(svc, "call_spe_login", _spe_ok(role="SUPER_ADMIN"))
    existing = SimpleNamespace(id=7, uid="spe_alice", username="alice", role="user", department_id=1, is_deleted=0)
    db = _FakeScalarSession([existing])  # 用户命中
    user, _ = await svc.login_with_spe(username="alice", password="pw", db=db)
    assert user.role == "user"  # 身份源拥有角色：每次登录同步（SUPER_ADMIN 已收紧为 user）
    assert db.added == []  # 不重复开户


async def test_username_collision_gets_suffix(wiring) -> None:
    wiring.monkeypatch.setattr(svc, "call_spe_login", _spe_ok(username="张文杰"))
    db = _FakeScalarSession([None, SimpleNamespace(id=1)])  # 用户未命中、同名本地用户命中
    user, _ = await svc.login_with_spe(username="张文杰", password="pw", db=db)
    assert user.uid == "spe_张文杰" and user.username == "张文杰(SPE)"


async def test_unconfigured_spe_fails_closed(wiring) -> None:
    wiring.monkeypatch.setattr(svc, "spe_agent_opts", _FakeOpts(base_url=None))
    with pytest.raises(HTTPException) as info:
        await svc.login_with_spe(username="a", password="b", db=_FakeScalarSession([]))
    assert info.value.status_code == 400 and "SPE 未配置" in info.value.detail


async def test_spe_wrong_password_maps_401(wiring) -> None:
    from yuxi.agents.toolkits.spe.client import SpeToolError

    async def reject(**kwargs):
        raise SpeToolError("AUTH_FAILED", "用户名或密码错误")

    wiring.monkeypatch.setattr(svc, "call_spe_login", reject)
    with pytest.raises(HTTPException) as info:
        await svc.login_with_spe(username="a", password="bad", db=_FakeScalarSession([]))
    assert info.value.status_code == 401
    assert wiring.cred.bound == []


async def test_zero_balance_blocks_login_without_provisioning(wiring) -> None:
    """入口闸（spec §8）：零余额 403 拒绝，且不开户、不绑定凭证。"""
    wiring.monkeypatch.setattr(svc, "call_spe_login", _spe_ok())
    wiring.monkeypatch.setattr(svc, "call_spe_credit_balance", _credit_ok(balance=0))
    db = _FakeScalarSession([])
    with pytest.raises(HTTPException) as info:
        await svc.login_with_spe(username="alice", password="pw", db=db)
    assert info.value.status_code == 403
    assert "积分余额不足" in info.value.detail
    assert db.added == []  # 闸门先于开户
    assert wiring.cred.bound == []  # 闸门先于凭证绑定


async def test_credit_query_upstream_error_maps_502(wiring) -> None:
    """积分查询上游故障 → 502（fail-closed，不静默放行）。"""
    from yuxi.agents.toolkits.spe.client import SpeToolError

    async def broken(**kwargs):
        raise SpeToolError("TOOL_UPSTREAM_ERROR", "积分查询连接失败")

    wiring.monkeypatch.setattr(svc, "call_spe_login", _spe_ok())
    wiring.monkeypatch.setattr(svc, "call_spe_credit_balance", broken)
    with pytest.raises(HTTPException) as info:
        await svc.login_with_spe(username="alice", password="pw", db=_FakeScalarSession([]))
    assert info.value.status_code == 502


async def test_first_login_provisions_personal_kb(wiring) -> None:
    """开户即预建个人知识库（P1#05）；既有用户登录不重复建。"""
    calls: list[tuple[str, str]] = []

    async def fake_provision(uid: str, display_name: str) -> None:
        calls.append((uid, display_name))

    wiring.monkeypatch.setattr(svc, "_provision_personal_kb", fake_provision)
    wiring.monkeypatch.setattr(svc, "call_spe_login", _spe_ok())
    db = _FakeScalarSession([])
    user, _ = await svc.login_with_spe(username="alice", password="pw", db=db)
    assert calls == [("spe_alice", "alice")]

    # 既有用户（用户命中）不再触发
    existing = SimpleNamespace(id=7, uid="spe_alice", username="alice", role="user", department_id=1, is_deleted=0)
    db2 = _FakeScalarSession([existing])
    await svc.login_with_spe(username="alice", password="pw", db=db2)
    assert calls == [("spe_alice", "alice")]
