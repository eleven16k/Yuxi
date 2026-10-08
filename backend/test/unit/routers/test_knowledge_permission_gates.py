"""知识库 HTTP 门禁测试（spec 2026-10-08 §7）：门=登录+资源权限，非 admin 角色。"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from server.utils import knowledge_permissions as kp


def _user(role, uid="u1", department_id=None):
    return SimpleNamespace(role=role, uid=uid, department_id=department_id, is_deleted=0)


_DEPT7 = {"access_level": "department", "department_ids": [7], "user_uids": []}


@pytest.fixture
def dept_kb_runtime(monkeypatch):
    detail = SimpleNamespace(
        kb_id="kb_t", name="租户库", created_by="spe_owner", additional_params={},
        share_config={"version": 2, "read_scope": _DEPT7, "manage_scope": _DEPT7},
    )

    async def fake_info(kb_id):
        return detail if kb_id == "kb_t" else None

    monkeypatch.setattr(kp.knowledge_base, "get_database_info", fake_info)
    return detail


async def test_tenant_admin_manage_own_tenant_kb(dept_kb_runtime):
    user = await kp.require_knowledge_base_manage(kb_id="kb_t", current_user=_user("tenant_admin", department_id=7))
    assert user.role == "tenant_admin"


async def test_member_read_ok_manage_denied(dept_kb_runtime):
    await kp.require_knowledge_base_read(kb_id="kb_t", current_user=_user("user", department_id=7))
    with pytest.raises(HTTPException) as info:
        await kp.require_knowledge_base_manage(kb_id="kb_t", current_user=_user("user", department_id=7))
    assert info.value.status_code == 403


async def test_other_tenant_denied(dept_kb_runtime):
    with pytest.raises(HTTPException) as info:
        await kp.require_knowledge_base_read(kb_id="kb_t", current_user=_user("tenant_admin", department_id=9))
    assert info.value.status_code == 403


# ---------- /files/upload 无 kb_id 直传仅平台管理员（spec §7） ----------

from server.routers import knowledge_router as kr


async def test_upload_without_kb_id_admin_only():
    kr._require_upload_role(None, _user("admin"))  # 不抛
    kr._require_upload_role("kb_t", _user("user"))  # 带 kb_id 不受此门
    with pytest.raises(HTTPException) as info:
        kr._require_upload_role(None, _user("user"))
    assert info.value.status_code == 403
