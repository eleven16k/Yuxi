"""内化知识面工具：kb_list / kb_read / yuxi_kb_search。

原桥经 HTTP 外呼 Yuxi external_kb 端点；迁移后进程内直调 KnowledgeBaseManager
（T07 差异清单定案）。语义适配：

- scope 三态坍缩为 uid 可见集，映射保留：official = read_scope access_level
  为 global（全平台共享），tenant = 其余；category 参数兼容保留、恒忽略
  （与桥 yuxi 模式同口径）。
- I-1 零探测：复合目标 kbId 不在可见集 → YUXI_NOT_FOUND，不触任何检索。
- I-3 单库降级：多库并发检索单库失败即丢弃，全败才抛首个异常。
- M-4 score 合并：按原始 score 降序、缺失/非数值垫 0、ties 稳定排序，
  round 只做展示。
- kb_read 两窗预算（每窗 1800 行）与 M-1 行号前缀剥除（总宽恒 6 防误剥 TSV）。
- YUXI_NOT_CONFIGURED / YUXI_AUTH_FAILED 错误族退役（无外部配置与凭据）。
"""

from __future__ import annotations

import asyncio
import itertools
import re
from typing import Any

from langchain_core.tools import ToolException
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import Field, create_model

from yuxi.agents.toolkits.registry import tool
from yuxi.agents.toolkits.spe.catalog import SPE_INTERNALIZED_KB_TOOLS
from yuxi.agents.toolkits.spe.errors import format_spe_failure

_TYPES = {"string": str, "number": float, "integer": int, "array": list, "object": dict}

_KB_READ_WINDOW = 1800
_KB_READ_MAX_WINDOWS = 2
# 总宽恒 6 的行号前缀（open 层生成 f"{n:6d}\t{line}"）；宽度先行断言防误剥正文 TSV 行
_LINE_PREFIX = re.compile(r"^(?=.{6}\t) {0,5}\d{1,6}\t")

_block_seq = itertools.count(1)


def _kb():
    """惰性取 KnowledgeBaseManager 单例（kbs toolkit 同款模式）。"""
    from yuxi.knowledge.runtime import knowledge_base

    return knowledge_base


def _next_block_id() -> str:
    return f"blk_{next(_block_seq)}"


async def visible_kb_summaries(runtime: ToolRuntime) -> list[dict[str, Any]]:
    """当前 uid 的可见 KB 摘要（含 share_config），缓存在 context 上。"""
    context = getattr(runtime, "context", None)
    if context is None:
        return []
    cached = getattr(context, "_spevisible_kb_summaries", None)
    if isinstance(cached, list):
        return cached
    uid = getattr(context, "uid", None)
    if not uid:
        summaries: list[Any] = []
    else:
        summaries = await _kb().get_databases_by_uid(str(uid))
    records = [summary if isinstance(summary, dict) else _summary_to_dict(summary) for summary in summaries]
    setattr(context, "_spevisible_kb_summaries", records)
    return records


def _summary_to_dict(summary: Any) -> dict[str, Any]:
    """KnowledgeBaseSummary → 需要的字段投影（含 scope 判定所需 share_config）。"""
    from yuxi.permissions import is_official_share_config

    return {
        "kb_id": summary.kb_id,
        "name": summary.name,
        "description": summary.description,
        "kb_type": summary.kb_type,
        "is_official": is_official_share_config(getattr(summary, "share_config", None)),
        "kb_name": summary.name,
        "can_manage": bool(getattr(summary, "can_manage", False)),
    }


def _scope_label(is_official: bool) -> str:
    return "官方" if is_official else "本租户"


def _filter_by_scope(summaries: list[dict[str, Any]], scope: str | None) -> list[dict[str, Any]]:
    if scope == "official":
        return [s for s in summaries if s["is_official"]]
    if scope == "tenant":
        return [s for s in summaries if not s["is_official"]]
    return summaries


def _raw_score(metadata: dict[str, Any]) -> float:
    """M-4：原始分值参与排序；非数值/缺失垫 0。"""
    score = metadata.get("score") if isinstance(metadata, dict) else None
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return 0.0
    return float(score)


# ---------- 三个工具的实现 ----------


async def _kb_search_impl(runtime: ToolRuntime, **params: Any) -> dict[str, Any]:
    query = (params.get("query") or "")[:200]
    if not query.strip():
        raise ToolException(format_spe_failure("INVALID_ARGS", "query 必填（非空）"))
    top_k = params.get("top_k")
    if not isinstance(top_k, int) or isinstance(top_k, bool):
        top_k = 10
    top_k = max(1, min(20, top_k))
    file_name = params.get("file_name") or None

    targets = _filter_by_scope(await visible_kb_summaries(runtime), params.get("scope"))
    if not targets:
        return {"ok": False, "error": "当前没有可访问的知识库"}

    options: dict[str, Any] = {"final_top_k": top_k, "search_mode": "hybrid"}
    if file_name:
        options["file_name"] = file_name

    async def _one(kb: dict[str, Any]) -> dict[str, Any]:
        return await _kb().retrieve(kb["kb_id"], query, **options)

    # I-3：单库失败丢弃，全败抛首个
    gathered = await asyncio.gather(*(_one(kb) for kb in targets), return_exceptions=True)
    failures = [g for g in gathered if isinstance(g, BaseException)]
    if len(failures) == len(gathered):
        raise ToolException(f"知识检索失败：{failures[0]}") from failures[0]

    rows: list[dict[str, Any]] = []
    for kb, result in zip(targets, gathered):
        if isinstance(result, BaseException):
            continue
        for hit in result.get("results", []):
            metadata = hit.get("metadata") or {}
            rows.append(
                {
                    "id": f"{kb['kb_id']}/{hit.get('file_id') or ''}",
                    "snippet": (hit.get("content") or "")[:200],
                    "source": metadata.get("source", ""),
                    "score": round(_raw_score(metadata), 3),
                    "chunkIndex": metadata.get("chunk_index"),
                    "_sort": _raw_score(metadata),
                }
            )
    # M-4：原始分降序、ties 稳定；round 只做展示
    rows.sort(key=lambda row: row["_sort"], reverse=True)
    rows = rows[:top_k]
    for row in rows:
        row.pop("_sort", None)

    return {
        "blockId": _next_block_id(),
        "ui": "panel",
        "v": 1,
        "data": {
            "columns": ["记录ID", "片段", "来源文件", "分数", "分片序"],
            "rows": rows,
            "page": 0,
            "size": len(rows),
            "total": len(rows),
        },
    }


async def _kb_list_impl(runtime: ToolRuntime, **params: Any) -> dict[str, Any]:
    page = params.get("page")
    page = page if isinstance(page, int) and not isinstance(page, bool) and page >= 0 else 0
    size = params.get("size")
    if not isinstance(size, int) or isinstance(size, bool):
        size = 20
    size = max(1, min(100, size))
    keyword = params.get("q")
    keyword = keyword[:100] if isinstance(keyword, str) and keyword else None

    visible = _filter_by_scope(await visible_kb_summaries(runtime), params.get("scope"))
    if not visible:
        return {"ok": False, "error": "当前没有可访问的知识库"}

    result = await _kb().search_document_files(visible, query=keyword, offset=page * size, limit=size)
    scope_by_kb = {s["kb_id"]: _scope_label(s["is_official"]) for s in visible}
    rows = [
        {
            "id": f"{item['kb_id']}/{item['file_id']}",
            "title": item["filename"],
            "category": item.get("kb_name") or "",
            "scope": scope_by_kb.get(item["kb_id"], ""),
            "updatedAt": item.get("updated_at") or "",
            "author": "",
        }
        for item in result.get("files", [])
    ]
    return {
        "blockId": _next_block_id(),
        "ui": "panel",
        "v": 1,
        "data": {
            "columns": ["记录ID", "标题", "分类", "范围", "更新时间", "作者"],
            "rows": rows,
            "page": page,
            "size": size,
            "total": result.get("total", len(rows)),
        },
    }


async def _kb_read_impl(runtime: ToolRuntime, **params: Any) -> dict[str, Any]:
    record_id = params.get("id")
    if not isinstance(record_id, str) or not record_id or len(record_id) > 128 or "/" not in record_id:
        raise ToolException(format_spe_failure("INVALID_ARGS", "id 必填（≤128 字符的「{kb_id}/{file_id}」复合记录ID）"))
    kb_id, file_id = record_id.split("/", 1)
    if not kb_id or not file_id:
        raise ToolException(format_spe_failure("INVALID_ARGS", "复合记录ID 的 kb_id 与 file_id 均不能为空"))

    visible = await visible_kb_summaries(runtime)
    target = next((s for s in visible if s["kb_id"] == kb_id), None)
    # I-1：不可见 = 不存在，零检索零探测
    if target is None:
        raise ToolException(
            format_spe_failure(
                "YUXI_NOT_FOUND", f"记录 '{record_id}' 不存在或当前身份无权访问；跨租户知识不可见是预期行为"
            )
        )

    # 定位文件元数据（file_id 不支持直查，按库内文件列表定位；与桥同款限制）
    listing = await _kb().search_document_files([target], offset=0, limit=500)
    meta = next((f for f in listing.get("files", []) if f["file_id"] == file_id), None)
    if meta is None:
        raise ToolException(format_spe_failure("YUXI_NOT_FOUND", f"文件 '{file_id}' 不存在或尚无解析产物"))

    # 两窗阅读：每窗 1800 行，第三窗零调用
    chunks: list[str] = []
    offset = 0
    has_more_after = False
    for _ in range(_KB_READ_MAX_WINDOWS):
        window = await _kb().open_document(kb_id, file_id, offset=offset, limit=_KB_READ_WINDOW)
        chunks.append(window.get("content") or "")
        has_more_after = bool(window.get("has_more_after"))
        next_offset = window.get("next_offset")
        if not has_more_after or next_offset is None:
            has_more_after = False
            break
        offset = next_offset
    else:
        has_more_after = True

    # M-1：剥行号前缀（总宽恒 6，防误剥正文 TSV 行）
    stripped_lines = [_LINE_PREFIX.sub("", line) for chunk in chunks for line in chunk.splitlines()]
    content = "\n".join(stripped_lines)
    if has_more_after:
        content += "\n（原文超长，已截断至 3600 行——可在知识后台查看全文）"

    return {
        "blockId": _next_block_id(),
        "ui": "article",
        "v": 1,
        "data": {
            "title": meta["filename"],
            "scope": _scope_label(target["is_official"]),
            "category": target["kb_name"],
            "author": "",
            "updatedAt": meta.get("updated_at") or "",
            "content": content,
        },
    }


_IMPLS = {
    "yuxi_kb_search": _kb_search_impl,
    "kb_list": _kb_list_impl,
    "kb_read": _kb_read_impl,
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


def _make_kb_tool(spec):
    impl = _IMPLS[spec.name]
    model = _input_model(spec)

    async def _wrapped(runtime: ToolRuntime, **params: Any) -> dict[str, Any]:
        uid = getattr(runtime.context, "uid", None) or ""
        if not uid:
            return {"ok": False, "error": "无法获取当前用户身份"}
        return await impl(runtime, **params)

    return tool(
        category="spe",
        tags=["SPE 运营"],
        display_name=spec.name,
        name_or_callable=spec.name,
        description=spec.description,
        args_schema=model,
    )(_wrapped)


SPE_INTERNALIZED_TOOL_NAMES: list[str] = []
for _spec in SPE_INTERNALIZED_KB_TOOLS:
    globals()[_spec.name] = _make_kb_tool(_spec)
    SPE_INTERNALIZED_TOOL_NAMES.append(_spec.name)
