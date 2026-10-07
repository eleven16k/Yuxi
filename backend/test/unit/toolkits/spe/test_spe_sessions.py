"""sessions.py 会话注册表测试（C-12 语义：共享创建、失败不污染、条件失效）。"""

import asyncio

import pytest

from yuxi.agents.toolkits.spe.sessions import SpeSessionRegistry


class _CountingCreate:
    """计数型 create：模拟一次慢创建，记录被调次数与键。"""

    def __init__(self, delay: float = 0.02) -> None:
        self.calls: list[str] = []
        self.delay = delay

    async def __call__(self, key: str) -> str:
        self.calls.append(key)
        await asyncio.sleep(self.delay)
        return f"sess-{key}"


class _FailingCreate:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, key: str) -> str:
        self.calls += 1
        raise RuntimeError("SPE 拒绝创建会话")


async def test_concurrent_ensure_shares_single_create() -> None:
    registry = SpeSessionRegistry()
    create = _CountingCreate()
    results = await asyncio.gather(*(registry.ensure("u1", create) for _ in range(5)))
    assert results == ["sess-u1"] * 5
    assert create.calls == ["u1"]


async def test_ensure_reuses_resolved_session() -> None:
    registry = SpeSessionRegistry()
    create = _CountingCreate()
    first = await registry.ensure("u1", create)
    second = await registry.ensure("u1", create)
    assert first == second == "sess-u1"
    assert len(create.calls) == 1


async def test_failure_reaches_all_callers_and_cache_stays_clean() -> None:
    registry = SpeSessionRegistry()
    create = _FailingCreate()
    results = await asyncio.gather(
        registry.ensure("u1", create),
        registry.ensure("u1", create),
        return_exceptions=True,
    )
    assert all(isinstance(r, RuntimeError) for r in results)
    assert create.calls == 1  # 并发共享同一失败创建

    # 失败不污染缓存：下次 ensure 真实重建
    recovering = _CountingCreate()
    session_id = await registry.ensure("u1", recovering)
    assert session_id == "sess-u1"
    assert recovering.calls == ["u1"]


async def test_invalidate_forces_recreate() -> None:
    registry = SpeSessionRegistry()
    create = _CountingCreate()
    await registry.ensure("u1", create)
    registry.invalidate("u1")
    again = await registry.ensure("u1", create)
    assert again == "sess-u1"
    assert create.calls == ["u1", "u1"]


async def test_invalidate_if_current_only_matches_resolved_id() -> None:
    registry = SpeSessionRegistry()
    create = _CountingCreate()
    session_id = await registry.ensure("u1", create)

    # 不匹配的 id 不失效（并发下先失败者不误删他人刚重建的会话）
    assert registry.invalidate_if_current("u1", "sess-other") is False
    assert registry.invalidate_if_current("missing", session_id) is False
    assert await registry.ensure("u1", create) == session_id
    assert create.calls == ["u1"]

    # 匹配当前 id 才失效，失效后重建
    assert registry.invalidate_if_current("u1", session_id) is True
    await registry.ensure("u1", create)
    assert create.calls == ["u1", "u1"]


async def test_in_flight_failure_then_success_isolation_between_keys() -> None:
    """键 A 的失败创建不影响键 B。"""
    registry = SpeSessionRegistry()
    failing = _FailingCreate()
    ok = _CountingCreate()
    with pytest.raises(RuntimeError):
        await registry.ensure("bad", failing)
    assert await registry.ensure("good", ok) == "sess-good"
