"""失败表重放测试（agent-compute-credit 卡2，spec §5.5 超管可重放）。

契约：
1. replay_failed_reports：读 credit_report_failed 每批 500 行（id 升序）→ 逐行用已存 payload
   调 _post_deduct；头批持续失败会阻塞后续批次（队头阻塞是管理端可人工干预的可接受权衡：
   清理/修复头批失败行后再重放）
2. 成功 → 删该行 + 回填 credits_li；失败 → 行保留（error 更新），继续下一行
3. 空表 → 返回 {"replayed": 0, "failed": 0}

mock 边界：_load_failed_reports/_drop_failed_report/_mark_failed_report_error 为持久化 helper，
_post_deduct 为出站边界；被测编排逻辑（增删、计数、回填）不 mock。
批次语义（order_by id + limit 500）由 test_load_failed_reports_batches_500_oldest_first
在 sqlite 上跑真实 _load_failed_reports 锁定。
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.services import spe_credit_service as svc
from yuxi.storage.postgres.models_business import Base, CreditReportFailed


def _failed_row(row_id: int, run_id: str, error: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=row_id,
        run_id=run_id,
        payload={
            "platform": "yuxi",
            "refId": f"agent-yuxi-run-{run_id}",
            "operationType": "agent_compute",
            "usage": [{"model": "m", "inputTokens": 10, "outputTokens": 1, "cacheReadTokens": 0}],
        },
        error=error,
    )


async def test_replay_empty_table_returns_zero(monkeypatch):
    monkeypatch.setattr(svc, "_load_failed_reports", _async_return([]))
    result = await svc.replay_failed_reports(uid="admin-1")
    assert result == {"replayed": 0, "failed": 0}


async def test_replay_success_deletes_row_and_backfills_credits(monkeypatch):
    rows = [_failed_row(1, "r1"), _failed_row(2, "r2")]
    monkeypatch.setattr(svc, "_load_failed_reports", _async_return(rows))
    monkeypatch.setattr(svc, "_run_owner_uid", _async_return("owner-alice"))
    deduct_calls = []
    monkeypatch.setattr(svc, "_post_deduct", _capture_post(deduct_calls))

    persisted, dropped, errors = [], [], []
    monkeypatch.setattr(svc, "_persist_run_credits", _capture(persisted))
    monkeypatch.setattr(svc, "_drop_failed_report", _capture(dropped))
    monkeypatch.setattr(svc, "_mark_failed_report_error", _capture(errors))

    result = await svc.replay_failed_reports(uid="admin-1")

    assert result == {"replayed": 2, "failed": 0}
    assert persisted == [("r1", 315), ("r2", 315)]
    assert [call[0].id for call in dropped] == [1, 2]
    assert errors == []
    # 计费归属：必须用 run 属主（原用户）的凭据重放，而不是操作重放的超管
    assert [call["uid"] for call in deduct_calls] == ["owner-alice", "owner-alice"]


async def test_replay_failure_keeps_row_and_updates_error(monkeypatch):
    rows = [_failed_row(7, "r7", error=None)]
    monkeypatch.setattr(svc, "_load_failed_reports", _async_return(rows))
    monkeypatch.setattr(svc, "_run_owner_uid", _async_return("owner-alice"))
    monkeypatch.setattr(svc, "_post_deduct", _async_return((None, "HTTP 503")))

    persisted, dropped, errors = [], [], []
    monkeypatch.setattr(svc, "_persist_run_credits", _capture(persisted))
    monkeypatch.setattr(svc, "_drop_failed_report", _capture(dropped))
    monkeypatch.setattr(svc, "_mark_failed_report_error", _capture(errors))

    result = await svc.replay_failed_reports(uid="admin-1")

    assert result == {"replayed": 0, "failed": 1}
    assert dropped == []  # 行保留
    assert persisted == []  # 未回填
    assert len(errors) == 1
    kept_row, error = errors[0]
    assert (kept_row.id, error) == (7, "HTTP 503")


async def test_replay_mixed_rows_continue_after_failure(monkeypatch):
    rows = [_failed_row(1, "r-bad"), _failed_row(2, "r-good")]
    monkeypatch.setattr(svc, "_load_failed_reports", _async_return(rows))
    monkeypatch.setattr(svc, "_run_owner_uid", _async_return("owner-bob"))

    outcomes = iter([(None, "HTTP 500"), (88, None)])

    async def fake_post_deduct(*, uid, payload):
        return next(outcomes)

    monkeypatch.setattr(svc, "_post_deduct", fake_post_deduct)

    persisted, dropped, errors = [], [], []
    monkeypatch.setattr(svc, "_persist_run_credits", _capture(persisted))
    monkeypatch.setattr(svc, "_drop_failed_report", _capture(dropped))
    monkeypatch.setattr(svc, "_mark_failed_report_error", _capture(errors))

    result = await svc.replay_failed_reports(uid="admin-1")

    assert result == {"replayed": 1, "failed": 1}
    assert persisted == [("r-good", 88)]
    assert [call[0].id for call in dropped] == [2]
    assert [call[0].id for call in errors] == [1]


async def test_replay_missing_run_keeps_row(monkeypatch):
    """run 已不存在 → 无法归属租户，保留行且不发起出站请求。"""
    rows = [_failed_row(9, "r-gone")]
    monkeypatch.setattr(svc, "_load_failed_reports", _async_return(rows))
    monkeypatch.setattr(svc, "_run_owner_uid", _async_return(None))
    deduct_calls = []
    monkeypatch.setattr(svc, "_post_deduct", _capture_post(deduct_calls))

    dropped, errors = [], []
    monkeypatch.setattr(svc, "_drop_failed_report", _capture(dropped))
    monkeypatch.setattr(svc, "_mark_failed_report_error", _capture(errors))

    result = await svc.replay_failed_reports(uid="admin-1")

    assert result == {"replayed": 0, "failed": 1}
    assert deduct_calls == []
    assert dropped == []
    assert len(errors) == 1


def _capture_post(seen: list):
    async def _inner(*, uid, payload):
        seen.append({"uid": uid, "payload": payload})
        return (315, None)

    return _inner


def _capture(seen: list):
    async def _inner(*args):
        seen.append(args)

    return _inner


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


@pytest_asyncio.fixture()
async def failed_table_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


@pytest.mark.asyncio
async def test_load_failed_reports_batches_500_oldest_first(failed_table_db, monkeypatch):
    """>500 行只取 id 升序头 500 行：order_by id + limit 语义锁定（防全表膨胀拖垮重放）。"""
    failed_table_db.add_all(CreditReportFailed(id=i, run_id=f"r{i}", payload={}, error=None) for i in range(1, 503))
    await failed_table_db.commit()

    @asynccontextmanager
    async def fake_session_context():
        yield failed_table_db

    monkeypatch.setattr(svc, "pg_manager", SimpleNamespace(get_async_session_context=fake_session_context))

    rows = await svc._load_failed_reports()

    assert len(rows) == 500
    assert [row.id for row in rows[:3]] == [1, 2, 3]  # id 升序
    assert rows[-1].id == 500  # 头批截止，501/502 留待下一批
