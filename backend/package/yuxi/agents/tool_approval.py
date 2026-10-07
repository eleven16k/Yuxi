from pathlib import PurePosixPath
from typing import Literal

from langchain.agents.middleware import HumanInTheLoopMiddleware

ToolApprovalMode = Literal["default", "always_trust"]

DEFAULT_TOOL_APPROVAL_MODE: ToolApprovalMode = "default"
TOOL_APPROVAL_MODES = frozenset({"default", "always_trust"})
# SPE 闸门写工具 14 名。此处内联字面量而非导入 spe 包：context 在 agents
# 包初始化早期导入本模块，经 spe/__init__ 拉起 services 会成环；与
# yuxi.agents.toolkits.spe.contract_names 的单一来源由对账测试守护恒等。
_SPE_WRITE_GATE_TOOLS = frozenset({
    "apply_dynamic_price", "restock_inventory", "gift_credits", "build_rich_content",
    "generate_variant_titles", "host_source_images", "generate_ozon_images", "generate_listing",
    "kb_upload", "yuxi_research", "grant_permission", "revoke_permission",
    "apply_permission_preset", "list_permissions",
})
# 默认审批模式下需要拦截或对子 Agent 隐藏的敏感 backend 工具；
# SPE 闸门写扩员（T04：无 when 谓词——任何参数组合都需确认）。
SENSITIVE_BACKEND_TOOLS = frozenset({"write_file", "edit_file", "execute"}) | _SPE_WRITE_GATE_TOOLS
_ALLOWED_DECISIONS = ["approve", "reject"]


def normalize_tool_approval_mode(value: object) -> ToolApprovalMode:
    mode = value.strip() if isinstance(value, str) else value
    if mode not in TOOL_APPROVAL_MODES:
        raise ValueError(f"不支持的 tool_approval_mode: {value}")
    return mode


def create_tool_approval_middleware(
    mode: ToolApprovalMode,
    *,
    current_project_path: str | None = None,
):
    """按审批模式与当前 Project 构造敏感工具审批。"""
    if mode == "always_trust":
        return None

    write_requires_approval = _project_write_requires_approval(current_project_path or "")
    interrupt_on = {
        "write_file": {"allowed_decisions": _ALLOWED_DECISIONS, "when": write_requires_approval},
        "edit_file": {"allowed_decisions": _ALLOWED_DECISIONS, "when": write_requires_approval},
        "execute": {"allowed_decisions": _ALLOWED_DECISIONS},
    }
    # SPE 写工具：mode 驱动（T04 定案）——default 拦截，always_trust 在上方整体放行
    for tool_name in sorted(_SPE_WRITE_GATE_TOOLS):
        interrupt_on[tool_name] = {"allowed_decisions": _ALLOWED_DECISIONS}
    return HumanInTheLoopMiddleware(interrupt_on=interrupt_on)


def _project_write_requires_approval(current_project_path: str):
    """创建仅豁免当前 Project 写入的审批谓词。"""
    project_root = _normalize_runtime_path(current_project_path)

    def requires_approval(request) -> bool:
        """非法路径或 Project 外路径继续请求人工确认。"""
        args = request.tool_call.get("args")
        file_path = _normalize_runtime_path(args.get("file_path") if isinstance(args, dict) else None)
        if project_root is None or file_path is None:
            return True
        return file_path != project_root and project_root not in file_path.parents

    return requires_approval


def _normalize_runtime_path(value: object) -> PurePosixPath | None:
    """将审批参数收敛为无跳转的 Sandbox 绝对路径。"""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    path = PurePosixPath(raw)
    if not raw or not path.is_absolute() or ".." in path.parts or "\\" in raw or "://" in raw:
        return None
    return path
