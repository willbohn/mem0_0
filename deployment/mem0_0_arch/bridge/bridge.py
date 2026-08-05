#!/usr/bin/env python3
"""Policy-enforcing MCP stdio bridge for the mem0_0 Arch deployment.

Only this process reads the Mem0 API key, from a mode-0600 file. The key is
never accepted inline in a harness configuration and is never included in
memory payloads, logs, errors, or MCP responses.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


LOG = logging.getLogger("mem0_0-arch")
MAX_BODY_BYTES = 512 * 1024
MAX_LINE_BYTES = 128 * 1024
MAX_CONTENT_CHARS = 16 * 1024
MAX_QUERY_CHARS = 4 * 1024
MAX_RESULTS = 100
PROVIDER_READINESS_CODES = {
    "provider_unavailable",
    "provider_invalid_response",
    "model_missing",
    "model_not_loaded",
}
REQUIRED_SCOPE = ("user_id", "agent_id", "machine_id", "project_id", "data_class")
MANAGED_METADATA = {"user_id", "agent_id", "machine_id", "project_id", "data_class"}
MEMORY_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")

SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}\b", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*[^\s,]{8,}",
        re.IGNORECASE,
    ),
    re.compile(r"\bcookie\s*:", re.IGNORECASE),
    re.compile(r"\b(?:postgres(?:ql)?|mysql|redis)://[^\s]+", re.IGNORECASE),
)


class BridgeError(Exception):
    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class Scope:
    user_id: str
    agent_id: str
    machine_id: str
    project_id: str
    data_class: str

    def as_dict(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "machine_id": self.machine_id,
            "project_id": self.project_id,
            "data_class": self.data_class,
        }


def _contains_secret(value: Any) -> bool:
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in SECRET_PATTERNS)
    if isinstance(value, dict):
        return any(_contains_secret(key) or _contains_secret(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item) for item in value)
    return False


def _reject_secrets(value: Any) -> None:
    if _contains_secret(value):
        raise BridgeError("secret_detected", "payload contains credential-like material")


def _require_text(args: dict[str, Any], key: str, maximum: int) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BridgeError("invalid_argument", f"{key} must be a non-empty string")
    if len(value) > maximum:
        raise BridgeError("invalid_argument", f"{key} exceeds the maximum permitted length")
    _reject_secrets(value)
    return value


def _scope(args: dict[str, Any]) -> Scope:
    values: dict[str, str] = {}
    for field in REQUIRED_SCOPE:
        value = args.get(field)
        if not isinstance(value, str) or not value.strip():
            raise BridgeError("invalid_scope", f"scope requires non-empty {field}")
        value = value.strip()
        if value in {"*", "all", "any", "unknown"}:
            raise BridgeError("invalid_scope", f"scope {field} must be explicit")
        if len(value) > 256:
            raise BridgeError("invalid_scope", f"scope {field} exceeds the maximum permitted length")
        values[field] = value
    _reject_secrets(values)
    return Scope(**values)


def _metadata(scope: Scope, value: Any) -> dict[str, Any]:
    if value is None:
        supplied: dict[str, Any] = {}
    elif isinstance(value, dict):
        supplied = dict(value)
    else:
        raise BridgeError("invalid_argument", "metadata must be an object")
    _reject_secrets(supplied)
    for field in MANAGED_METADATA:
        if field in supplied and supplied[field] != getattr(scope, field):
            raise BridgeError("invalid_scope", f"metadata cannot override {field}")
        supplied.pop(field, None)
    supplied.update(scope.as_dict())
    return supplied


def _memory_id(args: dict[str, Any]) -> str:
    value = _require_text(args, "memory_id", 256)
    if not MEMORY_ID_RE.fullmatch(value):
        raise BridgeError("invalid_argument", "memory_id contains unsupported characters")
    return value


def _rows(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, dict) and isinstance(response.get("results"), list):
        return [row for row in response["results"] if isinstance(row, dict)]
    if isinstance(response, list):
        return [row for row in response if isinstance(row, dict)]
    raise BridgeError("invalid_upstream_response", "Mem0 returned an unexpected result shape")


class Mem0Bridge:
    TOOLS = (
        (
            "add_memory",
            "Store one scoped memory.",
            {"content": {"type": "string"}, "metadata": {"type": "object"}, "infer": {"type": "boolean"}},
        ),
        (
            "search_memories",
            "Search only within one explicit scope.",
            {"query": {"type": "string"}, "top_k": {"type": "integer"}},
        ),
        ("get_memories", "List only memories within one explicit scope.", {"top_k": {"type": "integer"}}),
        ("get_memory", "Get one memory by explicit ID within one scope.", {"memory_id": {"type": "string"}}),
        (
            "update_memory",
            "Update one memory by explicit ID within one scope.",
            {"memory_id": {"type": "string"}, "content": {"type": "string"}},
        ),
        (
            "delete_memory",
            "Delete one memory by explicit ID after confirmation.",
            {"memory_id": {"type": "string"}, "confirm": {"type": "boolean"}},
        ),
    )

    def __init__(
        self,
        *,
        api_url: str | None = None,
        api_key_file: str | Path | None = None,
        timeout: float = 10.0,
        max_results: int = 50,
    ) -> None:
        if os.environ.get("MEM0_API_KEY"):
            raise BridgeError("inline_credentials_forbidden", "set MEM0_API_KEY_FILE; inline API keys are forbidden")
        self.api_url = (api_url or os.environ.get("MEM0_API_URL", "http://127.0.0.1:28889")).rstrip("/")
        self.api_key_file = Path(
            api_key_file or os.environ.get("MEM0_API_KEY_FILE", "/home/will/.config/mem0_0-arch/bridge-api-key")
        )
        self.timeout = max(0.1, min(float(timeout), 60.0))
        self.max_results = max(1, min(int(max_results), MAX_RESULTS))
        self.api_key = self._load_key()

    def self_test(self) -> dict[str, str]:
        report = {"status": "ok", "api": "ok", "provider": "unknown", "credential_source": "file"}
        try:
            self._request("GET", "/openapi.json")
        except BridgeError as exc:
            report["status"] = "degraded"
            report["api"] = exc.code
            return report
        try:
            self._request("GET", "/ready")
            report["provider"] = "ready"
        except BridgeError as exc:
            report["status"] = "degraded"
            report["provider"] = exc.code
        return report

    def _load_key(self) -> str:
        try:
            mode = self.api_key_file.stat().st_mode
        except OSError as exc:
            raise BridgeError("credential_file_error", "Mem0 API key file is unavailable") from exc
        if mode & 0o077:
            raise BridgeError("credential_file_error", "Mem0 API key file must be mode 0600")
        try:
            key = self.api_key_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise BridgeError("credential_file_error", "Mem0 API key file cannot be read") from exc
        if not key or "\n" in key or len(key) > 4096:
            raise BridgeError("credential_file_error", "Mem0 API key file has an invalid format")
        return key

    def _request(self, method: str, path: str, *, body: Any = None, query: dict[str, Any] | None = None) -> Any:
        url = f"{self.api_url}{path}"
        if query:
            url = f"{url}?{urlencode({key: str(value) for key, value in query.items()})}"
        raw_body = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        if raw_body is not None and len(raw_body) > MAX_BODY_BYTES:
            raise BridgeError("invalid_argument", "request body exceeds the maximum permitted size")
        request = Request(
            url,
            data=raw_body,
            method=method,
            headers={
                "Accept": "application/json",
                "X-API-Key": self.api_key,
                **({"Content-Type": "application/json"} if raw_body is not None else {}),
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                content = response.read(MAX_BODY_BYTES + 1)
                if len(content) > MAX_BODY_BYTES:
                    raise BridgeError("upstream_response_too_large", "Mem0 response exceeds the permitted size")
        except BridgeError:
            raise
        except HTTPError as exc:
            status = int(exc.code)
            if status in {401, 403}:
                raise BridgeError(
                    "authentication_failed", "Mem0 rejected bridge authentication", status=status
                ) from exc
            if status == 404:
                raise BridgeError("not_found", "Mem0 resource was not found", status=status) from exc
            if status == 503:
                try:
                    body = json.loads(exc.read(MAX_BODY_BYTES).decode("utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
                    body = {}
                code = body.get("code") if isinstance(body, dict) else None
                if code in PROVIDER_READINESS_CODES:
                    raise BridgeError(code, f"Mem0 provider is not ready ({code})", status=status) from exc
            raise BridgeError("upstream_http_error", f"Mem0 returned HTTP {status}", status=status) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise BridgeError("upstream_unavailable", "Mem0 local REST service is unavailable") from exc
        try:
            return json.loads(content.decode("utf-8")) if content else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeError("invalid_upstream_response", "Mem0 returned invalid JSON") from exc

    def _row_in_scope(self, row: dict[str, Any], scope: Scope) -> bool:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        return all(
            row.get(field) == getattr(scope, field) or metadata.get(field) == getattr(scope, field)
            for field in REQUIRED_SCOPE
        )

    def _check_row(self, row: Any, scope: Scope) -> dict[str, Any]:
        if not isinstance(row, dict) or not self._row_in_scope(row, scope):
            raise BridgeError("scope_mismatch", "memory is outside the requested scope")
        _reject_secrets(row)
        return row

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(args, dict):
            raise BridgeError("invalid_argument", "tool arguments must be an object")
        scope = _scope(args)
        if name == "add_memory":
            content = _require_text(args, "content", MAX_CONTENT_CHARS)
            metadata = _metadata(scope, args.get("metadata"))
            infer = args.get("infer", False)
            if not isinstance(infer, bool):
                raise BridgeError("invalid_argument", "infer must be boolean")
            response = self._request(
                "POST",
                "/memories",
                body={
                    "messages": [{"role": "user", "content": content}],
                    "user_id": scope.user_id,
                    "agent_id": scope.agent_id,
                    "metadata": metadata,
                    "infer": infer,
                },
            )
            _reject_secrets(response)
            return response if isinstance(response, dict) else {"results": _rows(response)}

        if name == "search_memories":
            query = _require_text(args, "query", MAX_QUERY_CHARS)
            top_k = args.get("top_k", self.max_results)
            if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= self.max_results:
                raise BridgeError("invalid_argument", f"top_k must be between 1 and {self.max_results}")
            response = self._request(
                "POST",
                "/search",
                body={"query": query, "filters": scope.as_dict(), "top_k": top_k},
            )
            rows = [row for row in _rows(response) if self._row_in_scope(row, scope)]
            _reject_secrets(rows)
            return {"results": rows}

        if name == "get_memories":
            top_k = args.get("top_k", self.max_results)
            if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= self.max_results:
                raise BridgeError("invalid_argument", f"top_k must be between 1 and {self.max_results}")
            response = self._request(
                "GET",
                "/memories",
                query={**scope.as_dict(), "top_k": top_k},
            )
            rows = [row for row in _rows(response) if self._row_in_scope(row, scope)]
            _reject_secrets(rows)
            return {"results": rows}

        if name == "get_memory":
            memory_id = _memory_id(args)
            row = self._request("GET", f"/memories/{memory_id}")
            return self._check_row(row, scope)

        if name == "update_memory":
            memory_id = _memory_id(args)
            content = _require_text(args, "content", MAX_CONTENT_CHARS)
            self._check_row(self._request("GET", f"/memories/{memory_id}"), scope)
            response = self._request("PUT", f"/memories/{memory_id}", body={"text": content})
            _reject_secrets(response)
            return response if isinstance(response, dict) else {"result": response}

        if name == "delete_memory":
            memory_id = _memory_id(args)
            if args.get("confirm") is not True:
                raise BridgeError(
                    "confirmation_required", "delete_memory requires explicit confirmation (confirm=true)"
                )
            self._check_row(self._request("GET", f"/memories/{memory_id}"), scope)
            response = self._request("DELETE", f"/memories/{memory_id}")
            _reject_secrets(response)
            return response if isinstance(response, dict) else {"result": response}

        raise BridgeError("unknown_tool", f"unknown tool: {name}")

    def _tool_schema(self, optional: dict[str, dict[str, str]]) -> dict[str, Any]:
        properties = {field: {"type": "string"} for field in REQUIRED_SCOPE}
        properties.update(optional)
        return {"type": "object", "properties": properties, "required": list(REQUIRED_SCOPE)}

    def handle_jsonrpc(self, request: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(request, dict):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        method = request.get("method")
        request_id = request.get("id")
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mem0_0-arch", "version": "0.1.0"},
                },
            }
        if method == "tools/list":
            tools = []
            for name, description, optional in self.TOOLS:
                tools.append({"name": name, "description": description, "inputSchema": self._tool_schema(optional)})
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}}
        if method == "tools/call":
            params = request.get("params") if isinstance(request.get("params"), dict) else {}
            name = params.get("name")
            arguments = params.get("arguments", {})
            try:
                result = self.call_tool(name, arguments)
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"content": [{"type": "text", "text": json.dumps(result, separators=(",", ":"))}]},
                }
            except BridgeError as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "isError": True,
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps({"code": exc.code, "error": exc.message}),
                            }
                        ],
                    },
                }
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}}


def serve_stdio(bridge: Mem0Bridge) -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    for raw_line in sys.stdin.buffer:
        if len(raw_line) > MAX_LINE_BYTES:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "request too large"},
            }
        else:
            try:
                request = json.loads(raw_line.decode("utf-8"))
                response = bridge.handle_jsonrpc(request)
            except (UnicodeDecodeError, json.JSONDecodeError):
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            except BridgeError as exc:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": exc.message}}
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        try:
            print(json.dumps(Mem0Bridge().self_test(), separators=(",", ":")))
        except BridgeError as exc:
            print(json.dumps({"status": "error", "code": exc.code, "error": exc.message}, separators=(",", ":")))
            raise SystemExit(1)
    else:
        serve_stdio(Mem0Bridge())
