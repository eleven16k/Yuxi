"""租户知识库兜底配额（spec 2026-10-08 §8）：官方库豁免，超限抛 TenantKBQuotaExceededError。"""

from __future__ import annotations


class TenantKBQuotaExceededError(Exception):
    """超出租户知识库兜底配额（文件数或总字节）。"""


async def ensure_tenant_kb_quota(kb_id: str, *, incoming_count: int = 1, incoming_bytes: int = 0) -> None:
    """写入前校验租户库兜底配额；官方库（global）豁免；配置为 0 视为不限制。"""
    from yuxi.config.options import spe_kb_quota_opts
    from yuxi.knowledge.runtime import knowledge_base

    detail = await knowledge_base.get_database_info(kb_id)
    if detail is None:
        return  # 库不存在由后续权限/登记链路报错，此处不抢答
    read_scope = (detail.share_config or {}).get("read_scope") or {}
    if read_scope.get("access_level") == "global":
        return

    config = await spe_kb_quota_opts.get()
    try:
        max_files = int(config.get("max_file_count") or 0)
        max_bytes = int(config.get("max_total_bytes") or 0)
    except (TypeError, ValueError):
        max_files = max_bytes = 0  # 配置脏值按不限制处理，不阻断业务

    if max_files > 0 and (detail.file_count or 0) + incoming_count > max_files:
        raise TenantKBQuotaExceededError(
            f"知识库文件数已达配额上限（现有 {detail.file_count} + 新增 {incoming_count} > {max_files}），"
            "请删除旧文档后再上传"
        )
    if max_bytes > 0 and (detail.total_size or 0) + incoming_bytes > max_bytes:
        raise TenantKBQuotaExceededError(
            f"知识库存储已达配额上限（现有 {detail.total_size} + 新增 {incoming_bytes} > {max_bytes} 字节），"
            "请删除旧文档后再上传"
        )
