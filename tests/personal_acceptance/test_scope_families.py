"""Scope-isolation scenario families (PA Task 5): Python, REST, MCP.

Each family drives a different surface of the stack -- the in-process
``Memory`` class, the FastAPI server, and the mem0_0-arch MCP bridge -- through
the same question: can one tenant/scope ever see or write another's data.
These tests PIN verified existing isolation behavior; each pinned assertion
cites the file:line it pins.
"""

import importlib
import importlib.util
import json
import os
import stat
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient

from tests.personal_acceptance.conftest import InMemoryVectorStore

# server/ modules use bare imports (from auth import ...), so the server
# directory itself must be importable, mirroring how it runs in Docker
# (documented at tests/test_api_keys_router.py:22-26).
_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

# Load the mem0_0-arch bridge by file location, under a sys.modules key
# distinct from tests/deployment/mem0_0_arch/test_bridge.py's "mem0_0_arch_bridge"
# so whole-tree runs never collide on the module cache.
BRIDGE_PATH = Path(__file__).parents[2] / "deployment" / "mem0_0_arch" / "bridge" / "bridge.py"
_BRIDGE_SPEC = importlib.util.spec_from_file_location("pa_bridge_module", BRIDGE_PATH)
if _BRIDGE_SPEC is None or _BRIDGE_SPEC.loader is None:
    raise RuntimeError(f"unable to load bridge from {BRIDGE_PATH}")
_bridge_module = importlib.util.module_from_spec(_BRIDGE_SPEC)
sys.modules[_BRIDGE_SPEC.name] = _bridge_module
_BRIDGE_SPEC.loader.exec_module(_bridge_module)

BridgeError = _bridge_module.BridgeError
Mem0Bridge = _bridge_module.Mem0Bridge


# ===========================================================================
# Python family
# ===========================================================================


class _FilteringVectorStore(InMemoryVectorStore):
    """Filters list() by exact-match payload fields.

    The conftest double (InMemoryVectorStore.list) intentionally ignores its
    filters argument and returns every record -- real vector stores filter
    server-side. This local subclass reproduces that filtering so Memory.get_all()
    can be exercised for cross-tenant isolation the way a real backend would.
    """

    def list(self, filters=None, **_kwargs):
        filters = filters or {}
        matches = [
            record for record in self.records.values() if all(record.payload.get(k) == v for k, v in filters.items())
        ]
        return matches, None


def test_scope_python_family(scripted_memory):
    memory = scripted_memory()
    memory.vector_store = _FilteringVectorStore()

    memory.add([{"role": "user", "content": "tenant-a event"}], user_id="tenant-a", infer=False)
    memory.add([{"role": "user", "content": "tenant-b event"}], user_id="tenant-b", infer=False)

    tenant_a = memory.get_all(filters={"user_id": "tenant-a"})["results"]
    tenant_b = memory.get_all(filters={"user_id": "tenant-b"})["results"]
    assert [item["memory"] for item in tenant_a] == ["tenant-a event"]
    assert [item["memory"] for item in tenant_b] == ["tenant-b event"]
    assert not any(item["user_id"] == "tenant-b" for item in tenant_a)  # zero cross-scope rows, a -> b
    assert not any(item["user_id"] == "tenant-a" for item in tenant_b)  # zero cross-scope rows, b -> a

    # Identity-injection attempt: caller is scoped to tenant-a but tries to plant
    # a row into tenant-b's scope via metadata. The stabilized identity-strip
    # (mem0/memory/main.py:167-180, applied via _build_filters_and_metadata at
    # mem0/memory/main.py:637-641) drops the metadata-supplied user_id before it
    # ever reaches the stored payload -- the actual user_id kwarg wins instead.
    memory.add(
        [{"role": "user", "content": "spoof attempt"}],
        user_id="tenant-a",
        run_id="r1",
        metadata={"user_id": "tenant-b"},
        infer=False,
    )

    tenant_b_after = memory.get_all(filters={"user_id": "tenant-b"})["results"]
    tenant_a_after = memory.get_all(filters={"user_id": "tenant-a"})["results"]
    assert [item["memory"] for item in tenant_b_after] == ["tenant-b event"]  # unchanged: injection never landed
    assert [item["memory"] for item in tenant_a_after] == ["tenant-a event", "spoof attempt"]
    assert all(item["user_id"] == "tenant-a" for item in tenant_a_after)


# ===========================================================================
# REST family
# ===========================================================================


@pytest.fixture
def _mock_memory():
    """Patch Memory.from_config so the server imports without a real backend."""
    mock_instance = MagicMock()
    mock_instance.add.return_value = {"results": [{"id": "mem-1", "event": "ADD", "memory": "test"}]}
    mock_instance.search.return_value = [{"id": "mem-1", "memory": "test", "score": 0.9}]
    mock_instance.get.return_value = {"id": "mem-1", "memory": "test memory"}
    mock_instance.get_all.return_value = [{"id": "mem-1", "memory": "test memory"}]
    mock_instance.update.return_value = {"message": "Memory updated"}
    mock_instance.history.return_value = [{"id": "mem-1", "old_memory": "a", "new_memory": "b"}]
    mock_instance.audit.return_value = {"summary": {"total": 1}, "issues": []}
    mock_instance.consolidate.return_value = {"dry_run": True, "strategy": "safe", "actions": []}
    mock_instance.feedback.return_value = {
        "message": "Feedback recorded successfully!",
        "memory_id": "mem-1",
        "signal": "helpful",
    }
    mock_instance.delete.return_value = None
    mock_instance.delete_all.return_value = {"message": "Memories deleted"}
    mock_instance.reset.return_value = None

    with patch.dict(
        os.environ,
        {"OPENAI_API_KEY": "fake-key", "ADMIN_API_KEY": "", "AUTH_DISABLED": "true"},
    ):
        with patch("mem0.Memory.from_config", return_value=mock_instance):
            yield mock_instance


@pytest.fixture
def client(_mock_memory, monkeypatch, request):
    """Return a TestClient wired to the server app with mocked Memory."""
    # auth.AUTH_DISABLED and auth.ADMIN_API_KEY are module-level constants read
    # once at import time -- whichever test imports `auth` FIRST in the
    # process permanently bakes its own environment into that cached module,
    # and neither a raw importlib.reload() nor monkeypatch.setattr on an
    # already-cached module undoes that for a *later* first-time importer.
    # So: (1) always force the values this fixture needs via monkeypatch,
    # scoped to this fixture and reverted automatically at teardown, and
    # (2) if `auth` / `server.main` were not already cached before this
    # fixture ran, drop them from sys.modules at teardown too, so the next
    # test to import them gets a genuinely fresh read of its own environment
    # instead of inheriting ours (see tests/test_server_auth.py, which is
    # not scope-isolation-related but shares this module-caching hazard).
    auth_was_cached = "auth" in sys.modules
    server_main_was_cached = "server.main" in sys.modules

    # Import (or fetch the cached) `auth` module and patch it *before* ever
    # touching server.main: server/main.py raises at import time if it sees
    # AUTH_DISABLED=False and no JWT_SECRET (server/main.py:92-95), and that
    # check fires on the very first `import server.main` statement below --
    # patching `auth` only after that first import would be too late.
    auth_module = importlib.import_module("auth")
    monkeypatch.setattr(auth_module, "AUTH_DISABLED", True)
    monkeypatch.setattr(auth_module, "ADMIN_API_KEY", "")

    import server.main as server_main

    with patch.dict(os.environ, {"ADMIN_API_KEY": "", "AUTH_DISABLED": "true"}):
        importlib.reload(server_main)
    monkeypatch.setattr(
        server_main,
        "_provider_status",
        lambda: {"status": "ready", "code": "ready", "message": "Provider is ready."},
    )
    monkeypatch.setattr(server_main, "_should_log_request", lambda _request: False)
    server_main.app.dependency_overrides[server_main.require_admin] = lambda: SimpleNamespace(role="admin")

    def _drop_freshly_cached_modules():
        if not auth_was_cached:
            sys.modules.pop("auth", None)
        if not server_main_was_cached:
            sys.modules.pop("server.main", None)

    request.addfinalizer(_drop_freshly_cached_modules)
    return TestClient(server_main.app)


@pytest.fixture
def mock_memory(_mock_memory):
    return _mock_memory


def test_scope_rest_family(client, mock_memory):
    # Admin listing (server/main.py:538-558, _serialize_memory at 511-529): raw
    # vector_store.list() rows must have text_lemmatized and every _mem0_* key
    # stripped from the caller-visible metadata, while ordinary metadata survives.
    admin_row = SimpleNamespace(
        id="mem-1",
        payload={
            "data": "admin visible text",
            "user_id": "tenant-a",
            "text_lemmatized": "admin visible text lemma",
            "_mem0_status": "active",
            "_mem0_kind": "state",
            "topic": "billing",
        },
    )
    mock_memory.vector_store.list.return_value = [admin_row]

    admin_resp = client.get("/memories")
    assert admin_resp.status_code == 200
    admin_results = admin_resp.json()["results"]
    assert len(admin_results) == 1
    metadata = admin_results[0]["metadata"]
    assert "text_lemmatized" not in metadata
    assert "_mem0_status" not in metadata
    assert not any(key.startswith("_mem0_") for key in metadata)
    assert metadata == {"topic": "billing"}

    # Scoped request (server/main.py:559-575): forwards exact filters to
    # Memory.get_all rather than falling through to the raw admin listing.
    scoped_resp = client.get("/memories", params={"user_id": "tenant-a"})
    assert scoped_resp.status_code == 200
    assert mock_memory.get_all.call_args.kwargs["filters"] == {"user_id": "tenant-a"}
    assert mock_memory.vector_store.list.call_count == 1  # scoped path never touches the raw admin listing


# ===========================================================================
# MCP family
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


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "bridge-key"
    path.write_text("fixture-only-key\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def test_scope_mcp_family(key_file: Path, monkeypatch) -> None:
    monkeypatch.delenv("MEM0_API_KEY", raising=False)

    scope_a = {
        "user_id": "fixture-user",
        "agent_id": "fixture-agent",
        "machine_id": "arch-primary",
        "project_id": "bridge-tests",
        "data_class": "test-fixture",
    }
    scope_b = {**scope_a, "project_id": "other-project"}

    with fake_mem0_server() as (_state, server):
        bridge = Mem0Bridge(api_url=f"http://127.0.0.1:{server.server_port}", api_key_file=key_file, timeout=1)

        added_a = bridge.call_tool("add_memory", {**scope_a, "content": "scope-a fixture memory"})
        added_b = bridge.call_tool("add_memory", {**scope_b, "content": "scope-b fixture memory"})
        id_a = added_a["results"][0]["id"]
        id_b = added_b["results"][0]["id"]
        assert id_a != id_b

        # search_memories / get_memories under scope A never surface scope B's row
        # (bridge.py:314-359: filtered server-side by REST filters/query, then
        # re-checked client-side by _row_in_scope at bridge.py:277-282).
        searched_a = bridge.call_tool("search_memories", {**scope_a, "query": "fixture memory"})
        listed_a = bridge.call_tool("get_memories", {**scope_a})
        assert {row["id"] for row in searched_a["results"]} == {id_a}
        assert {row["id"] for row in listed_a["results"]} == {id_a}

        # ... and the reverse direction: scope B's own listing never surfaces A's row.
        searched_b = bridge.call_tool("search_memories", {**scope_b, "query": "fixture memory"})
        listed_b = bridge.call_tool("get_memories", {**scope_b})
        assert {row["id"] for row in searched_b["results"]} == {id_b}
        assert {row["id"] for row in listed_b["results"]} == {id_b}

        # get_memory on B's ID from scope A raises scope_mismatch (bridge.py:361-364,
        # 284-288: the single-record REST fetch is never scope-filtered server-side,
        # so the bridge's own _check_row is the enforcement point).
        with pytest.raises(BridgeError) as mismatched:
            bridge.call_tool("get_memory", {**scope_a, "memory_id": id_b})
        assert mismatched.value.code == "scope_mismatch"

        # delete_memory without confirm raises confirmation_required (bridge.py:374-379).
        with pytest.raises(BridgeError) as unconfirmed:
            bridge.call_tool("delete_memory", {**scope_a, "memory_id": id_a})
        assert unconfirmed.value.code == "confirmation_required"

        # A secret-bearing query raises secret_detected before any request is made
        # (bridge.py:97-104, _require_text -> _reject_secrets).
        with pytest.raises(BridgeError) as secret:
            bridge.call_tool("search_memories", {**scope_a, "query": "token sk-fixture-not-real-123456789"})
        assert secret.value.code == "secret_detected"
