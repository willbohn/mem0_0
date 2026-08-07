import json
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock

import pytest

from mem0.configs.base import MemoryConfig
from mem0.memory.main import Memory


class InMemoryVectorStore:
    _mem0_supports_complete_list = True

    def __init__(self):
        self.records = {}

    def insert(self, vectors, ids, payloads):
        for memory_id, payload in zip(ids, payloads):
            self.records[str(memory_id)] = SimpleNamespace(id=str(memory_id), score=0.9, payload=dict(payload))

    def update(self, vector_id, vector, payload):
        self.records[str(vector_id)].payload = dict(payload)

    def delete(self, vector_id):
        self.records.pop(str(vector_id), None)

    def get(self, vector_id):
        return self.records.get(str(vector_id))

    def search(self, **_kwargs):
        return list(self.records.values())

    def keyword_search(self, **_kwargs):
        return list(self.records.values())

    def list(self, **_kwargs):
        return list(self.records.values()), None


def extraction_json(*items) -> str:
    """Build a scripted LLM extraction response containing the given items."""
    return json.dumps({"memory": list(items)})


def extraction_item(
    text,
    value,
    *,
    kind="state",
    subject="user",
    predicate="residence",
    excerpt=None,
    relation: Optional[str] = "supersedes",
    extra=None,
) -> dict:
    """Build one extraction-JSON memory item with grounded evidence."""
    item = {
        "id": "0",
        "text": text,
        "kind": kind,
        "subject": subject,
        "predicate": predicate,
        "value": value,
        "attributed_to": "user",
        "evidence": {"message_index": 0, "excerpt": excerpt or value},
    }
    if relation is not None:
        item["relation"] = relation
    item.update(extra or {})
    return item


@pytest.fixture
def scripted_memory(monkeypatch):
    """Factory fixture building an in-process Memory wired to scripted LLM responses."""

    def _build(*responses: str) -> Memory:
        memory = Memory.__new__(Memory)
        memory.config = MemoryConfig()
        memory.custom_instructions = None
        memory.api_version = "v1.1"
        memory.llm = MagicMock()
        memory.llm.generate_response.side_effect = list(responses)
        memory.embedding_model = MagicMock()
        memory.embedding_model.embed.return_value = [0.1, 0.2]
        memory.embedding_model.embed_batch.side_effect = lambda texts, _action: [[0.1, 0.2] for _ in texts]
        memory.vector_store = InMemoryVectorStore()
        memory.db = MagicMock()
        memory.db.get_last_messages.return_value = []
        memory._entity_store = None
        memory.reranker = None

        monkeypatch.setattr("mem0.memory.main.capture_event", lambda *_args, **_kwargs: None)
        monkeypatch.setattr("mem0.memory.main.display_first_run_notice", lambda *_args, **_kwargs: None)
        monkeypatch.setattr("mem0.memory.main.extract_entities", lambda _text: [])
        return memory

    return _build
