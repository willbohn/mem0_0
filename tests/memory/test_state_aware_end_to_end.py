import json
from types import SimpleNamespace
from unittest.mock import MagicMock

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


def _extraction(text, value, excerpt):
    return json.dumps(
        {
            "memory": [
                {
                    "id": "0",
                    "text": text,
                    "kind": "state",
                    "subject": "user",
                    "predicate": "residence",
                    "value": value,
                    "attributed_to": "user",
                    "evidence": {"message_index": 0, "excerpt": excerpt},
                    "relation": "supersedes",
                }
            ]
        }
    )


def test_add_correction_current_historical_audit_and_consolidate(monkeypatch):
    memory = Memory.__new__(Memory)
    memory.config = MemoryConfig()
    memory.custom_instructions = None
    memory.api_version = "v1.1"
    memory.llm = MagicMock()
    memory.llm.generate_response.side_effect = [
        _extraction("User lives in Tokyo", "Tokyo", "live in Tokyo"),
        _extraction("User moved from Tokyo to Kyoto", "Kyoto", "moved from Tokyo to Kyoto"),
    ]
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

    first = memory.add(
        [{"role": "user", "content": "I live in Tokyo"}],
        user_id="u1",
        metadata={"created_at": "2024-01-01T00:00:00Z"},
    )
    correction = memory.add(
        [{"role": "user", "content": "I moved from Tokyo to Kyoto"}],
        user_id="u1",
        metadata={"created_at": "2025-01-01T00:00:00Z"},
    )

    current = memory.search("Where do I live now?", filters={"user_id": "u1"}, threshold=0.0)
    historical = memory.search(
        "Where did I live on 2024-06-01?",
        filters={"user_id": "u1"},
        threshold=0.0,
    )
    audit = memory.audit({"user_id": "u1"})
    consolidation = memory.consolidate({"user_id": "u1"})

    assert first["results"][0]["event"] == "ADD"
    assert correction["results"][0]["event"] == "ADD"
    assert [item["memory"] for item in current["results"]] == ["User moved from Tokyo to Kyoto"]
    assert [item["memory"] for item in historical["results"]] == ["User lives in Tokyo"]
    assert audit["summary"]["stale_active_predecessors"] == 0
    assert consolidation == {"dry_run": True, "strategy": "safe", "actions": []}
    assert all("_mem0_status" not in repr(item) for item in current["results"] + historical["results"])
