"""闸挂钩接线：submit 路径必须调用 ensure_run_allowed（agent-compute-credit 卡2）。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from yuxi.services import agent_request_service as ars
from yuxi.services.input_message_service import build_chat_input_message


class _RequestRepo:
    def __init__(self, db):
        del db

    async def get_by_request_id(self, request_id):
        del request_id
        return None

    async def list_queued(self, **kwargs):
        del kwargs
        return []


class _RunRepo:
    def __init__(self, db):
        del db

    async def get_run_by_request_id(self, request_id):
        del request_id
        return None

    async def get_active_run_by_thread_for_user(self, **kwargs):
        del kwargs
        return None


async def test_submit_path_invokes_credit_gate(monkeypatch):
    called = {}

    async def fake_gate(*, uid, role):
        called["args"] = (uid, role)
        raise HTTPException(status_code=403, detail={"code": "credit_insufficient"})

    monkeypatch.setattr(ars, "ensure_run_allowed", fake_gate)

    class AgentRepo:
        def __init__(self, db):
            del db

        async def get_visible_by_slug(self, *, slug, user, kind="main"):
            return SimpleNamespace(slug=slug, backend_id="ChatbotAgent")

    class ConvRepo:
        def __init__(self, db):
            del db

        async def get_conversation_by_thread_id(self, thread_id):
            return None

        async def add_conversation(self, **kwargs):
            return SimpleNamespace(
                id=1,
                thread_id=kwargs["thread_id"],
                project_id=kwargs["project_id"],
                uid=kwargs["uid"],
            )

    conversation = SimpleNamespace(
        id=1,
        thread_id="thread-1",
        project_id="11111111-1111-4111-8111-111111111111",
        uid="user-1",
        extra_metadata={},
    )

    async def fake_get_thread_conversation(**kwargs):
        return conversation

    binding = SimpleNamespace(
        uid="user-1",
        thread_id="thread-1",
        conversation_id=1,
        project_id=conversation.project_id,
        materialize_managed=False,
    )

    class Db:
        @asynccontextmanager
        async def begin_nested(self):
            yield

    monkeypatch.setattr(ars, "AgentRepository", AgentRepo)
    monkeypatch.setattr(ars, "AgentRunRequestRepository", _RequestRepo)
    monkeypatch.setattr(ars, "AgentRunRepository", _RunRepo)
    monkeypatch.setattr(ars, "ConversationRepository", ConvRepo)
    monkeypatch.setattr(ars, "get_thread_conversation", fake_get_thread_conversation)
    monkeypatch.setattr(
        ars,
        "create_implicit_project",
        _async_value(SimpleNamespace(id=conversation.project_id, uid="user-1")),
    )
    monkeypatch.setattr(
        ars,
        "resolve_conversation_workdir_binding",
        _async_value(binding),
    )
    monkeypatch.setattr(ars, "get_agent_backend", lambda backend_id: object())

    request_input = ars.AgentRequestInput(
        agent_slug="translator",
        thread_id="thread-1",
        request_id="req-gate-1",
        input_message=build_chat_input_message("hello"),
        origin=ars.RunOrigin(source="chat", channel="web"),
        create_conversation=True,
    )

    current_user = SimpleNamespace(uid="user-1", role="user")
    # 闸门 403 必须阻断提交（而非调用后被吞掉）
    with pytest.raises(HTTPException) as ei:
        await ars.submit_agent_request(request_input=request_input, current_user=current_user, db=Db())
    assert ei.value.status_code == 403
    # (uid, role) 来自 current_user
    assert called["args"] == ("user-1", "user")


def _async_value(value):
    async def _inner(*args, **kwargs):
        return value
    return _inner
