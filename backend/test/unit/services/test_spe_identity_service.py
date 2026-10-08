"""SPE 账号联邦登录测试：角色映射、自动开户、角色同步与凭据绑定。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from yuxi.services import spe_identity_service as svc


def test_role_mapping_tenant_admin_scoped() -> None:
    """2026-10-08 spec §4.2：SPE ADMIN → tenant_admin（部门限管理，非平台 admin）；
    SUPER_ADMIN/未知/缺失 fail-safe 向下为 user。"""
    assert svc.map_spe_role_to_yuxi("SUPER_ADMIN") == "user"
    assert svc.map_spe_role_to_yuxi("superadmin") == "user"
    assert svc.map_spe_role_to_yuxi("ADMIN") == "tenant_admin"
    assert svc.map_spe_role_to_yuxi("ROLE_ADMIN") == "tenant_admin"
    assert svc.map_spe_role_to_yuxi("USER") == "user"
    assert svc.map_spe_role_to_yuxi("VIEWER") == "user"
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

    async def _noop_kb_provision(**kwargs) -> None:
        return None  # 默认桩：登录测试不构造真实 KB runtime/不触 DB（密闭化）

    monkeypatch.setattr(svc, "spe_agent_opts", _FakeOpts())
    monkeypatch.setattr(svc, "spe_credential_repository", cred)
    monkeypatch.setattr(svc, "call_spe_credit_balance", _credit_ok())
    monkeypatch.setattr(svc, "_ensure_tenant_kb", _noop_kb_provision)
    monkeypatch.setattr(svc, "_rescope_personal_kbs", _noop_kb_provision)
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
    assert user.role == "tenant_admin"  # SPE ADMIN → tenant_admin（2026-10-08 spec §4.2）
    assert user.department_id == 101
    assert wiring.cred.bound == [("spe_alice", "jwt-spe", db)]  # 登录即绑定（同事务会话）
    assert db.added and db.flushed >= 1 and db.added[0].name == "SPE租户-5"  # 部门=SPE 租户（2026-10-08 spec §4.1）


async def test_existing_user_role_and_department_synced(wiring) -> None:
    """身份源拥有角色与租户归属：每次登录同步（spec §4.1/§4.2）。"""
    wiring.monkeypatch.setattr(svc, "call_spe_login", _spe_ok(role="SUPER_ADMIN", tenantId=9))
    existing = SimpleNamespace(id=7, uid="spe_alice", username="alice", role="user", department_id=1, is_deleted=0)
    db = _FakeScalarSession([SimpleNamespace(id=55), existing])  # 租户部门命中，用户命中
    user, _ = await svc.login_with_spe(username="alice", password="pw", db=db)
    assert user.role == "user"  # SUPER_ADMIN 收紧为 user
    assert user.department_id == 55  # 部门随租户迁移
    assert db.added == []  # 不重复开户


async def test_username_collision_gets_suffix(wiring) -> None:
    wiring.monkeypatch.setattr(svc, "call_spe_login", _spe_ok(username="张文杰"))
    db = _FakeScalarSession([None, None, SimpleNamespace(id=1)])  # 部门未命中→创建、用户未命中、同名本地用户命中
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


@pytest.mark.parametrize("tenant_id", [None, "", "   "])
async def test_missing_tenant_id_falls_back_to_legacy_department(wiring, tenant_id) -> None:
    """tenantId 缺失/为空：回退旧「SPE 外部用户」大部门（无租户语义，fail-safe）。"""
    wiring.monkeypatch.setattr(svc, "call_spe_login", _spe_ok(tenantId=tenant_id))
    db = _FakeScalarSession([])
    user, _ = await svc.login_with_spe(username="alice", password="pw", db=db)
    assert db.added[0].name == "SPE 外部用户"


async def test_zero_tenant_id_uses_tenant_department(wiring) -> None:
    """tenantId=0 是显式租户值：走租户部门分支（SPE租户-0），不被 falsy 吞入回退。"""
    wiring.monkeypatch.setattr(svc, "call_spe_login", _spe_ok(tenantId=0))
    db = _FakeScalarSession([])
    user, _ = await svc.login_with_spe(username="alice", password="pw", db=db)
    assert db.added[0].name == "SPE租户-0"


# ---------- 租户库 get-or-create 与个人库懒改档（spec §6.1/§6.2） ----------


def _kb_summary_row(kb_id, *, access_level="department", department_ids=None, user_uids=None,
                    created_by="spe_alice", file_count=0):
    read_scope = {"access_level": access_level,
                  "department_ids": department_ids or [],
                  "user_uids": user_uids or []}
    return SimpleNamespace(
        kb_id=kb_id, name=kb_id, created_by=created_by, file_count=file_count,
        share_config={"version": 2, "read_scope": read_scope, "manage_scope": None},
    )


def _fake_kb_runtime(monkeypatch, kbs, created=None):
    from yuxi.knowledge import runtime as kb_runtime

    async def _get_databases():
        return list(kbs)

    async def _create(*args, **kwargs):
        if created is not None:
            created.append(kwargs)
        return SimpleNamespace(kb_id="kb_new")

    monkeypatch.setattr(kb_runtime, "knowledge_base",
                        SimpleNamespace(get_databases=_get_databases, create_database=_create))


async def test_ensure_tenant_kb_skips_when_exact_department_kb_exists(monkeypatch):
    created = []
    _fake_kb_runtime(monkeypatch, [_kb_summary_row("kb_t", department_ids=[7])], created=created)
    await svc._ensure_tenant_kb(uid="spe_alice", department_id=7, tenant_id=5)
    assert created == []


async def test_ensure_tenant_kb_creates_department_scoped_kb(monkeypatch):
    created = []
    _fake_kb_runtime(monkeypatch, [], created=created)
    await svc._ensure_tenant_kb(uid="spe_alice", department_id=7, tenant_id=5)
    assert len(created) == 1
    assert created[0].get("share_config")["read_scope"]["department_ids"] == [7]
    assert created[0].get("share_config")["manage_scope"]["department_ids"] == [7]
    # created_by 系统化（spec §6.1 修订）：首登成员不得经 owner 分支获越角色写权
    assert created[0]["created_by"] == "spe_system"


async def test_ensure_tenant_kb_name_conflict_tolerated(monkeypatch):
    """并发首登：KBNameConflictError → 视为他人已建，静默复用（spec §6.1/§11）。"""
    from yuxi.knowledge.base import KBNameConflictError
    from yuxi.knowledge import runtime as kb_runtime

    async def _create(*args, **kwargs):
        raise KBNameConflictError("知识库名称 'SPE租户-5知识库' 已存在")

    async def _get_databases():
        return []

    monkeypatch.setattr(kb_runtime, "knowledge_base",
                        SimpleNamespace(get_databases=_get_databases, create_database=_create))
    await svc._ensure_tenant_kb(uid="spe_alice", department_id=7, tenant_id=5)  # 不抛即通过


async def test_rescope_personal_kbs_only_own_nonempty(monkeypatch):
    from yuxi.knowledge import runtime as kb_runtime
    from yuxi.repositories import knowledge_base_repository as kb_repo_mod

    kbs = [
        _kb_summary_row("kb_mine_full", access_level="user", user_uids=["spe_alice"], file_count=3),
        _kb_summary_row("kb_mine_empty", access_level="user", user_uids=["spe_alice"], file_count=0),
        _kb_summary_row("kb_other", access_level="user", user_uids=["spe_bob"], file_count=9),
        _kb_summary_row("kb_dept", department_ids=[7], file_count=5),
    ]

    async def _get_databases():
        return list(kbs)

    monkeypatch.setattr(kb_runtime, "knowledge_base", SimpleNamespace(get_databases=_get_databases))

    updates = []

    class _FakeRepo:
        async def update(self, kb_id, data):
            updates.append((kb_id, data))

    monkeypatch.setattr(kb_repo_mod, "KnowledgeBaseRepository", _FakeRepo)
    await svc._rescope_personal_kbs(uid="spe_alice", department_id=7)
    assert [u[0] for u in updates] == ["kb_mine_full"]
    config = updates[0][1]["share_config"]
    assert config["read_scope"]["department_ids"] == [7] and config["manage_scope"]["department_ids"] == [7]


async def test_ensure_tenant_provisioning_gates(monkeypatch) -> None:
    """供给入口门控（路由 commit 后调用）：tenantId 非空且 department_id 就绪才触发。"""
    calls = []

    async def fake_ensure(**kw):
        calls.append(("ensure", kw["department_id"], kw["tenant_id"]))

    async def fake_rescope(**kw):
        calls.append(("rescope", kw["department_id"]))

    monkeypatch.setattr(svc, "_ensure_tenant_kb", fake_ensure)
    monkeypatch.setattr(svc, "_rescope_personal_kbs", fake_rescope)

    await svc.ensure_tenant_provisioning(uid="spe_alice", department_id=101, tenant_id=5)
    assert calls == [("ensure", 101, 5), ("rescope", 101)]

    for tenant_id in (None, "  "):  # tenantId 缺失/空白 → 无租户语义，跳过
        calls.clear()
        await svc.ensure_tenant_provisioning(uid="spe_alice", department_id=101, tenant_id=tenant_id)
        assert calls == []

    calls.clear()  # 无部门（未就绪）→ 跳过
    await svc.ensure_tenant_provisioning(uid="spe_alice", department_id=None, tenant_id=5)
    assert calls == []


async def test_login_with_spe_no_longer_provisions_kbs(wiring) -> None:
    """登录事务内不做租户供给——供给已后移到路由 commit 之后（评审修复 Issue 1）。"""
    calls = []

    async def record_ensure(**kw):
        calls.append("ensure")

    async def record_rescope(**kw):
        calls.append("rescope")

    wiring.monkeypatch.setattr(svc, "call_spe_login", _spe_ok())
    wiring.monkeypatch.setattr(svc, "_ensure_tenant_kb", record_ensure)
    wiring.monkeypatch.setattr(svc, "_rescope_personal_kbs", record_rescope)
    db = _FakeScalarSession([])
    await svc.login_with_spe(username="alice", password="pw", db=db)
    assert calls == []


# ---------- 空个人库清理判定（spec §6.2：只删 0 文件 user 档 spe_ 前缀库） ----------


def test_is_empty_spe_personal_kb():
    row = SimpleNamespace(
        created_by="spe_alice", file_count=0,
        share_config={"version": 2, "read_scope": {"access_level": "user", "user_uids": ["spe_alice"]}, "manage_scope": None},
    )
    assert svc.is_empty_spe_personal_kb(row) is True
    assert svc.is_empty_spe_personal_kb(SimpleNamespace(created_by="spe_alice", file_count=3, share_config=row.share_config)) is False  # 非空留懒改档
    assert svc.is_empty_spe_personal_kb(SimpleNamespace(created_by="zwj", file_count=0, share_config=row.share_config)) is False  # 本地账号
    global_cfg = {"version": 2, "read_scope": {"access_level": "global", "user_uids": []}, "manage_scope": None}
    assert svc.is_empty_spe_personal_kb(SimpleNamespace(created_by="spe_alice", file_count=0, share_config=global_cfg)) is False  # 官方库
    # 防护（Task 6 评审提醒）：spe_system 也以 spe_ 开头——系统预建租户库是 department 档，
    # 靠 access_level == "user" 条件排除；空库清理永不触碰系统租户库。
    dept_cfg = {"version": 2, "read_scope": {"access_level": "department", "department_ids": [7], "user_uids": []}, "manage_scope": None}
    assert svc.is_empty_spe_personal_kb(SimpleNamespace(created_by="spe_system", file_count=0, share_config=dept_cfg)) is False  # 系统租户库
