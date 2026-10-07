"""SPE 业务端点出站客户端（契约 C-11 / 会话创建的 Python 内化）。

桥 backend.ts 的等价实现：统一工具端点 `POST {base}/api/v1/agent/tools/{tool}`，
`X-Agent-Token` 承载服务令牌，body 携带 `sessionId` 与 `args`；会话创建端点
`POST {base}/api/v1/agent/sessions` 以用户 JWT 为 Bearer。全部失败路径归一为
带码 SpeToolError，错误信封的 code/message 原样透传（SPE 侧才是语义 Owner）。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from .errors import (
    SPE_SESSION_CREATE_CODE,
    SPE_TOOL_TIMEOUT_CODE,
    SPE_TOOL_UPSTREAM_CODE,
)

SPE_TOOL_TIMEOUT_SECONDS = 120.0
SPE_SESSION_TIMEOUT_SECONDS = 120.0


class SpeToolError(Exception):
    """SPE 出站调用失败的统一错误载体（code 定位类别，message 保留原因）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _base(base_url: str) -> str:
    return base_url.rstrip("/")


async def call_spe_biz_get(
    *,
    base_url: str,
    jwt: str,
    path: str,
    query: dict[str, str | int | float],
    timeout: float = SPE_SESSION_TIMEOUT_SECONDS,
) -> Any:
    """biz-get 通道（C-22）：GET + Bearer 用户 JWT，零会话零服务令牌。

    401/403/404 分别归一 BIZ_AUTH_FAILED / BIZ_FORBIDDEN / BIZ_NOT_FOUND；
    超时与网络失败同归 TOOL_UPSTREAM_ERROR（message 区分）；GET 侧 2xx
    body 必须是 JSON（无写通道的空 body 宽容）。
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            res = await client.get(f"{_base(base_url)}{path}", params=query, headers={"Authorization": f"Bearer {jwt}"})
        except httpx.TimeoutException as e:
            raise SpeToolError(SPE_TOOL_UPSTREAM_CODE, f"业务服务超时（{timeout}s）：{e}") from e
        except httpx.HTTPError as e:
            raise SpeToolError(SPE_TOOL_UPSTREAM_CODE, f"业务服务连接失败：{e}") from e

    if res.status_code == 401:
        raise SpeToolError("BIZ_AUTH_FAILED", "业务接口认证失败（HTTP 401）")
    if res.status_code == 403:
        raise SpeToolError("BIZ_FORBIDDEN", "业务接口拒绝访问（HTTP 403）")
    if res.status_code == 404:
        raise SpeToolError("BIZ_NOT_FOUND", "业务接口未找到（HTTP 404）")
    if res.is_error:
        raise SpeToolError(SPE_TOOL_UPSTREAM_CODE, f"业务服务返回异常状态（HTTP {res.status_code}）")

    try:
        return res.json()
    except ValueError as e:
        raise SpeToolError(SPE_TOOL_UPSTREAM_CODE, f"业务服务返回非 JSON（HTTP {res.status_code}）：{e}") from e


async def call_spe_biz_mutate(
    *,
    base_url: str,
    jwt: str,
    method: str,
    path: str,
    query: dict[str, str | int | float] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = SPE_SESSION_TIMEOUT_SECONDS,
) -> Any:
    """biz 写通道（C-26）：POST/PUT + Bearer 用户 JWT；2xx 空 body 宽容返回 None。

    400→BIZ_VALIDATION_FAILED（尽量带后端 error 文本）、402→BIZ_INSUFFICIENT_CREDIT
    （带后端 message）、401/403/404 与 GET 通道同码；超时不单列；timeout 可覆写
    （generate_listing 同步 AI 加工用 300s）。
    """
    headers = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            res = await client.request(
                method,
                f"{_base(base_url)}{path}",
                params=query or {},
                json=body,
                headers={"Authorization": f"Bearer {jwt}", **headers},
            )
        except httpx.TimeoutException as e:
            raise SpeToolError(SPE_TOOL_UPSTREAM_CODE, f"业务服务超时（{timeout}s）：{e}") from e
        except httpx.HTTPError as e:
            raise SpeToolError(SPE_TOOL_UPSTREAM_CODE, f"业务服务连接失败：{e}") from e

    try:
        parsed: Any = res.json()
    except ValueError:
        parsed = None
    # 错误分支只从 dict 视图取文案；list/scalar 成功响应（如 collect_products）不受影响
    payload = parsed if isinstance(parsed, dict) else {}

    if res.status_code == 400:
        error_text = payload.get("error")
        detail = f"：{error_text}" if isinstance(error_text, str) and error_text else ""
        raise SpeToolError("BIZ_VALIDATION_FAILED", f"业务接口参数校验失败（HTTP 400）{detail}")
    if res.status_code == 401:
        raise SpeToolError("BIZ_AUTH_FAILED", "业务接口认证失败（HTTP 401）")
    if res.status_code == 402:
        message = payload.get("message")
        raise SpeToolError(
            "BIZ_INSUFFICIENT_CREDIT",
            f"积分不足（HTTP 402）：{message if isinstance(message, str) and message else '未知原因'}",
        )
    if res.status_code == 403:
        raise SpeToolError("BIZ_FORBIDDEN", "业务接口拒绝访问（HTTP 403）")
    if res.status_code == 404:
        raise SpeToolError("BIZ_NOT_FOUND", "业务接口未找到（HTTP 404）")
    if res.is_error:
        raise SpeToolError(SPE_TOOL_UPSTREAM_CODE, f"业务服务返回异常状态（HTTP {res.status_code}）")

    if not (res.content or b"").strip():
        return None  # 写通道 2xx 空 body 宽容（GET 通道无此宽容）
    if parsed is None:
        raise SpeToolError(SPE_TOOL_UPSTREAM_CODE, f"业务服务返回非 JSON（HTTP {res.status_code}）")
    return parsed


async def call_spe_tool(
    *,
    base_url: str,
    service_token: str,
    session_id: str,
    tool_name: str,
    args: dict[str, Any] | None = None,
    timeout: float = SPE_TOOL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """调用 SPE 统一工具端点，成功返回 UiBlock（blockId/ui/v/data）。"""
    url = f"{_base(base_url)}/api/v1/agent/tools/{quote(tool_name, safe='')}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            res = await client.post(
                url,
                json={"sessionId": session_id, "args": args or {}},
                headers={"X-Agent-Token": service_token},
            )
        except httpx.TimeoutException as e:
            raise SpeToolError(SPE_TOOL_TIMEOUT_CODE, f"业务服务超时（{timeout}s）：{e}") from e
        except httpx.HTTPError as e:
            raise SpeToolError(SPE_TOOL_UPSTREAM_CODE, f"业务服务连接失败：{e}") from e

    try:
        envelope = res.json()
    except ValueError as e:
        raise SpeToolError(SPE_TOOL_UPSTREAM_CODE, f"业务服务返回非 JSON（HTTP {res.status_code}）：{e}") from e

    if envelope.get("ok") is True:
        data = envelope.get("data") or {}
        ui = data.get("ui")
        if not isinstance(ui, str) or not ui:
            raise SpeToolError(SPE_TOOL_UPSTREAM_CODE, "业务服务返回信封不合法（缺 data.ui）")
        block_id = data.get("blockId")
        version = data.get("v")
        return {
            "blockId": block_id if isinstance(block_id, str) else "",
            "ui": ui,
            "v": version if isinstance(version, int) and not isinstance(version, bool) else 1,
            "data": data.get("data"),
        }

    error = envelope.get("error") or {}
    code = error.get("code")
    message = error.get("message")
    raise SpeToolError(
        code if isinstance(code, str) else SPE_TOOL_UPSTREAM_CODE,
        message if isinstance(message, str) else "未知业务错误",
    )


def _session_id_of(body: dict[str, Any]) -> str:
    """id 提取顺序 sessionId → id → data.id（主字段为已核对的 AgentSessionView 形状）。"""
    for candidate in (body.get("sessionId"), body.get("id"), (body.get("data") or {}).get("id")):
        if isinstance(candidate, str) and candidate:
            return candidate
    return ""


async def create_spe_session(
    *,
    base_url: str,
    user_jwt: str,
    title: str,
    timeout: float = SPE_SESSION_TIMEOUT_SECONDS,
) -> str:
    """创建 SPE 会话，返回会话 id；全部失败归一为 SESSION_CREATE_FAILED。"""
    url = f"{_base(base_url)}/api/v1/agent/sessions"
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            res = await client.post(
                url,
                json={"title": title},
                headers={"Authorization": f"Bearer {user_jwt}"},
            )
        except httpx.TimeoutException as e:
            raise SpeToolError(SPE_SESSION_CREATE_CODE, f"创建会话超时（{timeout}s）：{e}") from e
        except httpx.HTTPError as e:
            raise SpeToolError(SPE_SESSION_CREATE_CODE, f"创建会话连接失败：{e}") from e

    try:
        body = res.json()
    except ValueError as e:
        raise SpeToolError(SPE_SESSION_CREATE_CODE, f"创建会话返回非 JSON（HTTP {res.status_code}）：{e}") from e

    # 信封优先：ok:false 时无论 HTTP 状态码如何，先取信封 error 作原始原因
    if body.get("ok") is False:
        error = body.get("error") or {}
        code = error.get("code")
        message = error.get("message")
        raise SpeToolError(
            SPE_SESSION_CREATE_CODE,
            f"创建会话被拒绝（{code if isinstance(code, str) else 'UNKNOWN'}）："
            f"{message if isinstance(message, str) else '未知错误'}",
        )

    if res.is_error:
        message = body.get("message")
        raise SpeToolError(
            SPE_SESSION_CREATE_CODE,
            f"创建会话失败（HTTP {res.status_code}）："
            f"{message if isinstance(message, str) and message else '未知错误'}",
        )

    session_id = _session_id_of(body)
    if not session_id:
        raise SpeToolError(SPE_SESSION_CREATE_CODE, "创建会话响应缺少会话 id（sessionId/id/data.id 均缺失）")
    return session_id


async def call_spe_login(
    *,
    base_url: str,
    username: str,
    password: str,
    bearer: str | None = None,
    timeout: float = SPE_SESSION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """登录端点（biz 通道语义：超时与网络失败同归 TOOL_UPSTREAM_ERROR）。

    返回登录响应原文（含 token/role/tenantId/username）；401 归一为
    AUTH_FAILED「用户名或密码错误」，400 归一为 BIZ_VALIDATION_FAILED。
    """
    url = f"{_base(base_url)}/api/v1/auth/login"
    headers = {}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            res = await client.post(url, json={"username": username, "password": password}, headers=headers)
        except httpx.TimeoutException as e:
            raise SpeToolError(SPE_TOOL_UPSTREAM_CODE, f"业务服务超时（{timeout}s）：{e}") from e
        except httpx.HTTPError as e:
            raise SpeToolError(SPE_TOOL_UPSTREAM_CODE, f"业务服务连接失败：{e}") from e

    try:
        body: Any = res.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = {}

    if res.status_code == 400:
        error_text = body.get("error")
        detail = f"：{error_text}" if isinstance(error_text, str) and error_text else ""
        raise SpeToolError("BIZ_VALIDATION_FAILED", f"业务接口参数校验失败（HTTP 400）{detail}")
    if res.status_code == 401:
        raise SpeToolError("AUTH_FAILED", "用户名或密码错误")
    if res.is_error:
        message = body.get("message")
        raise SpeToolError(
            SPE_TOOL_UPSTREAM_CODE,
            f"登录失败（HTTP {res.status_code}）：{message if isinstance(message, str) and message else '未知错误'}",
        )

    token = body.get("token")
    if not isinstance(token, str) or not token:
        raise SpeToolError(SPE_TOOL_UPSTREAM_CODE, "登录响应缺少 token 字段，无法存储登录态")
    return body
