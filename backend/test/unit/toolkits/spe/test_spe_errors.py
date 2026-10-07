"""errors.py 与契约快照的对账测试。

oracle 是冻结快照（fixtures/spe_contract_snapshot.json）：映射表 22 码的
guidance 必须与快照逐字一致，唯 ADAPTED_CODES（dsh→Yuxi 语境适配集）允许且
必须有显式差异；表外运行时码与映射表互不相交。
"""

import json
from pathlib import Path

from yuxi.agents.toolkits.spe.errors import (
    ADAPTED_CODES,
    SPE_FALLBACK_GUIDANCE,
    SPE_GUIDANCE,
    SPE_RUNTIME_CODES,
    format_spe_failure,
    spe_guidance_for,
)

_SNAPSHOT = Path(__file__).resolve().parent / "fixtures" / "spe_contract_snapshot.json"


def _snapshot_error_codes() -> list[dict[str, str]]:
    with _SNAPSHOT.open(encoding="utf-8") as f:
        return json.load(f)["error_codes"]


def test_table_codes_match_snapshot_exactly() -> None:
    """映射表 code 集合与快照 22 码完全一致。"""
    snapshot_codes = {e["code"] for e in _snapshot_error_codes()}
    assert set(SPE_GUIDANCE) == snapshot_codes


def test_guidance_verbatim_except_adapted() -> None:
    """guidance 逐字移植；差异集必须恰为 ADAPTED_CODES。"""
    differing = set()
    for entry in _snapshot_error_codes():
        code = entry["code"]
        if SPE_GUIDANCE[code] != entry["guidance"]:
            differing.add(code)
    assert differing == ADAPTED_CODES
    # 适配不是删减：适配码仍保有非空 guidance
    for code in ADAPTED_CODES:
        assert SPE_GUIDANCE[code]


def test_runtime_codes_disjoint_from_table() -> None:
    """表外运行时码与映射表互不相交（与快照 error_semantics 同构）。"""
    assert not SPE_RUNTIME_CODES & set(SPE_GUIDANCE)


def test_unknown_code_falls_back() -> None:
    """未知/空码回落通用文案，不编造 guidance。"""
    assert spe_guidance_for("NEVER_SEEN_CODE") == SPE_FALLBACK_GUIDANCE
    assert spe_guidance_for(None) == SPE_FALLBACK_GUIDANCE
    assert spe_guidance_for("TOOL_NO_DATA") == SPE_GUIDANCE["TOOL_NO_DATA"]


def test_failure_format_shape() -> None:
    """失败文案形如 `code: message` 换行附处置 guidance。"""
    text = format_spe_failure("BIZ_NO_STORE", "当前租户没有店铺")
    assert text == (f"BIZ_NO_STORE: 当前租户没有店铺\n处置：{SPE_GUIDANCE['BIZ_NO_STORE']}")
    # 空码回落 TOOL_UPSTREAM_ERROR 前缀
    assert format_spe_failure(None, "x").startswith("TOOL_UPSTREAM_ERROR: x")
