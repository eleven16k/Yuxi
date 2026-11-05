"""Run 视图与 dashboard 积分回显（agent-compute-credit 卡2 Task 2.5）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.services import agent_run_service as ars
from yuxi.services.dashboard_service import DashboardService
from yuxi.storage.postgres.models_business import AgentRun, Base, Conversation


def _run_namespace(**overrides):
    base = dict(
        status="completed",
        agent_slug="translator",
        conversation_thread_id="thread-1",
        conversation_id=None,
        id="r1",
        request_id="req-1",
        langfuse_trace_id=None,
        token_usage={"total": {"total_tokens": 100}},
        error_type=None,
        error_message=None,
        credits_li=315,
        created_at=None,
        started_at=None,
        prepared_at=None,
        first_output_at=None,
        finished_at=None,
        first_model_request_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_run_result_includes_credits_li(monkeypatch):
    class _Repo:
        def __init__(self, db):
            del db

        async def get_run_for_user(self, run_id, uid):
            return _run_namespace()

    monkeypatch.setattr(ars, "AgentRunRepository", _Repo)
    payload = await ars.get_agent_run_result(run_id="r1", current_uid="u1", db=None)
    assert payload["credits_li"] == 315


def test_agent_run_to_dict_includes_credits_li():
    """Web 端 /api/agent/runs/{id} 走 run.to_dict()，精确值必须透出。"""
    run = AgentRun(id="r1", credits_li=315)
    assert run.to_dict()["credits_li"] == 315


@pytest_asyncio.fixture()
async def credits_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add(
            Conversation(
                thread_id="thread-credits",
                uid="uid-alice",
                agent_id="translator",
                project_id="11111111-1111-4111-8111-111111111111",
                is_pinned=False,
            )
        )
        db.add(
            Conversation(
                thread_id="thread-no-credits",
                uid="uid-alice",
                agent_id="translator",
                project_id="11111111-1111-4111-8111-111111111111",
                is_pinned=False,
            )
        )
        await db.commit()

        def _agent_run(index: int, thread_id: str, **overrides) -> AgentRun:
            base = dict(
                id=f"run-{thread_id}-{index}",
                conversation_id=None,
                conversation_thread_id=thread_id,
                runtime_scope_id=thread_id,
                agent_slug="translator",
                uid="uid-alice",
                status="completed",
                request_id=f"request-{thread_id}-{index}",
                token_usage={"total": {"total_tokens": 10}, "complete": True, "usage_reported_call_count": 1},
            )
            base.update(overrides)
            return AgentRun(**base)

        credits_conversation = (
            await db.execute(select(Conversation).where(Conversation.thread_id == "thread-credits"))
        ).scalar_one()
        no_credits_conversation = (
            await db.execute(select(Conversation).where(Conversation.thread_id == "thread-no-credits"))
        ).scalar_one()

        db.add(_agent_run(0, "thread-credits", conversation_id=credits_conversation.id, credits_li=315))
        db.add(_agent_run(1, "thread-credits", conversation_id=credits_conversation.id, credits_li=100))
        db.add(_agent_run(0, "thread-no-credits", conversation_id=no_credits_conversation.id))
        await db.commit()
        yield db
    await engine.dispose()


@pytest.mark.asyncio
async def test_list_conversations_aggregates_credits_li(credits_db):
    service = DashboardService(credits_db)
    items = (await service.list_conversations(search="thread-credits"))["items"]
    assert items[0]["credits_li"] == 415


@pytest.mark.asyncio
async def test_list_conversations_credits_li_defaults_to_zero(credits_db):
    service = DashboardService(credits_db)
    items = (await service.list_conversations(search="thread-no-credits"))["items"]
    assert items[0]["credits_li"] == 0
