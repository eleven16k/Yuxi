"""内化知识面（kb_list/kb_read/yuxi_kb_search）测试：对账 + 直调语义（假 manager）。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.tools import ToolException

from yuxi.agents.toolkits.registry import get_all_tool_instances
from yuxi.agents.toolkits.spe import kb_tools
from yuxi.agents.toolkits.spe.catalog import SPE_INTERNALIZED_KB_TOOLS

_SNAPSHOT = Path(__file__).resolve().parent / "fixtures" / "spe_contract_snapshot.json"


def _runtime(uid: str = "u1"):
    return SimpleNamespace(context=SimpleNamespace(uid=uid))


def _summary(kb_id: str, name: str, *, official: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        kb_id=kb_id,
        name=name,
        description=None,
        kb_type="milvus",
        share_config={"read_scope": {"access_level": "global" if official else "user"}},
    )


class _FakeKB:
    """假 KnowledgeBaseManager：脚本化 retrieve/files/open 并记录调用。"""

    def __init__(self, summaries, retrieve_results=None, files=None, windows=None) -> None:
        self.summaries = summaries
        self.retrieve_results = retrieve_results or {}
        self.files = files or {"files": [], "total": 0}
        self.windows = windows or []
        self.retrieve_calls: list[dict] = []
        self.files_calls: list[dict] = []
        self.open_calls: list[dict] = []

    async def get_databases_by_uid(self, uid: str) -> list:
        return self.summaries

    async def retrieve(self, kb_id: str, query: str, **options):
        self.retrieve_calls.append({"kb_id": kb_id, "query": query, "options": options})
        outcome = self.retrieve_results[kb_id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def search_document_files(self, knowledge_bases, *, query=None, offset=0, limit=300, **kwargs):
        self.files_calls.append(
            {"kbs": [k["kb_id"] for k in knowledge_bases], "query": query, "offset": offset, "limit": limit}
        )
        return self.files

    async def open_document(self, kb_id: str, file_id: str, *, offset=0, limit=200):
        self.open_calls.append({"kb_id": kb_id, "file_id": file_id, "offset": offset, "limit": limit})
        return self.windows[len(self.open_calls) - 1]


def test_internalized_specs_reconcile_with_snapshot() -> None:
    """3 工具参数面逐字等于快照（scope/category 兼容保留，语义适配在实现层文档化）。"""
    snapshot = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    by_name = {t["name"]: t for t in snapshot["tools"] if t["name"] in {"kb_list", "kb_read", "yuxi_kb_search"}}
    assert {s.name for s in SPE_INTERNALIZED_KB_TOOLS} == set(by_name) == set(kb_tools.SPE_INTERNALIZED_TOOL_NAMES)
    for spec in SPE_INTERNALIZED_KB_TOOLS:
        assert spec.description == by_name[spec.name]["description"]
        assert spec.card == by_name[spec.name]["card_type"]
        assert [(p["name"], p["required"]) for p in spec.params] == [
            (p["name"], bool(p.get("required"))) for p in by_name[spec.name]["params"]
        ]
    assert {t.name for t in get_all_tool_instances()} >= set(kb_tools.SPE_INTERNALIZED_TOOL_NAMES)


def test_kb_summary_scope_mapping() -> None:
    official = kb_tools._summary_to_dict(_summary("kb_a", "官方库", official=True))
    tenant = kb_tools._summary_to_dict(_summary("kb_b", "私有库"))
    assert official["is_official"] is True and tenant["is_official"] is False
    assert kb_tools._scope_label(official["is_official"]) == "官方"


@pytest.fixture
def two_kb_manager(monkeypatch):
    manager = _FakeKB(
        summaries=[_summary("kb_off", "官方库", official=True), _summary("kb_ten", "租户库")],
        retrieve_results={
            "kb_off": {
                "kb_id": "kb_off",
                "results": [
                    {
                        "id": "c1",
                        "kb_id": "kb_off",
                        "file_id": "f1",
                        "content": "官方片段甲",
                        "metadata": {"source": "未知来源", "chunk_index": 0, "score": 0.9},
                    },
                ],
            },
            "kb_ten": {
                "kb_id": "kb_ten",
                "results": [
                    {
                        "id": "c2",
                        "kb_id": "kb_ten",
                        "file_id": "f2",
                        "content": "租户高分片段",
                        "metadata": {"source": "dify.md", "chunk_index": 3, "score": 0.95},
                    },
                    {
                        "id": "c3",
                        "kb_id": "kb_ten",
                        "file_id": "f2",
                        "content": "无分数片段",
                        "metadata": {"source": "dify.md", "chunk_index": 4},
                    },
                ],
            },
        },
    )
    monkeypatch.setattr(kb_tools, "_kb", lambda: manager)
    return manager


async def test_kb_search_merge_sort_and_envelope(two_kb_manager) -> None:
    result = await kb_tools.yuxi_kb_search.coroutine(runtime=_runtime(), query="选品", top_k=99)
    # M-4：原始分降序（0.95 > 0.9 > 缺失垫 0），top_k 夹紧到 20 内全量保留
    rows = result["data"]["rows"]
    assert [r["score"] for r in rows] == [0.95, 0.9, 0.0]
    assert rows[0]["id"] == "kb_ten/f2" and rows[0]["chunkIndex"] == 3
    assert result["data"]["columns"] == ["记录ID", "片段", "来源文件", "分数", "分片序"]
    assert result["data"]["total"] == result["data"]["size"] == 3
    # 检索选项恒带 hybrid + final_top_k
    assert all(
        c["options"]["search_mode"] == "hybrid" and c["options"]["final_top_k"] == 20
        for c in two_kb_manager.retrieve_calls
    )


async def test_kb_search_scope_official_only(two_kb_manager) -> None:
    await kb_tools.yuxi_kb_search.coroutine(runtime=_runtime(), query="选品", scope="official")
    assert [c["kb_id"] for c in two_kb_manager.retrieve_calls] == ["kb_off"]


async def test_kb_search_single_failure_degrades(two_kb_manager, monkeypatch) -> None:
    """I-3：单库失败丢弃不阻断；全败才报错。"""
    two_kb_manager.retrieve_results["kb_off"] = RuntimeError("milvus down")
    result = await kb_tools.yuxi_kb_search.coroutine(runtime=_runtime(), query="选品")
    assert all(r["id"].startswith("kb_ten/") for r in result["data"]["rows"])

    two_kb_manager.retrieve_results["kb_ten"] = RuntimeError("dify down")
    with pytest.raises(ToolException) as info:
        await kb_tools.yuxi_kb_search.coroutine(runtime=_runtime(), query="选品")
    assert "知识检索失败" in str(info.value)


async def test_kb_search_query_truncation_and_validation(two_kb_manager) -> None:
    with pytest.raises(ToolException) as info:
        await kb_tools.yuxi_kb_search.coroutine(runtime=_runtime(), query="   ")
    assert "INVALID_ARGS" in str(info.value)
    await kb_tools.yuxi_kb_search.coroutine(runtime=_runtime(), query="长" * 350)
    assert len(two_kb_manager.retrieve_calls[-1]["query"]) == 200


async def test_kb_list_pagination_and_mapping(monkeypatch) -> None:
    manager = _FakeKB(
        summaries=[_summary("kb_off", "官方库", official=True), _summary("kb_ten", "租户库")],
        files={
            "files": [
                {
                    "kb_id": "kb_off",
                    "kb_name": "官方库",
                    "file_id": "f1",
                    "filename": "选品方法.md",
                    "status": "done",
                    "updated_at": "2026-10-01",
                },
                {
                    "kb_id": "kb_ten",
                    "kb_name": "租户库",
                    "file_id": "f2",
                    "filename": "SOP.md",
                    "status": "done",
                    "updated_at": "2026-10-02",
                },
            ],
            "total": 42,
        },
    )
    monkeypatch.setattr(kb_tools, "_kb", lambda: manager)
    result = await kb_tools.kb_list.coroutine(runtime=_runtime(), q="选品" * 60, category="被忽略", page=2, size=50)
    call = manager.files_calls[0]
    assert call["offset"] == 100 and call["limit"] == 50  # SQL 级分页（优于桥侧切片）
    assert len(call["query"]) == 100  # q 截断
    rows = result["data"]["rows"]
    assert rows[0] == {
        "id": "kb_off/f1",
        "title": "选品方法.md",
        "category": "官方库",
        "scope": "官方",
        "updatedAt": "2026-10-01",
        "author": "",
    }
    assert rows[1]["scope"] == "本租户"
    assert result["data"]["total"] == 42


def _window(content: str, *, has_more_after: bool, next_offset=None) -> dict:
    return {"content": content, "has_more_after": has_more_after, "next_offset": next_offset}


async def test_kb_read_two_windows_strip_prefix_and_truncation(monkeypatch) -> None:
    manager = _FakeKB(
        summaries=[_summary("kb_off", "官方库", official=True)],
        files={
            "files": [{"file_id": "f1", "filename": "指南.md", "status": "done", "updated_at": "2026-10-03"}],
            "total": 1,
        },
        windows=[
            _window("     1\t第一行\n     2\ta\tb\tc", has_more_after=True, next_offset=1800),
            _window("  1801\t末行", has_more_after=True, next_offset=3600),
        ],
    )
    monkeypatch.setattr(kb_tools, "_kb", lambda: manager)
    result = await kb_tools.kb_read.coroutine(runtime=_runtime(), id="kb_off/f1")
    data = result["data"]
    assert set(data) == {"title", "scope", "category", "author", "updatedAt", "content"}
    assert data["title"] == "指南.md" and data["scope"] == "官方"
    # M-1：剥行号前缀；正文 TSV 行（a\tb\tc）原样保留
    assert data["content"] == "第一行\na\tb\tc\n末行\n（原文超长，已截断至 3600 行——可在知识后台查看全文）"
    assert len(manager.open_calls) == 2  # 第三窗零调用
    assert manager.open_calls[0]["limit"] == 1800


async def test_kb_read_invisible_kb_zero_probing(monkeypatch) -> None:
    manager = _FakeKB(summaries=[_summary("kb_off", "官方库", official=True)])
    monkeypatch.setattr(kb_tools, "_kb", lambda: manager)
    with pytest.raises(ToolException) as info:
        await kb_tools.kb_read.coroutine(runtime=_runtime(), id="kb_other/f1")
    assert "YUXI_NOT_FOUND" in str(info.value)
    assert manager.files_calls == [] and manager.open_calls == []  # I-1 零探测


async def test_kb_read_invalid_ids(monkeypatch) -> None:
    manager = _FakeKB(summaries=[])
    monkeypatch.setattr(kb_tools, "_kb", lambda: manager)
    for bad in ("no_slash", "/f", "kb/", "x" * 200):
        with pytest.raises(ToolException) as info:
            await kb_tools.kb_read.coroutine(runtime=_runtime(), id=bad)
        assert "INVALID_ARGS" in str(info.value)


async def test_kb_read_file_not_found(monkeypatch) -> None:
    manager = _FakeKB(
        summaries=[_summary("kb_off", "官方库", official=True)],
        files={"files": [{"file_id": "f_other", "filename": "别的.md"}], "total": 1},
    )
    monkeypatch.setattr(kb_tools, "_kb", lambda: manager)
    with pytest.raises(ToolException) as info:
        await kb_tools.kb_read.coroutine(runtime=_runtime(), id="kb_off/f_missing")
    assert "YUXI_NOT_FOUND" in str(info.value)
    assert manager.open_calls == []
