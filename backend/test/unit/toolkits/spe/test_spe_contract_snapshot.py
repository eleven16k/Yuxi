"""SPE 桥契约快照不变量（批次 0 对账骨架）。

快照冻结自 spe-agent 仓库 commit（见快照 source_commit 字段），是工具面移植的
oracle。本测试守护快照自身的不变量：计数自洽、命名唯一、模块归属一致、
闸门面与语义模式的微妙关系不被无意改写。批次 2/3 的工具移植测试以本快照
为对照源，不得在 CI 中重新生成快照再验证快照。
"""

import json
import re
from pathlib import Path

_SNAPSHOT_PATH = Path(__file__).resolve().parent / "fixtures" / "spe_contract_snapshot.json"

# 契约文档（s2t）声称并经源码核对的四个冻结计数；改动即代表契约演进，
# 必须显式更新快照并经 review，不允许静默漂移。
FROZEN_TOOL_FACE = 47
FROZEN_WRITE_GATE_FACE = 14
FROZEN_CARD_TOOLS = 46
FROZEN_TOOL_MODULES = 7


def _load_snapshot() -> dict:
    with _SNAPSHOT_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def test_snapshot_provenance_pinned() -> None:
    """快照必须钉死来源 commit 与提取日期，否则对账无从追溯。"""
    snapshot = _load_snapshot()
    assert re.fullmatch(r"[0-9a-f]{40}", snapshot["source_commit"])
    assert snapshot["extracted_at"]
    assert snapshot["snapshot_nature"]


def test_tool_face_count_self_consistent() -> None:
    """工具面总数 = 工具条目数 = 装配分解合计 = 冻结计数 47。"""
    snapshot = _load_snapshot()
    counts = snapshot["counts"]
    tools = snapshot["tools"]
    breakdown = counts["assembly_breakdown"]
    assert len(tools) == counts["tool_face"] == FROZEN_TOOL_FACE
    assert breakdown["total"] == FROZEN_TOOL_FACE
    assert sum(v for k, v in breakdown.items() if k != "total") == FROZEN_TOOL_FACE


def test_write_gate_face_semantics() -> None:
    """写目录 12 / 语义写 13 / 闸门面 14 三层计数互相咬合。

    三层是不同维度：写目录（biz-write-catalog 成员）12 = 10 个闸门写 +
    2 个预览类（preview_dynamic_price、collect_products，语义标 read、
    不闸不幂等）；语义 mode=write 13 = 10 闸门写 + 3 个管理写；闸门面
    14 = 前两者之并 + list_permissions（语义查询但 gate.ts 统一转 ask）。
    """
    snapshot = _load_snapshot()
    counts = snapshot["counts"]
    tools = snapshot["tools"]
    write_mode = [t for t in tools if t["mode"] == "write"]
    gated = [t for t in tools if t["write_gate"]]
    gated_write_mode = [t for t in gated if t["mode"] == "write"]
    gated_read = [t["name"] for t in gated if t["mode"] == "read"]
    by_name = {t["name"]: t for t in tools}

    assert len(gated) == counts["write_gate_face"] == FROZEN_WRITE_GATE_FACE
    assert counts["write_tool_catalog"] == 12
    assert len(write_mode) == 13
    assert len(gated_write_mode) == 13
    assert gated_read == ["list_permissions"]

    for preview in ("preview_dynamic_price", "collect_products"):
        entry = by_name[preview]
        assert entry["mode"] == "read"
        assert entry["write_gate"] is False
        assert entry["idempotent"] is False
    assert by_name["login"]["write_gate"] is False


def test_card_tools_face() -> None:
    """卡面工具 = 工具面减 spe_ping（唯一无信封工具），共 46。"""
    snapshot = _load_snapshot()
    counts = snapshot["counts"]
    tools = snapshot["tools"]
    no_card = [t["name"] for t in tools if t["card_type"] == "none"]
    card_tools = [t for t in tools if t["card_type"] != "none"]
    assert no_card == ["spe_ping"]
    assert len(card_tools) == counts["card_tools"] == FROZEN_CARD_TOOLS


def test_module_membership_consistent() -> None:
    """7 个模块的工具名单与工具条目的 module 字段互为镜像。"""
    snapshot = _load_snapshot()
    counts = snapshot["counts"]
    modules = snapshot["modules"]
    tools = snapshot["tools"]
    assert len(modules) == counts["tool_modules"] == FROZEN_TOOL_MODULES

    by_module: dict[str, list[str]] = {m["id"]: list(m["tools"]) for m in modules}
    flat = [name for names in by_module.values() for name in names]
    assert len(flat) == len(set(flat)), "同一工具出现在多个模块"

    tagged = {t["name"]: t["module"] for t in tools if t["module"]}
    assert set(flat) == set(tagged), "模块名单与工具 module 标注不一致"
    for name, module_id in tagged.items():
        assert name in by_module[module_id]
    # 系统级工具永不进模块矩阵
    for t in tools:
        if t["system_tool"]:
            assert t["module"] is None


def test_tool_entries_well_formed() -> None:
    """工具条目命名唯一、枚举字段合法、参数面形状完整。"""
    snapshot = _load_snapshot()
    tools = snapshot["tools"]
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", n) for n in names)
    assert all(t["mode"] in {"read", "write"} for t in tools)
    assert all(t["description"] for t in tools)
    for tool in tools:
        for param in tool["params"]:
            assert param.get("name")
            assert "type" in param


def test_error_code_registry_frozen() -> None:
    """错误码分两族：errors.ts 映射表 22 码 + 表外运行时码，互不相交。

    YUXI_NOT_CONFIGURED / YUXI_AUTH_FAILED 按 s2t 契约走 FALLBACK_GUIDANCE
    回落、不进映射表；映射表内的 YUXI 族只有 NOT_FOUND/DUPLICATE_CONTENT/
    RESEARCH_TIMEOUT/RESEARCH_FAILED 四个。
    """
    snapshot = _load_snapshot()
    table_codes = [e["code"] for e in snapshot["error_codes"]]
    assert len(table_codes) == len(set(table_codes))
    assert snapshot["counts"]["known_error_codes"] == len(table_codes) == 22
    for entry in snapshot["error_codes"]:
        assert entry["guidance"]

    for required in (
        "TOOL_BUDGET_EXCEEDED",
        "TOOL_NO_DATA",
        "YUXI_NOT_FOUND",
        "YUXI_DUPLICATE_CONTENT",
        "YUXI_RESEARCH_TIMEOUT",
        "YUXI_RESEARCH_FAILED",
    ):
        assert required in table_codes

    runtime_codes = snapshot["error_semantics"]["runtime_codes_outside_table"]
    assert "TOOL_UPSTREAM_ERROR" in runtime_codes
    assert "YUXI_NOT_CONFIGURED" in runtime_codes
    assert not set(runtime_codes) & set(table_codes)


def test_contract_doc_comparison_recorded() -> None:
    """快照内记录的源码计数与契约声称必须一致，防止篡改历史对照。"""
    snapshot = _load_snapshot()
    comparison = snapshot["counts"]["contract_doc_comparison"]
    assert comparison["doc_claims"] == comparison["source_counts"]
    assert comparison["doc_claims"]["tool_face"] == FROZEN_TOOL_FACE
    assert comparison["doc_claims"]["write_tool_face"] == FROZEN_WRITE_GATE_FACE
    assert comparison["doc_claims"]["card_tools"] == FROZEN_CARD_TOOLS
    assert comparison["doc_claims"]["tool_modules"] == FROZEN_TOOL_MODULES
