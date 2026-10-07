"""SPE biz 通道的辅助解析器（租户/店铺，契约 C-22 的桥内化）。

这些端点不在工具面，但 query 组装必需：list_products/low_stock 需要显式
tenantId（后端不做 JWT 反查），月报缺省店铺取首个启用店铺，store 参数按
ID 精确等值或店铺名全等解析。每次调用现解析、不缓存（桥同款）。
"""

from __future__ import annotations

from typing import Any

from yuxi.agents.toolkits.spe.client import SpeToolError, call_spe_biz_get

_STORES_PATH = "/api/v1/integration/ozon/stores"
_AUTH_ME_PATH = "/api/v1/auth/me"


async def resolve_tenant_id(*, base_url: str, jwt: str) -> int:
    """从 /auth/me 解析当前租户 id；无 tenantId 视为登录态异常。"""
    data = await call_spe_biz_get(base_url=base_url, jwt=jwt, path=_AUTH_ME_PATH, query={})
    tenant_id = data.get("tenantId") if isinstance(data, dict) else None
    if not isinstance(tenant_id, int) or isinstance(tenant_id, bool):
        raise SpeToolError("BIZ_AUTH_FAILED", "登录态缺少 tenantId，请用 login 工具重新登录")
    return tenant_id


async def fetch_stores(*, base_url: str, jwt: str) -> list[dict[str, Any]]:
    """拉取店铺列表；非数组回落空列表（桥护栏）。"""
    data = await call_spe_biz_get(base_url=base_url, jwt=jwt, path=_STORES_PATH, query={})
    return data if isinstance(data, list) else []


async def resolve_store_id(*, base_url: str, jwt: str) -> int | str:
    """取首个启用店铺 id；无启用店铺归一 BIZ_NO_STORE。"""
    stores = await fetch_stores(base_url=base_url, jwt=jwt)
    for store in stores:
        if isinstance(store, dict) and store.get("isActive") is True:
            store_id = store.get("id")
            if isinstance(store_id, (int, str)):
                return store_id
    raise SpeToolError("BIZ_NO_STORE", "未找到启用中的 Ozon 店铺（stores 列表为空或无 isActive=true 项）")


async def resolve_store_ref(*, base_url: str, jwt: str, ref: str) -> int | str:
    """store 参数解析：先按 id 字符串精确等值，再按店铺名全等；未命中列出全部店铺名。"""
    stores = await fetch_stores(base_url=base_url, jwt=jwt)
    for store in stores:
        if isinstance(store, dict) and str(store.get("id")) == ref:
            return store["id"]
    for store in stores:
        if isinstance(store, dict) and store.get("storeName") == ref:
            return store["id"]
    names = [s.get("storeName") for s in stores if isinstance(s, dict) and s.get("storeName")]
    raise SpeToolError("BIZ_STORE_NOT_FOUND", f"店铺不存在：{ref}（可选店铺：{', '.join(names) or '无'}）")
