"""SPE biz 写通道工具（biz-post 10 工具：8 闸门写 + 2 预览）。

闸门写经审批确认后（T04：interrupt_on 拦截，mode 驱动）由本层执行：校验 →
写回执登记（C-26：120s 幂等窗、uid 进 opKey、失败不缓存）→ biz 写通道出站
→ confirm-result 回执信封。预览类（preview_dynamic_price/collect_products）
不闸不幂等（桥既定语义）。列名/行映射按契约描述组织；精细卡面成形依赖批次 5
呈现决策，当前行数据原样透传。
"""

from __future__ import annotations

import itertools
from typing import Any

from langchain_core.tools import ToolException
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import Field, create_model

from yuxi.agents.toolkits.registry import tool
from yuxi.agents.toolkits.spe.catalog import SPE_BIZ_WRITE_TOOLS, SPE_ORDER_WRITE_TOOLS
from yuxi.agents.toolkits.spe.client import SpeToolError, call_spe_biz_mutate
from yuxi.agents.toolkits.spe.errors import format_spe_failure
from yuxi.agents.toolkits.spe.receipts import write_receipts
from yuxi.agents.toolkits.spe.resolvers import resolve_store_id, resolve_store_ref
from yuxi.config.options import spe_agent_opts
from yuxi.repositories import spe_credential_repository

_TYPES = {"string": str, "number": float, "integer": int, "array": list, "object": dict}
GENERATE_LISTING_TIMEOUT_SECONDS = 300.0

_block_seq = itertools.count(1)

PreviewColumns = ["价格记录 ID", "SKU", "原价", "推荐价", "价格变化百分比", "是否启用"]
CollectColumns = ["商品名称", "均价", "最低价", "销量", "销售额", "加购转化", "广告占比", "卖家", "主图", "链接"]


def _next_block_id() -> str:
    return f"blk_{next(_block_seq)}"


def _int_arg(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _cell(value: Any) -> Any:
    """透传标量；dict/list 原样（桥 cell 的等价最小实现）。"""
    return value


def _envelope(ui: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"blockId": _next_block_id(), "ui": ui, "v": 1, "data": data}


# ---------- 预览类（不闸不幂等） ----------


async def _exec_preview_dynamic_price(params: dict[str, Any], *, base_url: str, jwt: str, uid: str) -> dict[str, Any]:
    product_id = params.get("productId")
    if not isinstance(product_id, str) or not product_id:
        raise SpeToolError("INVALID_ARGS", "preview_dynamic_price 参数 productId 必填（非空字符串）")
    if len(product_id) > 64:
        raise SpeToolError("INVALID_ARGS", "preview_dynamic_price 参数 productId 超过 64 字符上限")
    records = await call_spe_biz_mutate(
        base_url=base_url,
        jwt=jwt,
        method="POST",
        path="/api/v1/pricing/dynamic/calculate",
        query={"productId": product_id},
    )
    rows = records if isinstance(records, list) else []
    return _envelope("data-table", {"columns": PreviewColumns, "rows": rows, "totalElements": len(rows)})


async def _exec_collect_products(params: dict[str, Any], *, base_url: str, jwt: str, uid: str) -> dict[str, Any]:
    skus = params.get("skus")
    valid = isinstance(skus, list) and 1 <= len(skus) <= 50 and all(isinstance(s, str) and s.strip() for s in skus)
    if not valid:
        raise SpeToolError("INVALID_ARGS", "collect_products 参数 skus 必填（1–50 个非空字符串的数组）")
    listing = await call_spe_biz_mutate(
        base_url=base_url,
        jwt=jwt,
        method="POST",
        path="/api/v1/products/collect/sku",
        body={"skus": skus},
    )
    rows = listing if isinstance(listing, list) else []
    return _envelope(
        "panel",
        {
            "columns": CollectColumns,
            "rows": rows,
            "totalElements": len(rows),
            "panel": {"tool": "collect_products", "page": 0, "size": len(rows), "total": len(rows)},
        },
    )


# ---------- 闸门写（审批后执行；回执登记） ----------


async def _exec_apply_dynamic_price(params: dict[str, Any], *, base_url: str, jwt: str, uid: str) -> dict[str, Any]:
    price_id = params.get("priceId")
    if not _int_arg(price_id):
        raise SpeToolError("INVALID_ARGS", "apply_dynamic_price 参数 priceId 必填（整数 ≥1）")

    async def _run() -> tuple[str, dict[str, Any]]:
        await call_spe_biz_mutate(
            base_url=base_url,
            jwt=jwt,
            method="POST",
            path=f"/api/v1/pricing/dynamic/{price_id}/apply",
        )
        return f"已应用动态推荐价（价格记录 #{price_id}）", {"priceId": price_id}

    receipt = await write_receipts.exec_tracked(
        uid=uid, tool="apply_dynamic_price", args={"priceId": price_id}, run=_run
    )
    return _confirm_envelope(receipt)


async def _exec_restock_inventory(params: dict[str, Any], *, base_url: str, jwt: str, uid: str) -> dict[str, Any]:
    inventory_id = params.get("inventoryId")
    quantity = params.get("quantity")
    unit_cost = params.get("unitCost")
    if (
        not _int_arg(inventory_id)
        or not _int_arg(quantity)
        or not isinstance(unit_cost, (int, float))
        or isinstance(unit_cost, bool)
        or unit_cost < 0
    ):
        raise SpeToolError(
            "INVALID_ARGS",
            "restock_inventory 参数 inventoryId/quantity/unitCost 必填（inventoryId/quantity 整数 ≥1，unitCost 为 ≥0 的数字）",  # noqa: E501
        )

    async def _run() -> tuple[str, dict[str, Any]]:
        await call_spe_biz_mutate(
            base_url=base_url,
            jwt=jwt,
            method="POST",
            path=f"/api/v1/inventory/{inventory_id}/restock",
            query={"quantity": quantity, "unitCost": unit_cost},
        )
        return f"已补货库存项 #{inventory_id}：+{quantity} 件 @ {unit_cost}", {
            "inventoryId": inventory_id,
            "quantity": quantity,
            "unitCost": unit_cost,
        }

    receipt = await write_receipts.exec_tracked(
        uid=uid,
        tool="restock_inventory",
        args={"inventoryId": inventory_id, "quantity": quantity, "unitCost": unit_cost},
        run=_run,
    )
    return _confirm_envelope(receipt)


async def _exec_gift_credits(params: dict[str, Any], *, base_url: str, jwt: str, uid: str) -> dict[str, Any]:
    tenant_id = params.get("tenantId")
    amount = params.get("amount")
    remark = params.get("remark")
    if not _int_arg(tenant_id) or not _int_arg(amount):
        raise SpeToolError("INVALID_ARGS", "gift_credits 参数 tenantId/amount 必填（整数 ≥1）")
    if remark is not None and (not isinstance(remark, str) or len(remark) > 200):
        raise SpeToolError("INVALID_ARGS", "gift_credits 参数 remark 需为 ≤200 字符的字符串")
    # opKey/body 共用同一清理后参数：remark 缺省不带键
    payload = (
        {"tenantId": tenant_id, "amount": amount}
        if remark is None
        else {"tenantId": tenant_id, "amount": amount, "remark": remark}
    )

    async def _run() -> tuple[str, dict[str, Any]]:
        result = await call_spe_biz_mutate(
            base_url=base_url,
            jwt=jwt,
            method="POST",
            path="/api/v1/credit/admin/gift",
            body=payload,
        )
        balance = result.get("balance") if isinstance(result, dict) else None
        return f"已向租户 {tenant_id} 赠送 {amount} 积分，余额 {balance}", {
            "tenantId": tenant_id,
            "amount": amount,
            "balance": balance,
        }

    receipt = await write_receipts.exec_tracked(uid=uid, tool="gift_credits", args=payload, run=_run)
    return _confirm_envelope(receipt)


async def _exec_generate_listing(params: dict[str, Any], *, base_url: str, jwt: str, uid: str) -> dict[str, Any]:
    source_product_id = params.get("sourceProductId")
    if not _int_arg(source_product_id):
        raise SpeToolError("INVALID_ARGS", "generate_listing 参数 sourceProductId 必填（整数 ≥1）")
    store_ref = params.get("store")
    store_ref = store_ref if isinstance(store_ref, str) and store_ref else ""

    async def _run() -> tuple[str, dict[str, Any]]:
        store_id = (
            await resolve_store_ref(base_url=base_url, jwt=jwt, ref=store_ref)
            if store_ref
            else await resolve_store_id(base_url=base_url, jwt=jwt)
        )
        draft = await call_spe_biz_mutate(
            base_url=base_url,
            jwt=jwt,
            method="POST",
            path=f"/api/v1/source-products/{source_product_id}/generate-listing",
            query={"storeId": store_id},
            timeout=GENERATE_LISTING_TIMEOUT_SECONDS,
        )
        d = draft if isinstance(draft, dict) else {}
        title = d.get("titleRu") or d.get("titleCn") or ""
        return (
            f"已为源货 #{source_product_id} 生成 Ozon 上架草稿（店铺 {store_ref or '默认'}）：{title}",
            {
                "draftId": _cell(d.get("id")),
                "sourceProductId": d.get("sourceProductId") or source_product_id,
                "titleRu": d.get("titleRu") or "",
                "titleCn": d.get("titleCn") or "",
                "priceRub": _cell(d.get("priceRub")),
                "priceCny": _cell(d.get("priceCny")),
                "categoryName": d.get("categoryName") or "",
                "status": d.get("status") or "",
            },
        )

    # opKey 只含清理后参数（store 名与缺省首店等价时 opKey 不同——按参数面去重）
    receipt = await write_receipts.exec_tracked(
        uid=uid,
        tool="generate_listing",
        args={"sourceProductId": source_product_id, "store": store_ref},
        run=_run,
    )
    return _confirm_envelope(receipt)


async def _exec_build_rich_content(params: dict[str, Any], *, base_url: str, jwt: str, uid: str) -> dict[str, Any]:
    source_product_id = params.get("sourceProductId")
    if not _int_arg(source_product_id):
        raise SpeToolError("INVALID_ARGS", "build_rich_content 参数 sourceProductId 必填（整数 ≥1）")

    async def _run() -> tuple[str, dict[str, Any]]:
        result = await call_spe_biz_mutate(
            base_url=base_url,
            jwt=jwt,
            method="POST",
            path=f"/api/v1/source-products/{source_product_id}/rich-content/build",
            query={"auto": "false"},
        )
        chars = (
            len(result["richContentJson"])
            if isinstance(result, dict) and isinstance(result.get("richContentJson"), str)
            else 0
        )
        return f"已为源货 #{source_product_id} 生成 Rich Content 富内容（{chars} 字符，已写回草稿）", {
            "sourceProductId": source_product_id,
            "richContentChars": chars,
        }

    receipt = await write_receipts.exec_tracked(
        uid=uid, tool="build_rich_content", args={"sourceProductId": source_product_id}, run=_run
    )
    return _confirm_envelope(receipt)


async def _exec_generate_variant_titles(params: dict[str, Any], *, base_url: str, jwt: str, uid: str) -> dict[str, Any]:
    source_product_id = params.get("sourceProductId")
    if not _int_arg(source_product_id):
        raise SpeToolError("INVALID_ARGS", "generate_variant_titles 参数 sourceProductId 必填（整数 ≥1）")

    async def _run() -> tuple[str, dict[str, Any]]:
        draft = await call_spe_biz_mutate(
            base_url=base_url,
            jwt=jwt,
            method="POST",
            path=f"/api/v1/source-products/{source_product_id}/draft/variant-titles",
        )
        d = draft if isinstance(draft, dict) else {}
        variants = d.get("variantsJson")
        count = len(variants) if isinstance(variants, list) else 0
        return f"已为源货 #{source_product_id} 生成变体标题（{count} 个变体已写回）", {
            "sourceProductId": source_product_id,
            "variantCount": count,
            "draftId": _cell(d.get("id")),
        }

    receipt = await write_receipts.exec_tracked(
        uid=uid, tool="generate_variant_titles", args={"sourceProductId": source_product_id}, run=_run
    )
    return _confirm_envelope(receipt)


async def _exec_host_source_images(params: dict[str, Any], *, base_url: str, jwt: str, uid: str) -> dict[str, Any]:
    source_product_id = params.get("sourceProductId")
    if not _int_arg(source_product_id):
        raise SpeToolError("INVALID_ARGS", "host_source_images 参数 sourceProductId 必填（整数 ≥1）")

    async def _run() -> tuple[str, dict[str, Any]]:
        mapping = await call_spe_biz_mutate(
            base_url=base_url,
            jwt=jwt,
            method="POST",
            path=f"/api/v1/source-products/{source_product_id}/host-images",
        )
        hosted = len(mapping) if isinstance(mapping, dict) else 0
        return f"已为源货 #{source_product_id} 托管 {hosted} 张图到公网图床", {
            "sourceProductId": source_product_id,
            "hostedCount": hosted,
        }

    receipt = await write_receipts.exec_tracked(
        uid=uid, tool="host_source_images", args={"sourceProductId": source_product_id}, run=_run
    )
    return _confirm_envelope(receipt)


async def _exec_generate_ozon_images(params: dict[str, Any], *, base_url: str, jwt: str, uid: str) -> dict[str, Any]:
    source_product_id = params.get("sourceProductId")
    if not _int_arg(source_product_id):
        raise SpeToolError("INVALID_ARGS", "generate_ozon_images 参数 sourceProductId 必填（整数 ≥1）")
    count = params.get("count")
    if count is not None and not _int_arg(count):
        raise SpeToolError("INVALID_ARGS", "generate_ozon_images 参数 count 需为整数 ≥1（或不传走后端默认）")
    query = {"count": count} if count is not None else {}

    async def _run() -> tuple[str, dict[str, Any]]:
        await call_spe_biz_mutate(
            base_url=base_url,
            jwt=jwt,
            method="POST",
            path=f"/api/v1/source-products/{source_product_id}/generate-images",
            query=query,
        )
        suffix = f"，{count} 张" if count is not None else ""
        return f"源货 #{source_product_id} 的 AI 套图已启动（异步）{suffix}——用 get_source_draft 轮询生图状态", {
            "sourceProductId": source_product_id,
            "count": count,
            "started": True,
        }

    receipt = await write_receipts.exec_tracked(
        uid=uid,
        tool="generate_ozon_images",
        args={"sourceProductId": source_product_id, "count": count},
        run=_run,
    )
    return _confirm_envelope(receipt)


def _confirm_envelope(receipt: dict[str, Any]) -> dict[str, Any]:
    """confirm-result 回执信封（C-26）。"""
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


def _text_arg(value: Any, label: str, *, max_len: int = 200) -> str:
    """非空字符串校验（剥空白后 1..max_len 字符）。"""
    text = str(value).strip() if value is not None else ""
    if not text or len(text) > max_len:
        raise SpeToolError("INVALID_ARGS", f"{label} 必填（{max_len} 字符内）")
    return text


# ---------- 订单状态机写工具（P1#06，对齐 SPE OrderController） ----------


async def _exec_confirm_order(params: dict[str, Any], *, base_url: str, jwt: str, uid: str) -> dict[str, Any]:
    order_id = params.get("orderId")
    if not _int_arg(order_id):
        raise SpeToolError("INVALID_ARGS", "confirm_order 参数 orderId 必填（整数 ≥1）")

    async def _run() -> tuple[str, dict[str, Any]]:
        await call_spe_biz_mutate(base_url=base_url, jwt=jwt, method="POST", path=f"/api/v1/orders/{order_id}/confirm")
        return f"订单 #{order_id} 已确认", {"orderId": order_id, "action": "confirm"}

    receipt = await write_receipts.exec_tracked(uid=uid, tool="confirm_order", args={"orderId": order_id}, run=_run)
    return _confirm_envelope(receipt)


async def _exec_ship_order(params: dict[str, Any], *, base_url: str, jwt: str, uid: str) -> dict[str, Any]:
    order_id = params.get("orderId")
    if not _int_arg(order_id):
        raise SpeToolError("INVALID_ARGS", "ship_order 参数 orderId 必填（整数 ≥1）")
    tracking = _text_arg(params.get("trackingNumber"), "ship_order 参数 trackingNumber", max_len=100)
    carrier = _text_arg(params.get("carrier"), "ship_order 参数 carrier", max_len=50)

    async def _run() -> tuple[str, dict[str, Any]]:
        await call_spe_biz_mutate(
            base_url=base_url,
            jwt=jwt,
            method="POST",
            path=f"/api/v1/orders/{order_id}/ship",
            query={"trackingNumber": tracking, "carrier": carrier},
        )
        return f"订单 #{order_id} 已发货（{carrier} {tracking}）", {
            "orderId": order_id,
            "action": "ship",
            "carrier": carrier,
            "trackingNumber": tracking,
        }

    receipt = await write_receipts.exec_tracked(
        uid=uid, tool="ship_order", args={"orderId": order_id, "trackingNumber": tracking, "carrier": carrier}, run=_run
    )
    return _confirm_envelope(receipt)


async def _exec_cancel_order(params: dict[str, Any], *, base_url: str, jwt: str, uid: str) -> dict[str, Any]:
    order_id = params.get("orderId")
    if not _int_arg(order_id):
        raise SpeToolError("INVALID_ARGS", "cancel_order 参数 orderId 必填（整数 ≥1）")
    reason = _text_arg(params.get("reason"), "cancel_order 参数 reason")

    async def _run() -> tuple[str, dict[str, Any]]:
        await call_spe_biz_mutate(
            base_url=base_url, jwt=jwt, method="POST", path=f"/api/v1/orders/{order_id}/cancel", query={"reason": reason}
        )
        return f"订单 #{order_id} 已取消：{reason}", {"orderId": order_id, "action": "cancel", "reason": reason}

    receipt = await write_receipts.exec_tracked(
        uid=uid, tool="cancel_order", args={"orderId": order_id, "reason": reason}, run=_run
    )
    return _confirm_envelope(receipt)


async def _exec_refund_order(params: dict[str, Any], *, base_url: str, jwt: str, uid: str) -> dict[str, Any]:
    order_id = params.get("orderId")
    if not _int_arg(order_id):
        raise SpeToolError("INVALID_ARGS", "refund_order 参数 orderId 必填（整数 ≥1）")
    reason = _text_arg(params.get("reason"), "refund_order 参数 reason")

    async def _run() -> tuple[str, dict[str, Any]]:
        await call_spe_biz_mutate(
            base_url=base_url, jwt=jwt, method="POST", path=f"/api/v1/orders/{order_id}/refund", query={"reason": reason}
        )
        return f"订单 #{order_id} 已退款：{reason}", {"orderId": order_id, "action": "refund", "reason": reason}

    receipt = await write_receipts.exec_tracked(
        uid=uid, tool="refund_order", args={"orderId": order_id, "reason": reason}, run=_run
    )
    return _confirm_envelope(receipt)


_EXECUTORS = {
    "preview_dynamic_price": _exec_preview_dynamic_price,
    "collect_products": _exec_collect_products,
    "apply_dynamic_price": _exec_apply_dynamic_price,
    "restock_inventory": _exec_restock_inventory,
    "gift_credits": _exec_gift_credits,
    "generate_listing": _exec_generate_listing,
    "build_rich_content": _exec_build_rich_content,
    "generate_variant_titles": _exec_generate_variant_titles,
    "host_source_images": _exec_host_source_images,
    "generate_ozon_images": _exec_generate_ozon_images,
    "confirm_order": _exec_confirm_order,
    "ship_order": _exec_ship_order,
    "cancel_order": _exec_cancel_order,
    "refund_order": _exec_refund_order,
}


def _input_model(spec) -> type:
    fields: dict[str, Any] = {}
    for param in spec.params:
        annotation = _TYPES[param["type"]]
        if param["required"]:
            fields[param["name"]] = (annotation, Field(description=param["description"]))
        else:
            fields[param["name"]] = (annotation | None, Field(default=None, description=param["description"]))
    return create_model(f"{spec.name}_input", **fields)


def _make_write_tool(spec):
    executor = _EXECUTORS[spec.name]
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
            return await executor(params, base_url=base_url, jwt=jwt, uid=uid)
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


SPE_WRITE_TOOL_NAMES: list[str] = []
for _spec in [*SPE_BIZ_WRITE_TOOLS, *SPE_ORDER_WRITE_TOOLS]:
    globals()[_spec.name] = _make_write_tool(_spec)
    SPE_WRITE_TOOL_NAMES.append(_spec.name)
