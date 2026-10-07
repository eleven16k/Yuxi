"""SPE biz 读工具执行层：catalog 16 工具的管线、构建器与注册。

每请求管线（认证决策定案）：uid → 用户 JWT（未登录 fail-closed 零出站）→
系统配置 base_url → 按 kind 组装 query（含租户/店铺解析）→ biz-get 通道。
分页/字符串参数共用口径与桥一致（page ≥0 缺省 0；size 夹紧 [1,100] 缺省 20；
空字符串参数不携带）。blockId 为进程级自增（桥同款），多用户唯一性在
generate_report 移植（批次 2B）时一并裁决。
"""

from __future__ import annotations

import itertools
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.tools import ToolException
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import Field, create_model

from yuxi.agents.toolkits.registry import tool
from yuxi.agents.toolkits.spe.catalog import BIZ_CARD_TYPES, BizReadSpec, SPE_BIZ_READ_TOOLS
from yuxi.agents.toolkits.spe.client import SpeToolError, call_spe_biz_get
from yuxi.agents.toolkits.spe.errors import format_spe_failure
from yuxi.agents.toolkits.spe.resolvers import (
    resolve_store_id,
    resolve_store_ref,
    resolve_tenant_id,
)
from yuxi.config.options import spe_agent_opts
from yuxi.repositories import spe_credential_repository

_TYPES = {"string": str, "number": float, "integer": int}
_OPS_PLAN_STATUSES = {"PREPARING", "ACTIVE", "ARCHIVED", "FAILED"}

_block_seq = itertools.count(1)

Builder = Callable[..., Awaitable[Any]]


def _next_block_id() -> str:
    """进程级自增 blockId（桥 nextBizBlockId 同款语义）。"""
    return f"blk_{next(_block_seq)}"


def _page_args(params: dict[str, Any]) -> dict[str, int]:
    """page ≥0 缺省 0；size 夹紧 [1,100] 缺省 20，越界不报错。"""
    page = params.get("page")
    page = page if isinstance(page, int) and not isinstance(page, bool) and page >= 0 else 0
    size = params.get("size")
    if not isinstance(size, int) or isinstance(size, bool):
        size = 20
    size = max(1, min(100, size))
    return {"page": page, "size": size}


def _string_args(params: dict[str, Any], keys: tuple[str, ...]) -> dict[str, str]:
    """仅非空字符串参数携带；多余参数一律忽略。"""
    return {key: params[key] for key in keys if isinstance(params.get(key), str) and params[key]}


# ---------- 各 kind 的 query 组装与出站 ----------


async def _build_plain(spec: BizReadSpec, params: dict[str, Any], *, base_url: str, jwt: str) -> Any:
    return await call_spe_biz_get(base_url=base_url, jwt=jwt, path=spec.path, query={})


async def _build_paged(spec: BizReadSpec, params: dict[str, Any], *, base_url: str, jwt: str) -> Any:
    return await call_spe_biz_get(base_url=base_url, jwt=jwt, path=spec.path, query=_page_args(params))


async def _build_list_orders(spec: BizReadSpec, params: dict[str, Any], *, base_url: str, jwt: str) -> Any:
    query = _page_args(params) | _string_args(params, ("status", "startDate", "endDate"))
    keyword = params.get("keyword")
    if isinstance(keyword, str) and keyword:
        query["keyword"] = keyword[:100]
    store = params.get("store")
    if isinstance(store, str) and store:
        query["storeId"] = await resolve_store_ref(base_url=base_url, jwt=jwt, ref=store)
    return await call_spe_biz_get(base_url=base_url, jwt=jwt, path=spec.path, query=query)


async def _build_hot_products(spec: BizReadSpec, params: dict[str, Any], *, base_url: str, jwt: str) -> Any:
    query = _page_args(params) | _string_args(params, ("keyword", "category1", "brand"))
    period = params.get("period")
    query["period"] = period if period in ("weekly", "monthly") else "weekly"
    for key in ("minPrice", "maxPrice"):
        value = params.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            query[key] = value
    return await call_spe_biz_get(base_url=base_url, jwt=jwt, path=spec.path, query=query)


async def _build_monthly_report(spec: BizReadSpec, params: dict[str, Any], *, base_url: str, jwt: str) -> Any:
    year = params.get("year")
    month = params.get("month")
    if not isinstance(year, int) or isinstance(year, bool) or not isinstance(month, int) or isinstance(month, bool):
        raise SpeToolError("INVALID_ARGS", "year 与 month 必填（整数）")
    if not 1 <= month <= 12:
        raise SpeToolError("INVALID_ARGS", "month 必须在 1–12 之间")
    store = params.get("store")
    if isinstance(store, str) and store:
        store_id = await resolve_store_ref(base_url=base_url, jwt=jwt, ref=store)
    else:
        store_id = await resolve_store_id(base_url=base_url, jwt=jwt)
    query = {"storeId": store_id, "year": year, "month": month}
    return await call_spe_biz_get(base_url=base_url, jwt=jwt, path=spec.path, query=query)


async def _build_credit_transactions(spec: BizReadSpec, params: dict[str, Any], *, base_url: str, jwt: str) -> Any:
    query = _page_args(params)
    transaction_type = params.get("type")
    if isinstance(transaction_type, str) and transaction_type:
        query["type"] = transaction_type
    date_range = params.get("dateRange")
    if date_range in ("7d", "30d"):
        query["dateRange"] = date_range
    return await call_spe_biz_get(base_url=base_url, jwt=jwt, path=spec.path, query=query)


async def _build_dashboard_stats(spec: BizReadSpec, params: dict[str, Any], *, base_url: str, jwt: str) -> Any:
    query: dict[str, Any] = {}
    store = params.get("store")
    if isinstance(store, str) and store:
        query["storeId"] = await resolve_store_ref(base_url=base_url, jwt=jwt, ref=store)
    return await call_spe_biz_get(base_url=base_url, jwt=jwt, path=spec.path, query=query)


async def _build_tenant_scoped(spec: BizReadSpec, params: dict[str, Any], *, base_url: str, jwt: str) -> Any:
    tenant_id = await resolve_tenant_id(base_url=base_url, jwt=jwt)
    return await call_spe_biz_get(base_url=base_url, jwt=jwt, path=spec.path, query={"tenantId": tenant_id})


async def _build_date_range(spec: BizReadSpec, params: dict[str, Any], *, base_url: str, jwt: str) -> Any:
    query = _string_args(params, ("dateFrom", "dateTo"))
    return await call_spe_biz_get(base_url=base_url, jwt=jwt, path=spec.path, query=query)


async def _build_date_range_paged(spec: BizReadSpec, params: dict[str, Any], *, base_url: str, jwt: str) -> Any:
    query = _page_args(params) | _string_args(params, ("dateFrom", "dateTo"))
    return await call_spe_biz_get(base_url=base_url, jwt=jwt, path=spec.path, query=query)


async def _build_ops_plans(spec: BizReadSpec, params: dict[str, Any], *, base_url: str, jwt: str) -> Any:
    status = params.get("status")
    query = _page_args(params) | {"status": status if status in _OPS_PLAN_STATUSES else "ACTIVE"}
    return await call_spe_biz_get(base_url=base_url, jwt=jwt, path=spec.path, query=query)


async def _build_source_draft(spec: BizReadSpec, params: dict[str, Any], *, base_url: str, jwt: str) -> Any:
    draft_id = params.get("sourceProductId")
    if not isinstance(draft_id, int) or isinstance(draft_id, bool) or draft_id < 1:
        raise SpeToolError("INVALID_ARGS", "sourceProductId 必填（整数 ≥1）")
    path = spec.path.format(id=draft_id)
    try:
        return await call_spe_biz_get(base_url=base_url, jwt=jwt, path=path, query={})
    except SpeToolError as e:
        if e.code == "BIZ_NOT_FOUND":
            raise SpeToolError("DRAFT_NOT_FOUND", e.message) from e
        raise


async def _build_collection_status(spec: BizReadSpec, params: dict[str, Any], *, base_url: str, jwt: str) -> Any:
    stats = await call_spe_biz_get(base_url=base_url, jwt=jwt, path=spec.path, query={})
    recent = await call_spe_biz_get(
        base_url=base_url, jwt=jwt, path="/api/v1/products/collected", query={"page": 0, "size": 5}
    )
    return {"stats": stats, "recent": recent}


_BUILDERS: dict[str, Builder] = {
    "plain": _build_plain,
    "paged": _build_paged,
    "list_orders": _build_list_orders,
    "hot_products": _build_hot_products,
    "monthly_report": _build_monthly_report,
    "credit_transactions": _build_credit_transactions,
    "dashboard_stats": _build_dashboard_stats,
    "tenant_scoped": _build_tenant_scoped,
    "date_range": _build_date_range,
    "date_range_paged": _build_date_range_paged,
    "ops_plans": _build_ops_plans,
    "source_draft": _build_source_draft,
    "collection_status": _build_collection_status,
}


# ---------- 工具工厂与注册 ----------


def _input_model(spec: BizReadSpec) -> type:
    """按 catalog 参数面生成输入模型；无参数工具补 dummy 占位（langchain 约定）。"""
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


def _make_biz_tool(spec: BizReadSpec):
    builder = _BUILDERS[spec.kind]
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
        if not base_url:
            raise ToolException(
                format_spe_failure("SPE_NOT_CONFIGURED", "SPE 未配置：请管理员设置 spe_agent_opts.base_url")
            )
        try:
            data = await builder(spec, params, base_url=base_url, jwt=jwt)
        except SpeToolError as e:
            raise ToolException(format_spe_failure(e.code, e.message)) from e
        return {"blockId": _next_block_id(), "ui": BIZ_CARD_TYPES[spec.name], "v": 1, "data": data}

    return tool(
        category="spe",
        tags=["SPE 运营"],
        display_name=spec.name,
        name_or_callable=spec.name,
        description=spec.description,
        args_schema=model,
    )(_impl)


# 模块导入即注册（与 login/logout 同机制）；同时以工具名为模块属性暴露供测试与装配
SPE_BIZ_READ_TOOL_NAMES: list[str] = []
for _spec in SPE_BIZ_READ_TOOLS:
    globals()[_spec.name] = _make_biz_tool(_spec)
    SPE_BIZ_READ_TOOL_NAMES.append(_spec.name)
