"""ChatbotAgent 沙盒文件/命令工具开关（enable_filesystem_tools）。

背景：deepagents 文件系统中间件无条件捆绑 execute 等沙盒工具，卖家对话中
模型误用 execute 撞审批闸（2026-10-08 实测）。开关默认 True 保持工程场景
行为不变；卖家助手预设置 False。
"""

from __future__ import annotations

from types import SimpleNamespace

from deepagents.middleware.filesystem import FilesystemMiddleware

from yuxi.agents.buildin.chatbot.graph import _build_middlewares
from yuxi.agents.backends.composite import YuxiFilesystemMiddleware


def _context(**overrides) -> SimpleNamespace:
    base = {"tool_approval_mode": "default", "workdir_relative_path": "projects/x", "model_retry_times": 2, "model": "minimax:MiniMax-M2", "thread_id": "t1", "uid": "u1"}
    base.update(overrides)
    return SimpleNamespace(**base)


async def test_default_keeps_filesystem_middleware(monkeypatch) -> None:
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr("yuxi.agents.buildin.chatbot.graph.create_memory_middleware", _noop)
    monkeypatch.setattr("yuxi.agents.buildin.chatbot.graph.create_subagent_task_middleware", _noop)
    monkeypatch.setattr("yuxi.agents.buildin.chatbot.graph.create_summary_middleware_from_context", lambda *a, **k: None)
    middlewares = await _build_middlewares(_context(), backend=object())
    assert any(isinstance(m, (FilesystemMiddleware, YuxiFilesystemMiddleware)) for m in middlewares)


async def test_disabled_drops_filesystem_middleware(monkeypatch) -> None:
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr("yuxi.agents.buildin.chatbot.graph.create_memory_middleware", _noop)
    monkeypatch.setattr("yuxi.agents.buildin.chatbot.graph.create_subagent_task_middleware", _noop)
    monkeypatch.setattr("yuxi.agents.buildin.chatbot.graph.create_summary_middleware_from_context", lambda *a, **k: None)
    middlewares = await _build_middlewares(_context(enable_filesystem_tools=False), backend=object())
    assert not any(isinstance(m, (FilesystemMiddleware, YuxiFilesystemMiddleware)) for m in middlewares)
