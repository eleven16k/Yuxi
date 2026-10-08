"""内化写工具：kb_upload / yuxi_research（原桥 yuxi 通道 HTTP 外呼）。

kb_upload：知识上传五步链（上传→登记→解析→轮询→入库），upload 步走
`services/knowledge_file_service`（路由同源）；授权按 2026-10-08 spec §7——
对目标库有 MANAGE 即可（角色门移除）；不可见=不存在零探测保持；opKey 用
内容 sha256 前 16 位而非全文，kbId 必进键；同内容去重映射 YUXI_DUPLICATE_CONTENT；
解析轮询超时不报错，回执注明索引未触发。

yuxi_research：直调 `submit_agent_request`（scheduled_agent 先例），request_id
由问题哈希稳定派生（同题重放归并同一 run）；轮询 `get_agent_run_result` 至
终态，240s 预算；超时/失败以带 run_id 的结构化错误引导，不落幂等缓存。
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import os
import time
from typing import Any

# 本模块经 toolkits/__init__ 在 agents 包初始化早期装载：services 与持久化
# 依赖（会拉起 buildin 图成环）一律函数内惰性导入（kbs toolkit 同款纪律）。
from langchain_core.tools import ToolException
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import Field, create_model

from yuxi.agents.toolkits.registry import tool
from yuxi.agents.toolkits.spe.catalog import SPE_INTERNAL_WRITE_TOOLS
from yuxi.agents.toolkits.spe.client import SpeToolError
from yuxi.agents.toolkits.spe.errors import format_spe_failure
from yuxi.agents.toolkits.spe.kb_tools import visible_kb_summaries
from yuxi.agents.toolkits.spe.receipts import write_receipts

RESEARCH_AGENT_SOURCE = "spe_agent"
RESEARCH_AGENT_SLUG = "deep-research"
RESEARCH_TERMINAL_FAILED = frozenset({"failed", "cancelled", "interrupted"})

_KB_UPLOAD_MAX_CONTENT_CHARS = 1_000_000
_POLL_INTERVAL_SECONDS = 0.5
_POLL_DEADLINE_SECONDS = float(os.getenv("SPE_KB_UPLOAD_POLL_DEADLINE_MS", "15000")) / 1000
_RESEARCH_DEADLINE_SECONDS = float(os.getenv("SPE_YUXI_RESEARCH_DEADLINE_MS", "240000")) / 1000
_RESEARCH_INTERVAL_SECONDS = float(os.getenv("SPE_YUXI_RESEARCH_POLL_INTERVAL_MS", "3000")) / 1000


def _kb():
    from yuxi.knowledge.runtime import knowledge_base

    return knowledge_base


def _content_hash16(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _research_request_id(question: str) -> str:
    digest = hashlib.sha256(question.encode()).hexdigest()[:16]
    return f"spe-research-{digest}"


_block_seq = itertools.count(1)


def _envelope(ui: str, data: dict[str, Any]) -> dict[str, Any]:
    block_id = f"blk_{next(_block_seq)}"
    return {"blockId": block_id, "ui": ui, "v": 1, "data": data}


async def _kb_upload_impl(runtime: ToolRuntime, **params: Any) -> dict[str, Any]:
    uid = getattr(runtime.context, "uid", None) or ""
    if not uid:
        return {"ok": False, "error": "无法获取当前用户身份"}

    kb_id = params.get("kb_id")
    filename = params.get("filename")
    content = params.get("content")
    if not isinstance(kb_id, str) or not kb_id.strip():
        raise ToolException(format_spe_failure("INVALID_ARGS", "kb_upload 参数 kb_id 必填（非空字符串）"))
    kb_id = kb_id.strip()
    if (
        not isinstance(filename, str)
        or not filename
        or len(filename) > 200
        or "/" in filename
        or "\\" in filename
        or not (filename.endswith(".md") or filename.endswith(".txt"))
    ):
        raise ToolException(
            format_spe_failure(
                "INVALID_ARGS", "kb_upload 参数 filename 必填（.md/.txt 结尾、不含路径分隔符、≤200 字符）"
            )
        )
    if not isinstance(content, str) or not content:
        raise ToolException(format_spe_failure("INVALID_ARGS", "kb_upload 参数 content 必填（非空 markdown 文本）"))
    if len(content) > _KB_UPLOAD_MAX_CONTENT_CHARS:
        raise ToolException(format_spe_failure("INVALID_ARGS", "kb_upload 参数 content 超过 1MB（1,000,000 字符）上限"))

    summaries = await visible_kb_summaries(runtime)
    target = next((s for s in summaries if s["kb_id"] == kb_id), None)
    if target is None:
        raise ToolException(format_spe_failure("YUXI_NOT_FOUND", f"知识库 '{kb_id}' 不存在或当前身份无权访问"))
    if not target.get("can_manage"):
        raise ToolException(format_spe_failure("BIZ_FORBIDDEN", f"对知识库 '{target['kb_name']}' 没有管理权限"))

    from yuxi.services.knowledge_quota_service import TenantKBQuotaExceededError, ensure_tenant_kb_quota

    try:
        await ensure_tenant_kb_quota(kb_id, incoming_count=1, incoming_bytes=len(content.encode("utf-8")))
    except TenantKBQuotaExceededError as e:
        raise ToolException(format_spe_failure("BIZ_FORBIDDEN", str(e))) from e

    content_hash = _content_hash16(content)
    # opKey 用内容哈希前 16 位而非全文；kbId 必进键（同内容异库是不同操作）
    receipt_args = {"kbId": kb_id, "filename": filename, "contentHash": content_hash}

    from yuxi.services.knowledge_file_service import KnowledgeFileDuplicateError, upload_knowledge_file_bytes
    from yuxi.services.task_service import tasker

    async def _run() -> tuple[str, dict[str, Any]]:
        file_bytes = content.encode("utf-8")
        try:
            uploaded = await upload_knowledge_file_bytes(kb_id=kb_id, filename=filename, file_bytes=file_bytes)
        except KnowledgeFileDuplicateError as e:
            raise SpeToolError(
                "YUXI_DUPLICATE_CONTENT",
                f"该文件内容已存在，无需重复上传——用 kb_list/yuxi_kb_search 确认；确需重传先删除或修改（{e}）",
            ) from e

        minio_path = uploaded["minio_path"]
        added = await _kb().add_file_record(
            kb_id, minio_path, {"content_hashes": {minio_path: uploaded["content_hash"]}}, operator_id=uid
        )
        file_id = _first_success_file_id(added)
        if file_id is None:
            raise SpeToolError("TOOL_UPSTREAM_ERROR", "知识文件登记失败（无成功条目）")

        parse_task = await tasker.enqueue(
            name=f"知识上传解析 ({target['kb_name']})",
            task_type="knowledge_parse",
            payload={"kb_id": kb_id, "file_ids": [file_id], "params": {}, "operator_id": uid},
        )
        task_ids = [parse_task.id]

        parsed, status = await _wait_parsed(kb_id, file_id)
        if parsed:
            index_task = await tasker.enqueue(
                name=f"知识上传入库 ({target['kb_name']})",
                task_type="knowledge_index",
                payload={"kb_id": kb_id, "file_ids": [file_id], "params": {}, "operator_id": uid},
            )
            task_ids.append(index_task.id)
            summary = f"已上传 {filename} 到 {target['kb_name']}：解析与索引已排队"
        else:
            summary = (
                f"已上传 {filename} 到 {target['kb_name']}：解析仍在进行（当前 {status}），索引未触发——"
                "稍后用 kb_list 确认状态；如需完成索引请在知识后台处理或删除后重传"
            )
        return summary, {"kbId": kb_id, "fileId": file_id, "filename": filename, "taskIds": task_ids}

    receipt = await write_receipts.exec_tracked(uid=uid, tool="kb_upload", args=receipt_args, run=_run)
    return _envelope(
        "confirm-result",
        {
            "success": True,
            "summary": receipt["summary"],
            "details": receipt["details"],
            "operationId": receipt["operationId"],
            "duplicate": receipt["duplicate"],
        },
    )


def _first_success_file_id(added: Any) -> str | None:
    items = added.get("items") if isinstance(added, dict) else None
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("status") == "success" and item.get("file_id"):
                return str(item["file_id"])
    return None


async def _wait_parsed(kb_id: str, file_id: str) -> tuple[bool, str | None]:
    """轮询至 parsed；超时返回 (False, 最后状态)——不报错，由回执给真话。"""
    deadline = time.monotonic() + _POLL_DEADLINE_SECONDS
    status: str | None = None
    while time.monotonic() < deadline:
        listing = await _kb().search_document_files([{"kb_id": kb_id, "name": kb_id}], offset=0, limit=500)
        meta = next((f for f in listing.get("files", []) if f.get("file_id") == file_id), None)
        status = meta.get("status") if meta else None
        if status == "parsed":
            return True, status
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    return False, status


async def _yuxi_research_impl(runtime: ToolRuntime, **params: Any) -> dict[str, Any]:
    uid = getattr(runtime.context, "uid", None) or ""
    if not uid:
        return {"ok": False, "error": "无法获取当前用户身份"}
    question = params.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ToolException(format_spe_failure("INVALID_ARGS", "question 必填（非空）"))
    question = question[:2000]

    from fastapi import HTTPException
    from sqlalchemy import select

    from yuxi.services.agent_request_service import AgentRequestInput, RunOrigin, submit_agent_request
    from yuxi.services.agent_run_service import get_agent_run_result
    from yuxi.services.input_message_service import build_chat_input_message
    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.utils.hash_utils import hash_id
    from yuxi.storage.postgres.models_business import User

    async def _run() -> tuple[str, dict[str, Any]]:
        async with pg_manager.get_async_session_context() as db:
            user = await db.scalar(select(User).where(User.uid == uid, User.is_deleted == 0))
            if user is None:
                raise SpeToolError("BIZ_AUTH_FAILED", "运行用户不存在或已删除")
            try:
                request_id = _research_request_id(question)
                # agent-call 同款缺省：uid:slug:request_id 稳定派生线程（同题归并）
                thread_id = hash_id("invocation_", f"{uid}:{RESEARCH_AGENT_SLUG}:{request_id}", 64)
                view = await submit_agent_request(
                    request_input=AgentRequestInput(
                        agent_slug=RESEARCH_AGENT_SLUG,
                        thread_id=thread_id,
                        request_id=request_id,
                        input_message=build_chat_input_message(question),
                        origin=RunOrigin(source=RESEARCH_AGENT_SOURCE, channel="worker"),
                        queue_policy="enqueue",
                        create_conversation=True,
                    ),
                    current_user=user,
                    db=db,
                )
            except HTTPException as e:
                raise SpeToolError("YUXI_NOT_FOUND", f"研究智能体不可用（{e.detail}）") from e

            run_id = view.get("run_id") or view.get("agent_run_id") or ""
            if not run_id:
                raise SpeToolError("TOOL_UPSTREAM_ERROR", "研究任务提交响应缺少 run_id")

            deadline = time.monotonic() + _RESEARCH_DEADLINE_SECONDS
            while True:
                result = await get_agent_run_result(run_id=run_id, current_uid=uid, db=db)
                status = result.get("status")
                if status == "completed":
                    output = result.get("output") or ""
                    summary = (
                        output
                        if len(output) <= 500
                        else (
                            output[:500] + f"…（研究全文 {len(output)} 字符已截断，完整结果可在知识后台凭 run_id 查看）"
                        )
                    )
                    details = {
                        "run_id": run_id,
                        "output_chars": len(output),
                        "usage": result.get("token_usage"),
                    }
                    if result.get("thread_id"):
                        details["thread_id"] = result["thread_id"]
                    return summary, details
                if status in RESEARCH_TERMINAL_FAILED:
                    error = result.get("error") or {}
                    message = error.get("message") if isinstance(error, dict) else str(error)
                    raise SpeToolError(
                        "YUXI_RESEARCH_FAILED",
                        f"深度研究运行失败（{status}，run_id={run_id}）：{message or '未知原因'}；不要原样重试",
                    )
                if time.monotonic() >= deadline:
                    raise SpeToolError(
                        "YUXI_RESEARCH_TIMEOUT",
                        f"深度研究超时未终态（run_id={run_id}）：任务仍在后台运行——同题再调会续取同一 run（不重复耗算力），不要立即以相同问题高频重发",  # noqa: E501
                    )
                await asyncio.sleep(_RESEARCH_INTERVAL_SECONDS)

    receipt = await write_receipts.exec_tracked(uid=uid, tool="yuxi_research", args={"question": question}, run=_run)
    return _envelope(
        "confirm-result",
        {
            "success": True,
            "summary": receipt["summary"],
            "details": receipt["details"],
            "operationId": receipt["operationId"],
            "duplicate": receipt["duplicate"],
        },
    )


_IMPLS = {"kb_upload": _kb_upload_impl, "yuxi_research": _yuxi_research_impl}


def _input_model(spec) -> type:
    fields: dict[str, Any] = {}
    for param in spec.params:
        annotation = {"string": str, "number": float, "integer": int, "array": list, "object": dict}[param["type"]]
        if param["required"]:
            fields[param["name"]] = (annotation, Field(description=param["description"]))
        else:
            fields[param["name"]] = (annotation | None, Field(default=None, description=param["description"]))
    return create_model(f"{spec.name}_input", **fields)


def _make_internal_write_tool(spec):
    impl = _IMPLS[spec.name]
    model = _input_model(spec)

    async def _wrapped(runtime: ToolRuntime, **params: Any) -> dict[str, Any]:
        try:
            return await impl(runtime, **params)
        except SpeToolError as e:
            raise ToolException(format_spe_failure(e.code, e.message)) from e

    return tool(
        category="spe",
        tags=["SPE 运营"],
        display_name=spec.name,
        name_or_callable=spec.name,
        description=spec.description,
        args_schema=model,
    )(_wrapped)


SPE_INTERNAL_WRITE_TOOL_NAMES: list[str] = []
for _spec in SPE_INTERNAL_WRITE_TOOLS:
    globals()[_spec.name] = _make_internal_write_tool(_spec)
    SPE_INTERNAL_WRITE_TOOL_NAMES.append(_spec.name)
