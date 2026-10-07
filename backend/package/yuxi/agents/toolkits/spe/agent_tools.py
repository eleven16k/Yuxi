"""SPE agent-session 通道执行层：catalog 6 工具的会话管线与注册。

契约 C-14：ensure(uid 会话键) → 统一 POST 端点（X-Agent-Token + sessionId）；
会话失效（SESSION_NOT_FOUND/SESSION_NOT_ACTIVE）条件失效后重建并**仅重试一次**；
其余错误不重试。成功直接透传后端 UiBlock 信封（blockId/ui/v/data 由 SPE 后端
拥有）。计费与预算闸在后端（TOOL_BUDGET_EXCEEDED 带内透传），客户端不设闸。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import ToolException
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import Field, create_model

from yuxi.agents.toolkits.registry import tool
from yuxi.agents.toolkits.spe.catalog import SPE_AGENT_SESSION_TOOLS
from yuxi.agents.toolkits.spe.client import SpeToolError, call_spe_tool, create_spe_session
from yuxi.agents.toolkits.spe.errors import format_spe_failure
from yuxi.agents.toolkits.spe.sessions import spe_session_registry
from yuxi.config.options import spe_agent_opts
from yuxi.repositories import spe_credential_repository

_TYPES = {"string": str, "number": float, "integer": int, "array": list, "object": dict}

# 会话失效码（触发条件失效 + 重建 + 仅重试一次）；UPSTREAM_SESSION_EXPIRED 是
# Ozon 上游会话概念，不触发本层重建。
SESSION_ERROR_CODES = frozenset({"SESSION_NOT_FOUND", "SESSION_NOT_ACTIVE"})


async def call_with_session_retry(
    *,
    tool_name: str,
    args: dict[str, Any],
    base_url: str,
    service_token: str,
    jwt: str,
    uid: str,
) -> dict[str, Any]:
    """ensure 会话 → 调用；会话失效则条件失效、重建后重试一次，且仅此一次。"""

    async def _create(key: str) -> str:
        return await create_spe_session(base_url=base_url, user_jwt=jwt, title=f"yuxi {key[:8]}")

    session_id = await spe_session_registry.ensure(uid, _create)
    try:
        return await call_spe_tool(
            base_url=base_url,
            service_token=service_token,
            session_id=session_id,
            tool_name=tool_name,
            args=args,
        )
    except SpeToolError as e:
        if e.code not in SESSION_ERROR_CODES:
            raise
    # 未删成说明并发调用已重建，ensure 直接命中新会话，天然正确
    spe_session_registry.invalidate_if_current(uid, session_id)
    rebuilt = await spe_session_registry.ensure(uid, _create)
    return await call_spe_tool(
        base_url=base_url,
        service_token=service_token,
        session_id=rebuilt,
        tool_name=tool_name,
        args=args,
    )


def _input_model(spec) -> type:
    """按 catalog 参数面生成输入模型；无参数工具补 dummy 占位。"""
    fields: dict[str, Any] = {}
    for param in spec.params:
        annotation = _TYPES[param["type"]]
        if param["required"]:
            fields[param["name"]] = (annotation, Field(description=param["description"]))
        else:
            fields[param["name"]] = (annotation | None, Field(default=None, description=param["description"]))
    if not fields:
        fields["dummy"] = (str | None, Field(default="", description="占位参数，忽略"))
    return create_model(f"{spec.name}_input", **fields)


def _make_agent_tool(spec):
    model = _input_model(spec)

    async def _impl(runtime: ToolRuntime, **params: Any) -> dict[str, Any]:
        uid = getattr(runtime.context, "uid", None) or ""
        if not uid:
            return {"ok": False, "error": "无法获取当前用户身份"}
        jwt = await spe_credential_repository.get_user_jwt(uid)
        if not jwt:
            raise ToolException(format_spe_failure("BIZ_AUTH_FAILED", "未登录 SPE：请先用 login 工具登录"))
        config = await spe_agent_opts.get()
        base_url = (config.get("base_url") or "").strip()
        service_token = (config.get("service_token") or "").strip()
        if not base_url or not service_token:
            raise ToolException(
                format_spe_failure(
                    "SPE_NOT_CONFIGURED", "SPE 未配置：请管理员设置 spe_agent_opts 的 base_url 与 service_token"
                )
            )
        try:
            return await call_with_session_retry(
                tool_name=spec.name,
                args=params,
                base_url=base_url,
                service_token=service_token,
                jwt=jwt,
                uid=uid,
            )
        except SpeToolError as e:
            raise ToolException(format_spe_failure(e.code, e.message)) from e

    return tool(
        category="spe",
        tags=["SPE 运营"],
        display_name=spec.name,
        name_or_callable=spec.name,
        description=spec.description,
        args_schema=model,
    )(_impl)


# 模块导入即注册（与 biz 工具同机制）
SPE_AGENT_TOOL_NAMES: list[str] = []
for _spec in SPE_AGENT_SESSION_TOOLS:
    globals()[_spec.name] = _make_agent_tool(_spec)
    SPE_AGENT_TOOL_NAMES.append(_spec.name)
