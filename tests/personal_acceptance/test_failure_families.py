"""Failure-path scenario families (PA Task 4): malformed extraction, persistence atomicity.

Each test drives an in-process ``Memory`` instance (scripted LLM, in-memory
vector store) through a canonical failure scenario. The atomicity assertions
PIN verified existing behavior from ``mem0/memory/main.py`` -- they assert
what the code actually does, not what it "should" do; each pinned assertion
cites the file:line it pins.
"""

import json
from types import SimpleNamespace

import pytest

from mem0.exceptions import EmbeddingError, ExtractionParseError, MemoryPersistenceError
from tests.personal_acceptance.conftest import InMemoryVectorStore, extraction_item, extraction_json


class _PartialInsertVectorStore(InMemoryVectorStore):
    """Persists the first planned record, then fails the batch insert call.

    Used to prove that compensating deletes (mem0/memory/main.py:1550-1560)
    actually restore an empty store, not just that an exception was raised.
    """

    def __init__(self):
        super().__init__()
        self.delete_calls = []

    def insert(self, vectors, ids, payloads):
        first_id, first_payload = ids[0], payloads[0]
        self.records[str(first_id)] = SimpleNamespace(id=str(first_id), score=0.9, payload=dict(first_payload))
        raise RuntimeError("provider inserted one record then failed")

    def delete(self, vector_id):
        self.delete_calls.append(str(vector_id))
        super().delete(vector_id)


def test_malformed_extraction_family(scripted_memory):
    # Non-object response -> typed parse error, nothing persisted.
    # Raising code: mem0/memory/main.py:248-250 (_parse_extraction_response).
    memory = scripted_memory('"bare string"')
    with pytest.raises(ExtractionParseError):
        memory.add([{"role": "user", "content": "hello"}], user_id="u1")
    assert memory.vector_store.records == {}

    # Mixed valid/invalid items -> valid persisted, skip count disclosed.
    # Skip accounting: mem0/memory/main.py:1325-1348.
    memory = scripted_memory(
        json.dumps(
            {
                "memory": [
                    "junk",
                    42,
                    extraction_item(
                        "User prefers tea",
                        "tea",
                        kind="preference",
                        predicate="drink",
                        relation=None,
                        excerpt="prefer tea",
                    ),
                ]
            }
        )
    )
    response = memory.add([{"role": "user", "content": "I prefer tea"}], user_id="u1")
    assert response["skipped_invalid_extractions"] == 2
    assert len(response["results"]) == 1


def test_persistence_atomicity_family(scripted_memory):
    # Embedding failure mid-batch must not leave partial writes
    # (complete-embedding-before-persistence, mem0/memory/main.py:1394-1401):
    # embed_batch raising is caught and re-raised as EmbeddingError before any
    # record is ever built or inserted.
    memory = scripted_memory(
        extraction_json(
            extraction_item("User lives in Toyama", "Toyama", relation=None),
            extraction_item("User works remotely", "remote", predicate="workstyle", relation=None, excerpt="Toyama"),
        )
    )
    memory.embedding_model.embed_batch.side_effect = RuntimeError("embedder down")
    with pytest.raises(EmbeddingError, match="complete embedding batch"):
        memory.add([{"role": "user", "content": "I moved to Toyama"}], user_id="u1")
    assert memory.vector_store.records == {}

    # Partial insert (one record actually written) then failure -> compensating
    # deletes for every planned ID restore an empty store, and the raised error
    # discloses every planned ID (mem0/memory/main.py:1547-1560).
    memory = scripted_memory(
        extraction_json(
            extraction_item("User lives in Kobe", "Kobe", relation=None),
            extraction_item("User works remotely", "remote", predicate="workstyle", relation=None, excerpt="Kobe"),
        )
    )
    store = _PartialInsertVectorStore()
    memory.vector_store = store
    with pytest.raises(MemoryPersistenceError, match="rolled back") as exc_info:
        memory.add([{"role": "user", "content": "I moved to Kobe"}], user_id="u1")

    assert len(store.delete_calls) == 2
    assert exc_info.value.details["planned_ids"] == store.delete_calls
    assert store.records == {}
