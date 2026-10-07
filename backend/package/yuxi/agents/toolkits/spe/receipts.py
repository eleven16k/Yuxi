"""写回执层（契约 C-26 的 Python 内化）。

120 秒去重窗（锚点=回执落缓存时刻）、opKey = sha256(uid + tool + 稳定串)、
operation_id 形如 op_{tool}_{seq}（进程内递增，duplicate 回吐首执同一 ID——
同一操作不是新操作）、duplicate 在 summary 追加逐字句、失败不缓存（异常冒泡，
重试真正重跑）、缓存以进程生命周期为界。多用户适配：uid 进 opKey（同参异
用户不共享去重窗，与桥 kb_upload 的 kbId-进键适配同理）。在飞去重：同 opKey
并发调用共享一次执行。
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

WINDOW_MS = 120_000
DUPLICATE_NOTE = "（120 秒内重复请求，未再次执行后端写）"


def _stable_stringify(value: Any) -> str:
    """对象键升序、数组保序、原始值 JSON 序列化——同内容异键序必产生同串。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def operation_key(uid: str, tool: str, args: Any) -> str:
    """uid 必进键：同参数不同用户是不同操作。"""
    digest = hashlib.sha256(f"{uid}\n{tool}\n{_stable_stringify(args)}".encode()).hexdigest()
    return f"{tool}:{digest[:16]}"


class WriteReceipts:
    """进程级写回执登记簿（worker 生命周期为界）。"""

    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._inflight: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._seq = itertools.count(1)
        self._now = now

    async def exec_tracked(
        self,
        *,
        uid: str,
        tool: str,
        args: Any,
        run: Callable[[], Awaitable[tuple[str, dict[str, Any]]]],
    ) -> dict[str, Any]:
        """执行写操作并登记回执；窗口内同键重复请求回吐首执回执。

        run 返回 (summary, details)；失败异常冒泡且不落缓存。
        """
        key = operation_key(uid, tool, args)
        hit = self._cache.get(key)
        if hit is not None and (self._now() - hit[0]) * 1000 < WINDOW_MS:
            receipt = hit[1]
            return {**receipt, "summary": receipt["summary"] + DUPLICATE_NOTE, "duplicate": True}

        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._execute(key, tool, run))
            self._inflight[key] = task
            task.add_done_callback(lambda _t, k=key: self._inflight.pop(k, None))
        return await task

    async def _execute(
        self, key: str, tool: str, run: Callable[[], Awaitable[tuple[str, dict[str, Any]]]]
    ) -> dict[str, Any]:
        summary, details = await run()  # 失败冒泡，不落缓存
        receipt = {
            "operationId": f"op_{tool}_{next(self._seq)}",
            "summary": summary,
            "details": details,
            "duplicate": False,
        }
        self._cache[key] = (self._now(), receipt)
        return receipt


# 进程级单例（对齐桥 WriteReceipts 进程单例）
write_receipts = WriteReceipts()
