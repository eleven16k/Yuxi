"""SPE 会话注册表（契约 C-12 的 Python 内化）。

uid/会话键 → SPE 会话 id 的进程内注册表：ensure 命中即复用，未命中经注入的
create 可调用创建并缓存；同键并发 ensure 共享一次创建；失败不污染缓存；
invalidateIfCurrent 条件失效防并发误删。纯内存、无持久化，进程退出即失，
下次调用自然重建——与桥 sessions.ts 语义一致。会话键在工具层固定为 Yuxi uid。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

SpeSessionCreate = Callable[[str], Awaitable[str]]


class SpeSessionRegistry:
    """会话键 → SPE 会话 id 的进程内注册表。"""

    def __init__(self) -> None:
        self._sessions: dict[str, asyncio.Task[str]] = {}
        self._resolved: dict[str, str] = {}

    async def ensure(self, key: str, create: Callable[[str], Awaitable[str]]) -> str:
        """返回键对应的会话 id；未命中则创建（并发共享同一创建任务）。"""
        task = self._sessions.get(key)
        if task is None:
            task = asyncio.ensure_future(create(key))
            self._sessions[key] = task
            task.add_done_callback(lambda t, k=key: self._on_done(k, t))
        return await task

    def invalidate(self, key: str) -> None:
        """使键失效：下次 ensure 重新创建。"""
        self._sessions.pop(key, None)
        self._resolved.pop(key, None)

    def invalidate_if_current(self, key: str, session_id: str) -> bool:
        """仅当键当前已缓存的会话 id 与传入一致时才失效；不匹配不动。"""
        if self._resolved.get(key) != session_id:
            return False
        self._sessions.pop(key, None)
        self._resolved.pop(key, None)
        return True

    def _on_done(self, key: str, task: asyncio.Task[str]) -> None:
        # 失败路径清缓存（键已被取代则不动）；成功记录 resolved id 供条件失效比对
        if task.exception() is not None:
            if self._sessions.get(key) is task:
                self._sessions.pop(key, None)
                self._resolved.pop(key, None)
            return
        if self._sessions.get(key) is task:
            self._resolved[key] = task.result()


# 进程级单例：worker 内全部 spe 工具共享（对齐桥进程级唯一 SessionRegistry）
spe_session_registry = SpeSessionRegistry()
