"""accessible 列表条目序列化测试（spec 2026-10-08 §7/§9：官方标注 + can_manage）。"""

from yuxi.knowledge.read_models import KnowledgeBaseSummary
from yuxi.permissions import ResourcePermission
from server.utils.knowledge_response import serialize_accessible_knowledge_base


def _summary(**over):
    base = dict(
        kb_id="kb1", name="库", description=None, kb_type="milvus",
        embedding_model_spec=None, llm_model_spec=None, query_params={}, additional_params={},
        share_config={"version": 2, "read_scope": {"access_level": "global", "department_ids": [], "user_uids": []}, "manage_scope": None},
        created_by="boss", created_at=None, effective_permission=ResourcePermission.READ,
    )
    base.update(over)
    return KnowledgeBaseSummary(**base)


def test_official_flag_from_global_read_scope():
    assert serialize_accessible_knowledge_base(_summary())["is_official"] is True
    dept_scope = {"access_level": "department", "department_ids": [7], "user_uids": []}
    tenant = _summary(share_config={"version": 2, "read_scope": dept_scope, "manage_scope": dept_scope})
    assert serialize_accessible_knowledge_base(tenant)["is_official"] is False


def test_can_manage_and_stats_fields_present():
    item = serialize_accessible_knowledge_base(_summary(effective_permission=ResourcePermission.MANAGE, file_count=12))
    assert item["can_manage"] is True
    assert item["file_count"] == 12
    assert item["created_at"] is None  # created_at=None 安全序列化
