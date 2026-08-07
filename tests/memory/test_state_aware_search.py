from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mem0.configs.base import MemoryConfig, RecallConfig
from mem0.memory.main import AsyncMemory, Memory
from mem0.utils.scoring import normalize_bm25


def stored(memory_id, text, score, **payload):
    return SimpleNamespace(id=memory_id, score=score, payload={"data": text, **payload})


def make_memory(mode="state_aware"):
    memory = Memory.__new__(Memory)
    memory.config = MemoryConfig(recall=RecallConfig(mode=mode))
    memory.embedding_model = MagicMock()
    memory.embedding_model.embed.return_value = [0.1, 0.2]
    memory.vector_store = MagicMock()
    memory._compute_entity_boosts = MagicMock(return_value={})
    return memory


def make_async_memory(mode="state_aware"):
    memory = AsyncMemory.__new__(AsyncMemory)
    memory.config = MemoryConfig(recall=RecallConfig(mode=mode))
    memory.embedding_model = MagicMock()
    memory.embedding_model.embed.return_value = [0.1, 0.2]
    memory.vector_store = MagicMock()
    memory._compute_entity_boosts_async = AsyncMock(return_value={})
    return memory


class CompleteSearchVectorStore:
    _mem0_supports_complete_list = True

    def __init__(self, rows):
        self.rows = [*rows]
        self.search = MagicMock(return_value=[])
        self.keyword_search = MagicMock(return_value=None)
        self.get = MagicMock(return_value=None)
        self.list = MagicMock(side_effect=lambda filters=None, top_k=None: (self.rows[:top_k], None))
        self.complete_list = MagicMock(return_value=(self.rows, None))

    def _mem0_list_complete(self, filters=None, top_k=None):
        return self.complete_list(filters=filters, top_k=top_k)


def test_state_aware_search_returns_keyword_only_candidate_and_hides_internal_metadata(monkeypatch):
    memory = make_memory()
    memory.vector_store.search.return_value = [stored("dense", "A dense result", 0.9)]
    memory.vector_store.keyword_search.return_value = [
        stored(
            "keyword",
            "Passport suffix 42",
            12.0,
            topic="identity",
            _mem0_status="active",
            _mem0_schema_version=1,
        )
    ]
    monkeypatch.setattr("mem0.memory.main.extract_entities", lambda _query: [])
    monkeypatch.setattr("mem0.memory.main.lemmatize_for_bm25", lambda query, **_kwargs: query.casefold())

    results = memory._search_vector_store("passport 42", {"user_id": "u1"}, 10, threshold=0.0)

    assert {item["id"] for item in results} == {"dense", "keyword"}
    keyword = next(item for item in results if item["id"] == "keyword")
    assert keyword["metadata"] == {"topic": "identity"}
    assert all(not key.startswith("_mem0_") for key in keyword["metadata"])


def test_state_aware_search_fetches_entity_only_candidate(monkeypatch):
    memory = make_memory()
    memory.vector_store.search.return_value = []
    memory.vector_store.keyword_search.return_value = []
    memory._compute_entity_boosts.return_value = {"entity-only": 0.4}
    memory.vector_store.get.return_value = stored("entity-only", "Met Akira", 0.0, user_id="u1")
    monkeypatch.setattr("mem0.memory.main.extract_entities", lambda _query: [("person", "Akira")])
    monkeypatch.setattr("mem0.memory.main.lemmatize_for_bm25", lambda query: query.casefold())

    results = memory._search_vector_store("Akira", {"user_id": "u1"}, 10, threshold=0.0)

    assert [item["id"] for item in results] == ["entity-only"]
    memory.vector_store.get.assert_called_once_with(vector_id="entity-only")


def test_state_aware_search_rejects_entity_link_outside_identity_scope(monkeypatch):
    memory = make_memory()
    memory.vector_store.search.return_value = []
    memory.vector_store.keyword_search.return_value = []
    memory._compute_entity_boosts.return_value = {"other-users-memory": 0.9}
    memory.vector_store.get.return_value = stored(
        "other-users-memory",
        "Private memory for another user",
        0.0,
        user_id="u2",
    )
    monkeypatch.setattr("mem0.memory.main.extract_entities", lambda _query: [("person", "Akira")])
    monkeypatch.setattr("mem0.memory.main.lemmatize_for_bm25", lambda query: query.casefold())

    results = memory._search_vector_store("Akira", {"user_id": "u1"}, 10, threshold=0.0)

    assert results == []


@pytest.mark.asyncio
async def test_async_state_aware_search_rejects_entity_link_outside_identity_scope(monkeypatch):
    memory = make_async_memory()
    memory.vector_store.search.return_value = []
    memory.vector_store.keyword_search.return_value = []
    memory._compute_entity_boosts_async.return_value = {"other-users-memory": 0.9}
    memory.vector_store.get.return_value = stored(
        "other-users-memory",
        "Private memory for another user",
        0.0,
        user_id="u2",
    )
    monkeypatch.setattr("mem0.memory.main.extract_entities", lambda _query: [("person", "Akira")])
    monkeypatch.setattr("mem0.memory.main.lemmatize_for_bm25", lambda query: query.casefold())

    results = await memory._search_vector_store("Akira", {"user_id": "u1"}, 10, threshold=0.0)

    assert results == []


def test_state_aware_search_uses_lexical_fallback_when_store_has_no_keyword_api(monkeypatch):
    memory = make_memory()
    memory.vector_store = CompleteSearchVectorStore(
        [stored("lexical", "東京都に住んでいます", 0.0, user_id="u1")]
    )
    monkeypatch.setattr("mem0.memory.main.extract_entities", lambda _query: [])

    results = memory._search_vector_store("東京", {"user_id": "u1"}, 10, threshold=0.0)

    assert [item["id"] for item in results] == ["lexical"]


def test_state_aware_lexical_fallback_finds_candidate_beyond_dense_fetch_budget(monkeypatch):
    rows = [stored(f"row-{index}", "unrelated", 0.0, user_id="u1") for index in range(70)]
    rows.append(stored("late-keyword", "Passport suffix 42", 0.0, user_id="u1"))
    memory = make_memory()
    memory.vector_store = CompleteSearchVectorStore(rows)
    monkeypatch.setattr("mem0.memory.main.extract_entities", lambda _query: [])

    results = memory._search_vector_store("Passport 42", {"user_id": "u1"}, 10, threshold=0.0)

    assert [item["id"] for item in results] == ["late-keyword"]
    memory.vector_store.complete_list.assert_called_once_with(filters={"user_id": "u1"}, top_k=10_000)


def test_state_aware_lexical_fallback_skips_unproven_listing_without_losing_dense_result(monkeypatch):
    memory = make_memory()
    memory.vector_store.search.return_value = [stored("dense", "Dense answer", 0.9, user_id="u1")]
    memory.vector_store.keyword_search.return_value = None
    memory.vector_store.list.return_value = ([stored("unsafe", "Passport 42", 0.0, user_id="u1")], None)
    monkeypatch.setattr("mem0.memory.main.extract_entities", lambda _query: [])

    results = memory._search_vector_store("Passport 42", {"user_id": "u1"}, 10, threshold=0.0)

    assert [item["id"] for item in results] == ["dense"]
    memory.vector_store.list.assert_not_called()


@pytest.mark.asyncio
async def test_async_state_aware_lexical_fallback_finds_candidate_beyond_dense_fetch_budget(monkeypatch):
    rows = [stored(f"row-{index}", "unrelated", 0.0, user_id="u1") for index in range(70)]
    rows.append(stored("late-keyword", "予約番号は四二です", 0.0, user_id="u1"))
    memory = make_async_memory()
    memory.vector_store = CompleteSearchVectorStore(rows)
    monkeypatch.setattr("mem0.memory.main.extract_entities", lambda _query: [])

    results = await memory._search_vector_store("予約番号", {"user_id": "u1"}, 10, threshold=0.0)

    assert [item["id"] for item in results] == ["late-keyword"]
    memory.vector_store.complete_list.assert_called_once_with(filters={"user_id": "u1"}, top_k=10_000)


def test_legacy_search_keeps_semantic_prethreshold_and_excludes_keyword_only(monkeypatch):
    memory = make_memory(mode="legacy")
    memory.vector_store.search.return_value = [stored("dense", "Dense", 0.4)]
    memory.vector_store.keyword_search.return_value = [stored("keyword", "Keyword", 20.0)]
    monkeypatch.setattr("mem0.memory.main.extract_entities", lambda _query: [])
    monkeypatch.setattr("mem0.memory.main.lemmatize_for_bm25", lambda query, **_kwargs: query.casefold())

    results = memory._search_vector_store("query", {"user_id": "u1"}, 10, threshold=0.5)

    assert results == []


def test_legacy_search_preserves_prechange_multi_candidate_order_and_scores(monkeypatch):
    memory = make_memory(mode="legacy")
    memory.vector_store.search.return_value = [
        stored("dense-a", "Keyword-favored", 0.7),
        stored("dense-b", "Entity-favored", 0.9),
    ]
    memory.vector_store.keyword_search.return_value = [stored("dense-a", "Keyword-favored", 10.0)]
    memory._compute_entity_boosts.return_value = {"dense-b": 0.4}
    monkeypatch.setattr("mem0.memory.main.extract_entities", lambda _query: [("topic", "query")])
    monkeypatch.setattr(
        "mem0.memory.main.lemmatize_for_bm25",
        lambda query, **_kwargs: query.casefold(),
    )

    results = memory._search_vector_store(
        "query",
        {"user_id": "u1"},
        10,
        threshold=0.5,
        explain=True,
    )

    keyword_score = normalize_bm25(10.0, midpoint=5.0, steepness=0.7)
    assert [item["id"] for item in results] == ["dense-a", "dense-b"]
    assert results[0]["score"] == pytest.approx((0.7 + keyword_score) / 2.5)
    assert results[1]["score"] == pytest.approx((0.9 + 0.4) / 2.5)
    assert results[0]["score_details"] == {
        "semantic_score": 0.7,
        "bm25_score": pytest.approx(keyword_score),
        "entity_boost": 0.0,
        "raw_score": pytest.approx(0.7 + keyword_score),
        "max_possible_score": 2.5,
        "final_score": pytest.approx((0.7 + keyword_score) / 2.5),
        "threshold": 0.5,
    }


@pytest.mark.asyncio
async def test_async_legacy_search_keeps_semantic_prethreshold_and_excludes_keyword_only(monkeypatch):
    memory = make_async_memory(mode="legacy")
    memory.vector_store.search.return_value = [stored("dense", "Dense", 0.4)]
    memory.vector_store.keyword_search.return_value = [stored("keyword", "Keyword", 20.0)]
    monkeypatch.setattr("mem0.memory.main.extract_entities", lambda _query: [])
    monkeypatch.setattr("mem0.memory.main.lemmatize_for_bm25", lambda query, **_kwargs: query.casefold())

    results = await memory._search_vector_store("query", {"user_id": "u1"}, 10, threshold=0.5)

    assert results == []


def test_get_never_exposes_reserved_lifecycle_metadata():
    memory = make_memory()
    memory.vector_store.get.return_value = stored(
        "memory-1",
        "User lives in Tokyo",
        0.0,
        topic="profile",
        _mem0_status="active",
        _mem0_source_hashes=["hash"],
    )

    result = memory.get("memory-1")

    assert result["metadata"] == {"topic": "profile"}


@pytest.mark.asyncio
async def test_async_state_aware_search_returns_keyword_only_candidate(monkeypatch):
    memory = make_async_memory()
    memory.vector_store.search.return_value = []
    memory.vector_store.keyword_search.return_value = [
        stored("keyword", "予約番号は四二です", 8.0, _mem0_status="active")
    ]
    monkeypatch.setattr("mem0.memory.main.extract_entities", lambda _query: [])
    monkeypatch.setattr("mem0.memory.main.lemmatize_for_bm25", lambda query: query)

    results = await memory._search_vector_store("予約番号", {"user_id": "u1"}, 10, threshold=0.0)

    assert [item["id"] for item in results] == ["keyword"]


@pytest.mark.asyncio
async def test_async_get_never_exposes_reserved_lifecycle_metadata():
    memory = make_async_memory()
    memory.vector_store.get.return_value = stored(
        "memory-1",
        "User lives in Tokyo",
        0.0,
        topic="profile",
        _mem0_status="active",
    )

    result = await memory.get("memory-1")

    assert result["metadata"] == {"topic": "profile"}


def _raise_not_implemented(_query):
    raise NotImplementedError("no noun chunks")


def test_state_aware_search_survives_entity_extraction_failure(monkeypatch):
    memory = make_memory()
    memory.vector_store.search.return_value = []
    memory.vector_store.keyword_search.return_value = []
    monkeypatch.setattr("mem0.memory.main.extract_entities", _raise_not_implemented)
    monkeypatch.setattr("mem0.memory.main.lemmatize_for_bm25", lambda query, **_kwargs: query.casefold())

    results = memory._search_vector_store("query", {"user_id": "u1"}, 10, threshold=0.0)

    assert results == []


def test_legacy_search_survives_entity_extraction_failure(monkeypatch):
    memory = make_memory(mode="legacy")
    memory.vector_store.search.return_value = []
    memory.vector_store.keyword_search.return_value = []
    monkeypatch.setattr("mem0.memory.main.extract_entities", _raise_not_implemented)
    monkeypatch.setattr("mem0.memory.main.lemmatize_for_bm25", lambda query, **_kwargs: query.casefold())

    results = memory._search_vector_store("query", {"user_id": "u1"}, 10, threshold=0.0)

    assert results == []


@pytest.mark.asyncio
async def test_async_state_aware_search_survives_entity_extraction_failure(monkeypatch):
    memory = make_async_memory()
    memory.vector_store.search.return_value = []
    memory.vector_store.keyword_search.return_value = []
    monkeypatch.setattr("mem0.memory.main.extract_entities", _raise_not_implemented)
    monkeypatch.setattr("mem0.memory.main.lemmatize_for_bm25", lambda query, **_kwargs: query.casefold())

    results = await memory._search_vector_store("query", {"user_id": "u1"}, 10, threshold=0.0)

    assert results == []


@pytest.mark.asyncio
async def test_async_legacy_search_survives_entity_extraction_failure(monkeypatch):
    memory = make_async_memory(mode="legacy")
    memory.vector_store.search.return_value = []
    memory.vector_store.keyword_search.return_value = []
    monkeypatch.setattr("mem0.memory.main.extract_entities", _raise_not_implemented)
    monkeypatch.setattr("mem0.memory.main.lemmatize_for_bm25", lambda query, **_kwargs: query.casefold())

    results = await memory._search_vector_store("query", {"user_id": "u1"}, 10, threshold=0.0)

    assert results == []
