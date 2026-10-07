"""SPE 工具面错误码注册表与失败文案（契约 C-13 的 Python 内化）。

错误码分两族：映射表内 22 码（桥 errors.ts GUIDANCE 键集合）与表外运行时码
（走 FALLBACK_GUIDANCE 回落）。guidance 逐字移植自契约快照，仅四处 dsh 运维
语境做显式适配（ADAPTED_CODES），对账测试以快照为 oracle 核对两族一致性。
"""

from __future__ import annotations

SPE_TOOL_TIMEOUT_CODE = "TOOL_UPSTREAM_TIMEOUT"
SPE_TOOL_UPSTREAM_CODE = "TOOL_UPSTREAM_ERROR"
SPE_SESSION_CREATE_CODE = "SESSION_CREATE_FAILED"

# 映射表外、由运行时直接归一的码（快照 error_semantics.runtime_codes_outside_table）
SPE_RUNTIME_CODES = frozenset(
    {
        SPE_TOOL_TIMEOUT_CODE,
        SPE_TOOL_UPSTREAM_CODE,
        SPE_SESSION_CREATE_CODE,
        "AUTH_FAILED",
        "YUXI_NOT_CONFIGURED",
        "NAV_ROUTE_REJECTED",
    }
)

# 桥 errors.ts GUIDANCE 全量移植：code -> 模型降级指引
SPE_GUIDANCE: dict[str, str] = {
    "TOOL_BUDGET_EXCEEDED": "按次预算已用尽：不要重试本工具，用已取数据回答并如实告知",
    "UPSTREAM_SESSION_EXPIRED": "上游 Ozon 会话过期：提示用户到「平台对接」页重新登录，恢复前本工具不可用",
    "JEV_DISABLED": "Jev 打分链路未启用：不要重试，如实说明本次没有 Jev 评级，改用已取数据回答",
    "JEV_UNAVAILABLE": "Jev 上游本次不可用：不要重试，如实说明本次没有 Jev 评级",
    "TOOL_NO_DATA": "本次查询无可用数据：不要重试，换筛选条件或换 SKU，不编造数据",
    "INSUFFICIENT_CREDIT": "账户积分不足：提示用户充值后再试",
    "SESSION_NOT_FOUND": "内部信号：会话在后端不存在，工具层自动重建后重试一次；正常不直达模型",
    "SESSION_NOT_ACTIVE": "内部信号：会话不活跃，工具层自动重建后重试一次；正常不直达模型",
    "TOOL_NOT_IMPLEMENTED": "后端未实现该工具：以原始错误为准，不要重试",
    "UNKNOWN_TOOL": "后端不认识该工具名：核对后改用工具目录内工具",
    "INVALID_ARGS": "参数校验失败：按提示修正参数后再调用",
    "BIZ_AUTH_FAILED": "SPE 登录态无效或过期：请用 login 工具重新登录后再试",
    "BIZ_FORBIDDEN": "该接口需要更高账号角色，hot_products 需 ADMIN",
    "BIZ_NOT_FOUND": "检查参数；租户未绑定店铺时先到 SPE 后台绑定",
    "BIZ_NO_STORE": "当前租户未绑定启用的 Ozon 店铺",
    "BIZ_VALIDATION_FAILED": "参数校验失败，检查参数后重试",
    "BIZ_INSUFFICIENT_CREDIT": "告知用户积分不足需充值，不要重试；充值后可再执行",
    "DRAFT_NOT_FOUND": "先用 generate_listing 生成草稿，再执行本操作",
    "YUXI_NOT_FOUND": "该知识在 Yuxi 不存在或无权访问——确认记录ID 来自 kb_list/yuxi_kb_search；跨租户不可见是预期行为",
    "YUXI_DUPLICATE_CONTENT": "该文件内容已存在，无需重复上传——用 kb_list/yuxi_kb_search 确认；确需重传先删除或修改",
    "YUXI_RESEARCH_TIMEOUT": "深度研究超时未终态：run 仍在后台，同题再调续取同一 run（不重复耗算力）；避免高频重发",
    "YUXI_RESEARCH_FAILED": "深度研究失败（failed/cancelled/interrupted）：以原始错误为准（含 run_id），不要原样重试",
}

# 未知/空码的通用回落文案（不编造数据、如实说明失败）
SPE_FALLBACK_GUIDANCE = "如实说明本次调用失败，不编造数据；可向用户确认配置或稍后再试"

# 因宿主从 dsh 换为 Yuxi 而显式改写的 guidance（对账测试核对差异集恰为此四条）
ADAPTED_CODES = frozenset(
    {
        "BIZ_AUTH_FAILED",  # 原文引用环境变量 SPE_AGENT_JWT，Yuxi 无此变量
        "SESSION_NOT_FOUND",  # 原文「桥自动重建」改为「工具层自动重建」
        "SESSION_NOT_ACTIVE",
        # UPSTREAM_SESSION_EXPIRED 提到的「平台对接」页属于 SPE 前端，仍存在，保留原文
    }
)


def spe_guidance_for(code: str | None) -> str:
    """取错误码对应的模型降级 guidance；未知/空码回落通用文案。"""
    if code and code in SPE_GUIDANCE:
        return SPE_GUIDANCE[code]
    return SPE_FALLBACK_GUIDANCE


def format_spe_failure(code: str | None, message: str) -> str:
    """组装失败返回文案：`code: message` 换行后附处置 guidance。"""
    return f"{code or SPE_TOOL_UPSTREAM_CODE}: {message}\n处置：{spe_guidance_for(code)}"
