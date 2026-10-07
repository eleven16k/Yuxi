"""MinimaxEmbedding 单测：原生协议 payload 构造、vectors 解析与 db/query 模式切换。"""

from __future__ import annotations

import pytest

from yuxi.models.embed import MinimaxEmbedding, _minimax_query_mode


def _make() -> MinimaxEmbedding:
    return MinimaxEmbedding(
        model="embo-01",
        base_url="https://api.minimaxi.com/v1/embeddings",
        api_key="k",
        dimension=1536,
        batch_size=16,
    )


def test_payload_defaults_to_db_type() -> None:
    emb = _make()
    assert emb.build_payload("文本") == {"model": "embo-01", "texts": ["文本"], "type": "db"}
    assert emb.build_payload(["a", "b"]) == {"model": "embo-01", "texts": ["a", "b"], "type": "db"}


def test_payload_query_mode_via_contextvar() -> None:
    emb = _make()
    token = _minimax_query_mode.set(True)
    try:
        assert emb.build_payload("问句")["type"] == "query"
    finally:
        _minimax_query_mode.reset(token)
    assert emb.build_payload("文本")["type"] == "db"  # 复位后回到 db


def test_extract_embeddings_reads_vectors() -> None:
    assert MinimaxEmbedding._extract_embeddings({"vectors": [[0.1, 0.2], [0.3]]}) == [[0.1, 0.2], [0.3]]


@pytest.mark.parametrize("bad", [{"vectors": []}, {"data": [{"embedding": [1]}]}, {"base_resp": {"status_msg": "err"}}, "not-dict"])
def test_extract_embeddings_rejects_invalid(bad) -> None:
    with pytest.raises(ValueError):
        MinimaxEmbedding._extract_embeddings(bad)
