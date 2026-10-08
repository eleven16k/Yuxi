"""内化写工具（kb_upload / yuxi_research）测试：门禁、五步链、轮询与研究三态。

惰性导入纪律：services 依赖在函数内导入，桩必须打在源模块
（knowledge_file_service / task_service / agent_request_service /
agent_run_service）而非本包命名空间。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from langchain_core.tools import ToolException

from yuxi.agents.toolkits.spe import internal_write
from yuxi.agents.toolkits.spe.receipts import WriteReceipts
from yuxi.services import agent_request_service, agent_run_service, knowledge_file_service, task_service


def _runtime(uid: str = "u1"):
    return SimpleNamespace(context=SimpleNamespace(uid=uid))


class _FakeSession:
    def __init__(self, scalar_result=None) -> None:
        self.scalar_result = scalar_result

    async def scalar(self, query):
        return self.scalar_result


class _FakePG:
    def __init__(self, scalar_result=None) -> None:
        self.scalar_result = scalar_result

    def get_async_session_context(self):
        session = _FakeSession(self.scalar_result)

        @asynccontextmanager
        async def ctx():
            yield session

        return ctx()


class _FakeTasker:
    def __init__(self) -> None:
        self.enqueued: list[dict] = []
        self._seq = iter(range(1, 100))

    async def enqueue(self, **kwargs):
        self.enqueued.append(kwargs)
        return SimpleNamespace(id=f"task{next(self._seq)}")


class _FakeKB:
    def __init__(self, add_result=None, poll_statuses=None) -> None:
        self.add_result = add_result or {"items": [{"status": "success", "file_id": "f1"}]}
        self.poll_statuses = poll_statuses or []
        self.add_calls: list[dict] = []
        self.files_calls = 0

    async def add_file_record(self, kb_id, item, params, operator_id=None):
        self.add_calls.append({"kb_id": kb_id, "item": item, "params": params, "operator_id": operator_id})
        return self.add_result

    async def search_document_files(self, kbs, **kwargs):
        self.files_calls += 1
        status = (
            self.poll_statuses[min(self.files_calls - 1, len(self.poll_statuses) - 1)]
            if self.poll_statuses
            else "parsed"
        )
        return {"files": [{"file_id": "f1", "filename": "x.md", "status": status}], "total": 1}


def _kb_summary(kb_id: str = "kb1", *, can_manage: bool = True):
    return {
        "kb_id": kb_id,
        "name": "运营库",
        "kb_name": "运营库",
        "kb_type": "milvus",
        "is_official": False,
        "can_manage": can_manage,
    }


@pytest.fixture
def upload_env(monkeypatch):
    """kb_upload 桩环境：可管理库（无角色门）+ 成功五步链。"""
    tasker = _FakeTasker()
    fake_kb = _FakeKB()
    uploads: list[dict] = []

    async def fake_upload(*, kb_id, filename, file_bytes, max_size_bytes=None):
        uploads.append({"kb_id": kb_id, "filename": filename, "size": len(file_bytes)})
        return {
            "file_path": "minio://doc/x.md",
            "minio_path": "minio://doc/x.md",
            "kb_id": kb_id,
            "content_hash": "hash-full",
            "filename": filename,
            "size": len(file_bytes),
        }

    # quota 服务为函数内惰性 import：桩必须打在源模块属性上（spec 2026-10-08 §8）
    from yuxi.services import knowledge_quota_service as quota_svc

    async def fake_quota(kb_id, **kwargs):
        return None

    monkeypatch.setattr(quota_svc, "ensure_tenant_kb_quota", fake_quota)
    monkeypatch.setattr(knowledge_file_service, "upload_knowledge_file_bytes", fake_upload)
    monkeypatch.setattr(task_service, "tasker", tasker)
    monkeypatch.setattr(internal_write, "_kb", lambda: fake_kb)
    monkeypatch.setattr(internal_write, "visible_kb_summaries", _returning([_kb_summary()]))
    monkeypatch.setattr(internal_write, "write_receipts", WriteReceipts())
    return SimpleNamespace(tasker=tasker, fake_kb=fake_kb, uploads=uploads, monkeypatch=monkeypatch)


def _returning(value):
    async def _fake(runtime):
        return value

    return _fake


# ---------- kb_upload：门禁与校验 ----------


async def test_kb_upload_no_admin_role_gate(upload_env) -> None:
    """2026-10-08 spec §7：角色门移除——目标库 MANAGE 是唯一权限闸。

    普通 user 对可见且可管理的库全链放行（角色不再是阻断条件）。
    """
    result = await internal_write.kb_upload.coroutine(runtime=_runtime(), kb_id="kb1", filename="a.md", content="x")
    assert result["data"]["success"] is True
    assert upload_env.uploads and upload_env.uploads[0]["kb_id"] == "kb1"


async def test_kb_upload_invisible_kb_zero_probing(upload_env) -> None:
    upload_env.monkeypatch.setattr(internal_write, "visible_kb_summaries", _returning([]))
    with pytest.raises(ToolException) as info:
        await internal_write.kb_upload.coroutine(runtime=_runtime(), kb_id="kb_other", filename="a.md", content="x")
    assert "YUXI_NOT_FOUND" in str(info.value)
    assert upload_env.uploads == []


async def test_kb_upload_requires_manage(upload_env) -> None:
    upload_env.monkeypatch.setattr(internal_write, "visible_kb_summaries", _returning([_kb_summary(can_manage=False)]))
    with pytest.raises(ToolException) as info:
        await internal_write.kb_upload.coroutine(runtime=_runtime(), kb_id="kb1", filename="a.md", content="x")
    assert "没有管理权限" in str(info.value)


async def test_kb_upload_param_validation(upload_env) -> None:
    cases = [
        {"kb_id": "", "filename": "a.md", "content": "x"},
        {"kb_id": "kb1", "filename": "a.exe", "content": "x"},
        {"kb_id": "kb1", "filename": "a/b.md", "content": "x"},
        {"kb_id": "kb1", "filename": "x" * 201 + ".md", "content": "x"},
        {"kb_id": "kb1", "filename": "a.md", "content": "x" * 1_000_001},
    ]
    for kwargs in cases:
        with pytest.raises(ToolException, match="INVALID_ARGS"):
            await internal_write.kb_upload.coroutine(runtime=_runtime(), **kwargs)
    assert upload_env.uploads == []


# ---------- kb_upload：五步链与回执 ----------


async def test_kb_upload_five_step_chain(upload_env) -> None:
    result = await internal_write.kb_upload.coroutine(
        runtime=_runtime(), kb_id="kb1", filename="sop.md", content="# 运营SOP"
    )
    data = result["data"]
    assert data["success"] is True and data["duplicate"] is False
    assert "解析与索引已排队" in data["summary"]
    assert data["details"]["fileId"] == "f1"
    assert data["details"]["taskIds"] == ["task1", "task2"]
    # 登记参数：content_hashes 透传 + operator 记真实用户
    add_call = upload_env.fake_kb.add_calls[0]
    assert add_call["params"]["content_hashes"] == {"minio://doc/x.md": "hash-full"}
    assert add_call["operator_id"] == "u1"
    assert [t["task_type"] for t in upload_env.tasker.enqueued] == ["knowledge_parse", "knowledge_index"]


async def test_kb_upload_poll_timeout_skips_index(upload_env, monkeypatch) -> None:
    upload_env.fake_kb.poll_statuses = ["uploaded", "uploaded"]
    monkeypatch.setattr(internal_write, "_POLL_DEADLINE_SECONDS", 0.0)
    result = await internal_write.kb_upload.coroutine(runtime=_runtime(), kb_id="kb1", filename="a.md", content="x")
    assert [t["task_type"] for t in upload_env.tasker.enqueued] == ["knowledge_parse"]  # 未触发 index
    assert "解析仍在进行" in result["data"]["summary"] and "索引未触发" in result["data"]["summary"]
    assert result["data"]["details"]["taskIds"] == ["task1"]


async def test_kb_upload_duplicate_content_maps_contract_code(upload_env, monkeypatch) -> None:
    from yuxi.services.knowledge_file_service import KnowledgeFileDuplicateError

    async def duplicate(*, kb_id, filename, file_bytes, max_size_bytes=None):
        raise KnowledgeFileDuplicateError("already exists")

    monkeypatch.setattr(knowledge_file_service, "upload_knowledge_file_bytes", duplicate)
    with pytest.raises(ToolException) as info:
        await internal_write.kb_upload.coroutine(runtime=_runtime(), kb_id="kb1", filename="a.md", content="x")
    assert "YUXI_DUPLICATE_CONTENT" in str(info.value)
    assert "无需重复上传" in str(info.value)


async def test_kb_upload_receipt_dedup_by_content_hash(upload_env) -> None:
    await internal_write.kb_upload.coroutine(runtime=_runtime(), kb_id="kb1", filename="a.md", content="same")
    second = await internal_write.kb_upload.coroutine(runtime=_runtime(), kb_id="kb1", filename="a.md", content="same")
    assert len(upload_env.uploads) == 1  # 幂等：未再次上传
    assert second["data"]["duplicate"] is True
    # 同内容异库是不同操作（kbId 必进键）
    upload_env.monkeypatch.setattr(
        internal_write, "visible_kb_summaries", _returning([_kb_summary(), _kb_summary("kb2")])
    )
    third = await internal_write.kb_upload.coroutine(runtime=_runtime(), kb_id="kb2", filename="a.md", content="same")
    assert third["data"]["duplicate"] is False and len(upload_env.uploads) == 2


# ---------- yuxi_research ----------


@pytest.fixture
def research_env(monkeypatch):
    submits: list[dict] = []
    poll_plan: list[dict] = []
    state = SimpleNamespace(run_seq=0)

    async def fake_submit(*, request_input, current_user, db):
        submits.append(
            {
                "agent_slug": request_input.agent_slug,
                "request_id": request_input.request_id,
                "origin_source": request_input.origin.source,
                "queue_policy": request_input.queue_policy,
                "uid": current_user.uid,
            }
        )
        state.run_seq += 1
        return {"run_id": f"run{state.run_seq}", "status": "dispatched"}

    async def fake_result(*, run_id, current_uid, db):
        return poll_plan.pop(0) if poll_plan else {"status": "pending", "output": ""}

    monkeypatch.setattr(agent_request_service, "submit_agent_request", fake_submit)
    monkeypatch.setattr(agent_run_service, "get_agent_run_result", fake_result)
    monkeypatch.setattr("yuxi.storage.postgres.manager.pg_manager", _FakePG(scalar_result=SimpleNamespace(uid="u1")))
    monkeypatch.setattr(internal_write, "write_receipts", WriteReceipts())
    return SimpleNamespace(submits=submits, poll_plan=poll_plan, monkeypatch=monkeypatch)


async def test_research_completed(research_env) -> None:
    research_env.poll_plan.extend(
        [
            {"status": "running", "output": ""},
            {"status": "completed", "output": "研究报告结论……", "thread_id": "t9", "token_usage": {"total": 100}},
        ]
    )
    result = await internal_write.yuxi_research.coroutine(runtime=_runtime(), question="俄罗斯厨具市场?")
    submit = research_env.submits[0]
    assert submit["agent_slug"] == "deep-research" and submit["origin_source"] == "spe_agent"
    assert submit["request_id"].startswith("spe-research-")  # 同题稳定派生
    data = result["data"]
    assert data["summary"] == "研究报告结论……"
    assert data["details"] == {"run_id": "run1", "output_chars": 8, "usage": {"total": 100}, "thread_id": "t9"}


async def test_research_request_id_stable_for_same_question(research_env) -> None:
    research_env.poll_plan.append({"status": "completed", "output": "ok"})
    await internal_write.yuxi_research.coroutine(runtime=_runtime(), question="同题")
    research_env.poll_plan.append({"status": "completed", "output": "ok"})
    await internal_write.yuxi_research.coroutine(runtime=_runtime(), question="同题")
    ids = {s["request_id"] for s in research_env.submits}
    assert len(ids) == 1  # 同题归并同一 run（幂等回吐）


async def test_research_output_truncated_at_500(research_env) -> None:
    research_env.poll_plan.append({"status": "completed", "output": "字" * 700})
    result = await internal_write.yuxi_research.coroutine(runtime=_runtime(), question="q")
    summary = result["data"]["summary"]
    assert summary.startswith("字" * 500) and "700 字符已截断" in summary
    assert result["data"]["details"]["output_chars"] == 700


async def test_research_failed_maps_contract_code(research_env) -> None:
    research_env.poll_plan.append({"status": "failed", "error": {"message": "模型超限"}})
    with pytest.raises(ToolException) as info:
        await internal_write.yuxi_research.coroutine(runtime=_runtime(), question="q")
    assert "YUXI_RESEARCH_FAILED" in str(info.value) and "run1" in str(info.value)


async def test_research_timeout_guides_resume(research_env, monkeypatch) -> None:
    monkeypatch.setattr(internal_write, "_RESEARCH_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(internal_write, "_RESEARCH_DEADLINE_SECONDS", 0.0)
    with pytest.raises(ToolException) as info:
        await internal_write.yuxi_research.coroutine(runtime=_runtime(), question="q")
    assert "YUXI_RESEARCH_TIMEOUT" in str(info.value) and "run1" in str(info.value)
    assert "续取同一 run" in str(info.value)


async def test_research_agent_unavailable(research_env, monkeypatch) -> None:
    async def reject(*, request_input, current_user, db):
        raise HTTPException(status_code=404, detail="智能体不存在")

    monkeypatch.setattr(agent_request_service, "submit_agent_request", reject)
    with pytest.raises(ToolException) as info:
        await internal_write.yuxi_research.coroutine(runtime=_runtime(), question="q")
    assert "YUXI_NOT_FOUND" in str(info.value)


async def test_research_question_truncation(research_env) -> None:
    research_env.poll_plan.append({"status": "completed", "output": "ok"})
    await internal_write.yuxi_research.coroutine(runtime=_runtime(), question="长" * 3000)
    assert len(research_env.submits[0]["request_id"]) <= 64
