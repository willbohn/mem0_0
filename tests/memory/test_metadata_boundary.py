"""Metadata-boundary regression matrix.

Pins already-correct behavior: `text_lemmatized` and every `_mem0_*` lifecycle
field must never appear in caller-visible metadata on any read path (get,
get_all, state-aware search, legacy search; sync and async). No production
code changes accompany this file -- every test here exercises the real
serializer path in `mem0/memory/main.py` (the `core_and_promoted_keys`
exclusion plus `strip_internal_metadata`) against vector-store doubles whose
raw payloads still carry the internal fields, so a regression in the
stripping logic would fail loudly here.
"""

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from mem0.memory.main import AsyncMemory
from tests.memory.test_state_aware_search import (
    make_async_memory as make_async_search_memory,
)
from tests.memory.test_state_aware_search import (
    make_memory as make_search_memory,
)
from tests.memory.test_state_aware_search import stored
from tests.memory.test_state_aware_writes import make_memory as make_write_memory


def _make_async_write_memory() -> AsyncMemory:
    # make_write_memory's `memory_class` param is inferred as type[Memory]
    # from its default value, so passing AsyncMemory is only a static-typing
    # mismatch -- the fixture itself is class-agnostic at runtime (see its
    # `memory_class.__new__(memory_class)` construction).
    return cast(AsyncMemory, make_write_memory([], memory_class=cast(type, AsyncMemory)))


PAYLOAD = {
    "data": "likes hiking",
    "user_id": "u1",
    "source": "fixture",
    "text_lemmatized": "like hike",
    "_mem0_status": "active",
    "created_at": None,
    "updated_at": None,
    "hash": "h",
}

# Same internal-field shape as PAYLOAD, minus "data" (stored() supplies that
# positionally) -- reused as the raw row payload for every search double.
SEARCH_PAYLOAD_EXTRAS = {
    "user_id": "u1",
    "source": "fixture",
    "text_lemmatized": "like hike",
    "_mem0_status": "active",
    "created_at": None,
    "updated_at": None,
    "hash": "h",
}


def _row():
    return SimpleNamespace(id="m1", payload=dict(PAYLOAD), score=0.9)


def _assert_metadata_clean(metadata):
    # "source" surviving guards against a vacuous pass where metadata was
    # wiped entirely instead of having only the internal fields stripped.
    assert metadata.get("source") == "fixture"
    assert "text_lemmatized" not in metadata
    assert not any(key.startswith("_mem0_") for key in metadata)


# --- get ---------------------------------------------------------------


def test_get_excludes_internal_fields():
    memory = make_write_memory([])
    memory.vector_store.get = MagicMock(return_value=_row())

    item = memory.get("m1")

    assert item is not None
    _assert_metadata_clean(item.get("metadata") or {})


@pytest.mark.asyncio
async def test_async_get_excludes_internal_fields():
    memory = _make_async_write_memory()
    memory.vector_store.get = MagicMock(return_value=_row())

    item = await memory.get("m1")

    assert item is not None
    _assert_metadata_clean(item.get("metadata") or {})


# --- get_all -------------------------------------------------------------


def test_get_all_excludes_internal_fields(monkeypatch):
    memory = make_write_memory([])
    memory.vector_store.list = MagicMock(return_value=([_row()], None))
    monkeypatch.setattr("mem0.memory.main.capture_event", lambda *_a, **_k: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice", lambda *_a, **_k: None)

    rows = memory.get_all(filters={"user_id": "u1"})["results"]

    assert rows
    for row in rows:
        _assert_metadata_clean(row.get("metadata") or {})


@pytest.mark.asyncio
async def test_async_get_all_excludes_internal_fields(monkeypatch):
    memory = _make_async_write_memory()
    memory.vector_store.list = MagicMock(return_value=([_row()], None))
    monkeypatch.setattr("mem0.memory.main.capture_event", lambda *_a, **_k: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice_async", AsyncMock())

    rows = (await memory.get_all(filters={"user_id": "u1"}))["results"]

    assert rows
    for row in rows:
        _assert_metadata_clean(row.get("metadata") or {})


# --- search: state-aware ---------------------------------------------------


def test_state_aware_search_excludes_internal_fields(monkeypatch):
    memory = make_search_memory()  # mode="state_aware" (default)
    memory.vector_store.search.return_value = [stored("m1", "likes hiking", 0.9, **SEARCH_PAYLOAD_EXTRAS)]
    memory.vector_store.keyword_search.return_value = []
    monkeypatch.setattr("mem0.memory.main.extract_entities", lambda _query: [])
    monkeypatch.setattr("mem0.memory.main.lemmatize_for_bm25", lambda query, **_kwargs: query.casefold())

    results = memory._search_vector_store("hiking", {"user_id": "u1"}, 10, threshold=0.0)

    assert results
    for item in results:
        _assert_metadata_clean(item.get("metadata") or {})


@pytest.mark.asyncio
async def test_async_state_aware_search_excludes_internal_fields(monkeypatch):
    memory = make_async_search_memory()  # mode="state_aware" (default)
    memory.vector_store.search.return_value = [stored("m1", "likes hiking", 0.9, **SEARCH_PAYLOAD_EXTRAS)]
    memory.vector_store.keyword_search.return_value = []
    monkeypatch.setattr("mem0.memory.main.extract_entities", lambda _query: [])
    monkeypatch.setattr("mem0.memory.main.lemmatize_for_bm25", lambda query, **_kwargs: query.casefold())

    results = await memory._search_vector_store("hiking", {"user_id": "u1"}, 10, threshold=0.0)

    assert results
    for item in results:
        _assert_metadata_clean(item.get("metadata") or {})


# --- search: legacy --------------------------------------------------------


def test_legacy_search_excludes_internal_fields(monkeypatch):
    memory = make_search_memory(mode="legacy")
    memory.vector_store.search.return_value = [stored("m1", "likes hiking", 0.9, **SEARCH_PAYLOAD_EXTRAS)]
    memory.vector_store.keyword_search.return_value = []
    monkeypatch.setattr("mem0.memory.main.extract_entities", lambda _query: [])
    monkeypatch.setattr("mem0.memory.main.lemmatize_for_bm25", lambda query, **_kwargs: query.casefold())

    results = memory._search_vector_store("hiking", {"user_id": "u1"}, 10, threshold=0.0)

    assert results
    for item in results:
        _assert_metadata_clean(item.get("metadata") or {})


@pytest.mark.asyncio
async def test_async_legacy_search_excludes_internal_fields(monkeypatch):
    memory = make_async_search_memory(mode="legacy")
    memory.vector_store.search.return_value = [stored("m1", "likes hiking", 0.9, **SEARCH_PAYLOAD_EXTRAS)]
    memory.vector_store.keyword_search.return_value = []
    monkeypatch.setattr("mem0.memory.main.extract_entities", lambda _query: [])
    monkeypatch.setattr("mem0.memory.main.lemmatize_for_bm25", lambda query, **_kwargs: query.casefold())

    results = await memory._search_vector_store("hiking", {"user_id": "u1"}, 10, threshold=0.0)

    assert results
    for item in results:
        _assert_metadata_clean(item.get("metadata") or {})
