"""Recall-family scenarios (PA Task 6): Japanese lexical fallback, context-size budgets.

Legacy ordering (personal_acceptance/families.py's "legacy_ordering" entry) is pinned by
the existing tests/memory/test_state_aware_search.py and tests/utils/test_scoring.py
files, so no new test is added here for that family.
"""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

from mem0.configs.base import MemoryConfig, RecallConfig
from mem0.memory.lifecycle import reciprocal_rank_fusion, unicode_lexical_terms
from mem0.memory.main import Memory

# ===========================================================================
# Japanese lexical fallback family
# ===========================================================================
#
# Local copies of stored()/CompleteSearchVectorStore/make_memory from
# tests/memory/test_state_aware_search.py:11-47 -- kept local rather than
# imported across sibling test modules per the task brief.


def stored(memory_id, text, score, **payload):
    return SimpleNamespace(id=memory_id, score=score, payload={"data": text, **payload})


def make_memory(mode: Literal["state_aware", "legacy"] = "state_aware"):
    memory = Memory.__new__(Memory)
    memory.config = MemoryConfig(recall=RecallConfig(mode=mode))
    memory.embedding_model = MagicMock()
    memory.embedding_model.embed.return_value = [0.1, 0.2]
    memory.vector_store = MagicMock()
    memory._compute_entity_boosts = MagicMock(return_value={})
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


def test_lexical_unicode_ja_family(monkeypatch):
    # mem0/utils/lemmatization.py:28-36 imports get_nlp_lemma from
    # mem0.utils.spacy_models *inside* lemmatize_for_bm25, so patching the
    # accessor there (not mem0.utils.lemmatization.get_nlp_lemma, which the
    # task brief guessed) is what actually forces the no-spaCy branch that
    # falls back to unicode_lexical_terms.
    monkeypatch.setattr("mem0.utils.spacy_models.get_nlp_lemma", lambda: None)
    monkeypatch.setattr("mem0.memory.main.extract_entities", lambda _query: [])

    memory = make_memory()
    residence = stored(
        "residence",
        "ユーザーは京都に住んでいる",
        0.0,
        user_id="u1",
        topic="residence",
        _mem0_status="active",
        _mem0_schema_version=1,
    )
    tea = stored(
        "tea",
        "ユーザーは紅茶が好き",
        0.0,
        user_id="u1",
        topic="beverage",
        _mem0_status="active",
        _mem0_schema_version=1,
    )
    memory.vector_store = CompleteSearchVectorStore([residence, tea])

    results = memory._search_vector_store("京都", {"user_id": "u1"}, 10, threshold=0.0)

    # Only the residence row shares a lexical n-gram with the query; the tea
    # row never overlaps and must not appear (this fails if the CJK n-gram
    # branch at mem0/memory/lifecycle.py:121-124 regresses, since whole-string
    # word matching alone would match neither row against the 2-char query).
    assert [item["id"] for item in results] == ["residence"]
    metadata = results[0]["metadata"]
    assert metadata == {"topic": "residence"}
    assert not any(key.startswith("_mem0_") for key in metadata)

    # The real (unmocked) mem0.memory.main.lemmatize_for_bm25 ran with the
    # no-spaCy branch forced above -- pin that the vector store's
    # keyword_search actually received the unicode-fallback lemmatization of
    # the query, proving the real function executed that branch.
    assert memory.vector_store.keyword_search.call_args.kwargs["query"] == " ".join(unicode_lexical_terms("京都"))

    # Direct unit pin of the fallback itself (mem0/memory/lifecycle.py:108-126).
    assert "京都" in unicode_lexical_terms("京都に住む")


# ===========================================================================
# Context-size family
# ===========================================================================


def test_context_size_family(tmp_path):
    # --- (a) SDK cumulative budget: reciprocal_rank_fusion, mirroring
    # tests/memory/test_lifecycle.py:205-221's invocation pattern. -----------
    #
    # max_chars is set to the length of "first"'s text. Since a fully-consumed
    # budget leaves zero room for anything evaluated afterward, "first" is
    # ranked *last* here (list position sets RRF rank for a single channel) so
    # that "third" can still be evaluated -- and admitted -- while the budget
    # is still empty, after "second" is skipped for being over budget on its
    # own. This is exactly what distinguishes skip-not-break: a `break` on
    # the first over-budget candidate would leave `third` unevaluated too.
    first_text = "User keeps a Kyoto residence noted here for budget reference only"
    second_text = first_text + " plus extra padding that pushes this memory well past the allotted budget"
    third_text = "Osaka short note"
    max_chars = len(first_text)
    assert len(second_text) > max_chars
    assert len(third_text) <= max_chars

    channels = {
        "semantic": [
            stored("second", second_text, 0.9),
            stored("third", third_text, 0.8),
            stored("first", first_text, 0.7),
        ]
    }

    ranked = reciprocal_rank_fusion(
        channels,
        threshold=0.0,
        top_k=10,
        historical=False,
        max_chars=max_chars,
    )

    ranked_ids = [item["id"] for item in ranked]
    assert "second" not in ranked_ids  # over-budget on its own: excluded
    assert "third" in ranked_ids  # shorter, evaluated after the skip: admitted
    assert ranked_ids == ["third"]  # skip-not-break: only "third" fits at all

    # --- (b) bridge budget: FakeMem0 harness, replicated locally following
    # tests/personal_acceptance/test_scope_families.py's pattern (a distinct
    # sys.modules key, "pa_recall_bridge_module", avoids colliding with that
    # module's "pa_bridge_module" or tests/deployment/mem0_0_arch's own key
    # on whole-tree runs). -----------------------------------------------
    key_file = tmp_path / "bridge-key"
    key_file.write_text("fixture-only-key\n", encoding="utf-8")
    key_file.chmod(stat.S_IRUSR | stat.S_IWUSR)

    scope = {
        "user_id": "pa-recall-user",
        "agent_id": "pa-recall-agent",
        "machine_id": "arch-primary",
        "project_id": "pa-recall-tests",
        "data_class": "test-fixture",
    }
    long_text = "L" * 150  # exceeds the 100-char budget entirely by itself
    short_text = "short memory"
    assert len(long_text) > 100
    assert len(short_text) <= 100

    with fake_mem0_server() as (state, server):
        bridge = Mem0Bridge(api_url=f"http://127.0.0.1:{server.server_port}", api_key_file=key_file, timeout=1)
        bridge.call_tool("add_memory", {**scope, "content": long_text})
        bridge.call_tool("add_memory", {**scope, "content": short_text})

        result = bridge.call_tool("search_memories", {**scope, "query": "memory", "max_chars": 100})

        assert [row["memory"] for row in result["results"]] == [short_text]  # long omitted whole, short present
        assert result["omitted_for_budget"] == 1

        search_requests = [request for request in state.requests if request["path"] == "/search"]
        assert len(search_requests) == 1
        assert "max_chars" not in search_requests[0]["body"]  # budget enforced client-side, never sent upstream


# ===========================================================================
# FakeMem0 harness (bridge family), replicated locally from
# tests/personal_acceptance/test_scope_families.py:239-364.
# ===========================================================================


class FakeMem0State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.next_id = 1
        self.records: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []

    def record(self, method: str, path: str, *, body: Any = None) -> None:
        with self.lock:
            self.requests.append({"method": method, "path": path, "body": body})

    def add(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            memory_id = f"fixture-{self.next_id}"
            self.next_id += 1
            record = {
                "id": memory_id,
                "memory": body["messages"][0]["content"],
                "user_id": body.get("user_id"),
                "agent_id": body.get("agent_id"),
                "metadata": body.get("metadata", {}),
            }
            self.records[memory_id] = record
            return {"results": [{**record, "event": "ADD"}]}


class FakeMem0Handler(BaseHTTPRequestHandler):
    state: FakeMem0State

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(self, status: int, payload: dict[str, Any] | list[Any]) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length))

    def do_POST(self) -> None:
        body = self._body()
        self.state.record("POST", self.path, body=body)
        if self.path == "/memories":
            self._json(200, self.state.add(body))
            return
        if self.path == "/search":
            filters = body.get("filters", {})
            with self.state.lock:
                rows = [
                    record
                    for record in self.state.records.values()
                    if all(
                        record.get(key) == value or record.get("metadata", {}).get(key) == value
                        for key, value in filters.items()
                    )
                ]
            self._json(200, {"results": rows[: body.get("top_k", 20)]})
            return
        self._json(404, {"detail": "not found"})

    def do_GET(self) -> None:
        self.state.record("GET", self.path)
        parsed = urlparse(self.path)
        if parsed.path == "/openapi.json":
            self._json(200, {"openapi": "3.1.0"})
            return
        if parsed.path == "/ready":
            self._json(200, {"status": "ready"})
            return
        if parsed.path == "/memories":
            query = {key: value[-1] for key, value in parse_qs(parsed.query).items()}
            with self.state.lock:
                rows = [
                    record
                    for record in self.state.records.values()
                    if all(
                        record.get(key) == value or record.get("metadata", {}).get(key) == value
                        for key, value in query.items()
                        if key in {"user_id", "agent_id", "machine_id", "project_id", "data_class"}
                    )
                ]
            self._json(200, {"results": rows})
            return
        memory_id = parsed.path.rsplit("/", 1)[-1]
        with self.state.lock:
            record = self.state.records.get(memory_id)
        self._json(200 if record else 404, record or {"detail": "not found"})

    def do_PUT(self) -> None:
        body = self._body()
        self.state.record("PUT", self.path, body=body)
        memory_id = self.path.rsplit("/", 1)[-1]
        with self.state.lock:
            if memory_id not in self.state.records:
                self._json(404, {"detail": "not found"})
                return
            self.state.records[memory_id]["memory"] = body["text"]
        self._json(200, {"message": "Memory updated successfully!"})

    def do_DELETE(self) -> None:
        self.state.record("DELETE", self.path)
        memory_id = self.path.rsplit("/", 1)[-1]
        with self.state.lock:
            self.state.records.pop(memory_id, None)
        self._json(200, {"message": "Memory deleted successfully"})


@contextmanager
def fake_mem0_server():
    state = FakeMem0State()
    FakeMem0Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMem0Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# Load the mem0_0-arch bridge by file location, under a sys.modules key
# distinct from tests/personal_acceptance/test_scope_families.py's
# "pa_bridge_module" and tests/deployment/mem0_0_arch/test_bridge.py's
# "mem0_0_arch_bridge", so whole-tree runs never collide on the module cache.
_BRIDGE_PATH = Path(__file__).parents[2] / "deployment" / "mem0_0_arch" / "bridge" / "bridge.py"
_BRIDGE_SPEC = importlib.util.spec_from_file_location("pa_recall_bridge_module", _BRIDGE_PATH)
if _BRIDGE_SPEC is None or _BRIDGE_SPEC.loader is None:
    raise RuntimeError(f"unable to load bridge from {_BRIDGE_PATH}")
_bridge_module = importlib.util.module_from_spec(_BRIDGE_SPEC)
sys.modules[_BRIDGE_SPEC.name] = _bridge_module
_BRIDGE_SPEC.loader.exec_module(_bridge_module)

Mem0Bridge = _bridge_module.Mem0Bridge
