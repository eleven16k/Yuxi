from dataclasses import dataclass, field

from yuxi.agents.context import BaseContext, ResourceSelection


@dataclass(kw_only=True)
class ChatBotContext(BaseContext):
    enable_filesystem_tools: bool = field(
        default=True,
        metadata={
            "name": "沙盒文件与命令工具",
            "description": (
                "是否启用沙盒文件与命令工具（write_file/edit_file/execute 等）。"
                "面向卖家的对话助手建议关闭——模型误用 execute 会撞人工审批闸（2026-10-08 实测）；"
                "工程/数据分析场景保持开启。"
            ),
            "type": "boolean",
        },
    )
    subagents: ResourceSelection = field(
        default_factory=lambda: ["general-purpose"],
        metadata={
            "name": "子智能体",
            "options": [],
            "description": (
                "可选子智能体列表，默认仅启用通用任务，选择全部可启用当前用户可见的全部子智能体，空列表表示不启用。"
            ),
            "type": "list",
            "kind": "subagents",
        },
    )
