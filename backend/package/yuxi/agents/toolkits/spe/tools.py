"""SPE 工具面：本地工具（spe_ping / login / logout）。

身份架构（认证决策定案）：login 是用户级 SPE JWT 的唯一写入口（用户主动
提供凭证即确认，不进写闸门——桥侧既定语义）；其余工具经
runtime.context.uid 每次调用解析该用户凭据，未配置/过期 fail-closed。
会话键固定为 uid，登录/退出都会使注册表失效以重建 SPE 会话。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import ToolException
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import BaseModel, Field

from yuxi.agents.toolkits.registry import tool
from yuxi.agents.toolkits.spe.client import SpeToolError, call_spe_login
from yuxi.agents.toolkits.spe.errors import format_spe_failure
from yuxi.agents.toolkits.spe.sessions import spe_session_registry
from yuxi.config.options import spe_agent_opts
from yuxi.repositories import spe_credential_repository


def _uid_of(runtime: ToolRuntime) -> str:
    """从运行上下文取当前用户 uid。"""
    return getattr(runtime.context, "uid", None) or ""


async def _spe_base_url() -> str:
    """解析系统配置中的 SPE 后端地址；未配置时显式失败。"""
    config = await spe_agent_opts.get()
    base_url = (config.get("base_url") or "").strip()
    if not base_url:
        raise ToolException(
            format_spe_failure("SPE_NOT_CONFIGURED", "SPE 未配置：请管理员在系统配置（spe_agent_opts）设置 base_url")
        )
    return base_url


class SpePingInput(BaseModel):
    """连通性自检输入模型"""

    dummy: str = Field(default="", description="占位参数，忽略")


@tool(category="spe", tags=["SPE 运营"], display_name="SPE 连通性自检", args_schema=SpePingInput)
async def spe_ping(dummy: str, runtime: ToolRuntime) -> dict[str, Any]:
    """SPE 工具面连通性自检：返回 ok（无信封，纯 JSON）。"""
    return {"ok": True}


class SpeLoginInput(BaseModel):
    """SPE 登录输入模型"""

    username: str = Field(description="SPE 用户名")
    password: str = Field(description="SPE 密码，只在本次调用中使用，不会存储")


@tool(category="spe", tags=["SPE 运营"], display_name="SPE 登录", args_schema=SpeLoginInput)
async def login(username: str, password: str, runtime: ToolRuntime) -> dict[str, Any]:
    """登录 SPE 平台（输入用户名和密码）。登录后 JWT 按当前用户存储，后续所有工具用该身份。

    安全注意：密码只在本次调用中用于登录，不会存储。
    """
    uid = _uid_of(runtime)
    if not uid:
        return {"ok": False, "error": "无法获取当前用户身份"}
    if not username.strip() or not password:
        raise ToolException(format_spe_failure("INVALID_ARGS", "login 参数 username 与 password 必填（非空字符串）"))

    base_url = await _spe_base_url()
    try:
        result = await call_spe_login(
            base_url=base_url,
            username=username.strip(),
            password=password,
            bearer=await spe_credential_repository.get_user_jwt(uid),
        )
    except SpeToolError as e:
        raise ToolException(format_spe_failure(e.code, e.message)) from e

    await spe_credential_repository.upsert_user_jwt(uid=uid, user_jwt=result["token"])
    spe_session_registry.invalidate(uid)
    return {
        "ok": True,
        "data": {
            "success": True,
            "role": result.get("role"),
            "tenantId": result.get("tenantId"),
            "username": result.get("username"),
            "message": "登录成功",
        },
    }


class SpeLogoutInput(BaseModel):
    """SPE 退出登录输入模型"""

    dummy: str = Field(default="", description="占位参数，忽略")


@tool(category="spe", tags=["SPE 运营"], display_name="SPE 退出登录", args_schema=SpeLogoutInput)
async def logout(dummy: str, runtime: ToolRuntime) -> dict[str, Any]:
    """退出登录：清除当前用户存储的 SPE JWT，后续工具不再以该身份出站。"""
    uid = _uid_of(runtime)
    if not uid:
        return {"ok": False, "error": "无法获取当前用户身份"}
    await spe_credential_repository.delete_user_jwt(uid)
    spe_session_registry.invalidate(uid)
    return {"ok": True, "data": {"success": True, "message": "已退出登录"}}
