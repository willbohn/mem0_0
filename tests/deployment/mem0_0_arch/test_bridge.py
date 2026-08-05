from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest


BRIDGE_PATH = Path(__file__).parents[3] / "deployment" / "mem0_0_arch" / "bridge" / "bridge.py"
SPEC = importlib.util.spec_from_file_location("mem0_0_arch_bridge", BRIDGE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"unable to load bridge from {BRIDGE_PATH}")
bridge_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge_module
SPEC.loader.exec_module(bridge_module)

BridgeError = bridge_module.BridgeError
Mem0Bridge = bridge_module.Mem0Bridge


def test_canonical_bridge_defaults_to_isolated_shadow_port(key_file: Path, monkeypatch) -> None:
    monkeypatch.delenv("MEM0_API_URL", raising=False)
    monkeypatch.delenv("MEM0_API_KEY", raising=False)

    bridge = Mem0Bridge(api_key_file=key_file)

    assert bridge.api_url == "http://127.0.0.1:28889"


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

    def log_message(self, *_args: Any) -> None:
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
            if body.get("query") == "provider readiness":
                self._json(503, {"code": "model_missing", "detail": "configured model is unavailable"})
                return
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
            self._json(503, {"code": "model_missing", "detail": "configured model is unavailable"})
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


@pytest.fixture
def scope() -> dict[str, str]:
    return {
        "user_id": "fixture-user",
        "agent_id": "fixture-agent",
        "machine_id": "arch-primary",
        "project_id": "bridge-tests",
        "data_class": "test-fixture",
    }


def test_runtime_identity_and_tool_names_are_canonical(key_file: Path) -> None:
    bridge = Mem0Bridge(api_key_file=key_file)
    initialized = bridge.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    listed = bridge.handle_jsonrpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})

    assert initialized["result"]["serverInfo"] == {"name": "mem0_0-arch", "version": "0.1.0"}
    assert [tool["name"] for tool in listed["result"]["tools"]] == [
        "add_memory",
        "search_memories",
        "get_memories",
        "get_memory",
        "update_memory",
        "delete_memory",
    ]


def test_mem0_environment_and_file_only_key_policy(key_file: Path) -> None:
    with fake_mem0_server() as (_state, server):
        env = {
            "MEM0_API_URL": f"http://127.0.0.1:{server.server_port}/",
            "MEM0_API_KEY_FILE": str(key_file),
        }
        with patch.dict(os.environ, env, clear=True):
            bridge = Mem0Bridge(timeout=1)
            assert bridge.self_test() == {
                "status": "degraded",
                "api": "ok",
                "provider": "model_missing",
                "credential_source": "file",
            }

        with patch.dict(os.environ, {**env, "MEM0_API_KEY": "inline-fixture"}, clear=True):
            with pytest.raises(BridgeError, match="inline API keys are forbidden") as raised:
                Mem0Bridge(timeout=1)
        assert raised.value.code == "inline_credentials_forbidden"


def test_default_key_path_uses_mem0_0_arch_identity(key_file: Path) -> None:
    with patch.dict(os.environ, {}, clear=True), patch.object(Mem0Bridge, "_load_key", return_value="fixture"):
        bridge = Mem0Bridge()
    assert bridge.api_key_file == Path("/home/will/.config/mem0_0-arch/bridge-api-key")


def test_requires_five_explicit_scope_fields_and_rejects_secret_content(key_file: Path, scope: dict[str, str]) -> None:
    bridge = Mem0Bridge(api_key_file=key_file)
    incomplete = {**scope, "content": "safe fixture"}
    del incomplete["project_id"]
    with pytest.raises(BridgeError, match="project_id") as missing:
        bridge.call_tool("add_memory", incomplete)
    assert missing.value.code == "invalid_scope"

    with pytest.raises(BridgeError) as wildcard:
        bridge.call_tool("get_memories", {**scope, "project_id": "*"})
    assert wildcard.value.code == "invalid_scope"

    secret = "sk-fixture-not-real-123456789"
    with pytest.raises(BridgeError) as rejected:
        bridge.call_tool("add_memory", {**scope, "content": f"token={secret}"})
    assert rejected.value.code == "secret_detected"
    assert secret not in str(rejected.value)


def test_rest_paths_envelopes_scope_isolation_and_crud(key_file: Path, scope: dict[str, str]) -> None:
    with fake_mem0_server() as (state, server):
        bridge = Mem0Bridge(api_url=f"http://127.0.0.1:{server.server_port}", api_key_file=key_file, timeout=1)
        added = bridge.call_tool(
            "add_memory", {**scope, "content": "blue bicycle fixture", "metadata": {"source": "test"}}
        )
        memory_id = added["results"][0]["id"]
        other_scope = {**scope, "agent_id": "other-agent", "project_id": "other-project"}
        other = bridge.call_tool("add_memory", {**other_scope, "content": "other fixture"})

        searched = bridge.call_tool("search_memories", {**scope, "query": "blue bicycle", "top_k": 7})
        listed = bridge.call_tool("get_memories", {**scope, "top_k": 9})
        fetched = bridge.call_tool("get_memory", {**scope, "memory_id": memory_id})
        updated = bridge.call_tool("update_memory", {**scope, "memory_id": memory_id, "content": "updated fixture"})

        assert searched == {"results": [fetched]}
        assert {row["id"] for row in listed["results"]} == {memory_id}
        list_request = next(
            request
            for request in state.requests
            if request["method"] == "GET" and request["path"].startswith("/memories?")
        )
        assert all(f"{key}={value}" in list_request["path"] for key, value in scope.items())
        assert updated == {"message": "Memory updated successfully!"}
        with pytest.raises(BridgeError) as mismatched:
            bridge.call_tool("get_memory", {**scope, "memory_id": other["results"][0]["id"]})
        assert mismatched.value.code == "scope_mismatch"

        with pytest.raises(BridgeError) as unconfirmed:
            bridge.call_tool("delete_memory", {**scope, "memory_id": memory_id})
        assert unconfirmed.value.code == "confirmation_required"
        deleted = bridge.call_tool("delete_memory", {**scope, "memory_id": memory_id, "confirm": True})
        assert deleted == {"message": "Memory deleted successfully"}

        post_bodies = [request["body"] for request in state.requests if request["method"] == "POST"]
        assert post_bodies[0] == {
            "messages": [{"role": "user", "content": "blue bicycle fixture"}],
            "user_id": "fixture-user",
            "agent_id": "fixture-agent",
            "metadata": {**scope, "source": "test"},
            "infer": False,
        }
        assert post_bodies[2] == {"query": "blue bicycle", "filters": scope, "top_k": 7}
        assert any(
            request == {"method": "PUT", "path": f"/memories/{memory_id}", "body": {"text": "updated fixture"}}
            for request in state.requests
        )
        assert state.requests[-1] == {"method": "DELETE", "path": f"/memories/{memory_id}", "body": None}


def test_key_permissions_upstream_errors_and_mcp_error_shape(
    tmp_path: Path, key_file: Path, scope: dict[str, str]
) -> None:
    insecure = tmp_path / "insecure-key"
    insecure.write_text("fixture-only-key\n", encoding="utf-8")
    insecure.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    with pytest.raises(BridgeError, match="0600") as raised:
        Mem0Bridge(api_key_file=insecure)
    assert raised.value.code == "credential_file_error"

    unavailable = Mem0Bridge(api_url="http://127.0.0.1:1", api_key_file=key_file, timeout=0.1)
    result = unavailable.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_memories", "arguments": scope},
        }
    )
    assert result["result"]["isError"] is True
    assert json.loads(result["result"]["content"][0]["text"]) == {
        "code": "upstream_unavailable",
        "error": "Mem0 local REST service is unavailable",
    }

    with fake_mem0_server() as (_state, server):
        bridge = Mem0Bridge(api_url=f"http://127.0.0.1:{server.server_port}", api_key_file=key_file, timeout=1)
        provider = bridge.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "search_memories", "arguments": {**scope, "query": "provider readiness"}},
            }
        )
    assert provider["result"]["isError"] is True
    assert json.loads(provider["result"]["content"][0]["text"])["code"] == "model_missing"


def test_mcp_ping_and_invalid_scope_are_protocol_compatible(key_file: Path) -> None:
    bridge = Mem0Bridge(api_key_file=key_file)
    assert bridge.handle_jsonrpc({"jsonrpc": "2.0", "id": 5, "method": "ping", "params": {}}) == {
        "jsonrpc": "2.0",
        "id": 5,
        "result": {},
    }
    result = bridge.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "get_memories", "arguments": {"user_id": "fixture-user"}},
        }
    )
    assert result["result"]["isError"] is True
    assert json.loads(result["result"]["content"][0]["text"])["code"] == "invalid_scope"
