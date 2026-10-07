"""契约冻结的工具名单（快照派生，纯数据零依赖）。

供审批层等轻量消费方引用，避免拖入整个 spe 包的导入链。
"""

# 闸门写工具 14 名（快照 write_gate 逐字）
SPE_WRITE_GATE_TOOLS: frozenset[str] = frozenset(
    [
        "apply_dynamic_price",
        "apply_permission_preset",
        "build_rich_content",
        "cancel_order",
        "confirm_order",
        "generate_listing",
        "generate_ozon_images",
        "generate_variant_titles",
        "gift_credits",
        "grant_permission",
        "host_source_images",
        "kb_upload",
        "list_permissions",
        "restock_inventory",
        "refund_order",
        "revoke_permission",
        "ship_order",
        "yuxi_research",
    ]
)
