from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from deployment.mem0_0_arch.live_verify import ConfigurationError, LiveVerifier, REQUIRED_SCOPE


class FakeLiveAPI:
    def __init__(self, *, fail_path: str | None = None) -> None:
        self.fail_path = fail_path
        self.requests = []
        self.scope: dict[str, str] | None = None
        self.records: dict[str, dict] = {}

    def __call__(self, request, timeout: float) -> bytes:
        del timeout
        method = request.get_method()
        parsed = urlparse(request.full_url)
        body = json.loads(request.data) if request.data else None
        self.requests.append((method, parsed.path, parse_qs(parsed.query), body, dict(request.header_items())))
        if parsed.path == self.fail_path:
            raise OSError("fixture transport error containing private-response-body")

        if method == "POST" and parsed.path == "/memories":
            if self.scope is None:
                self.scope = {key: body["metadata"][key] for key in REQUIRED_SCOPE}
            memory_id = "synthetic-kyoto" if not self.records else "synthetic-osaka"
            memory = "The residence was Kyoto in 2025" if not self.records else "The current residence is Osaka"
            self.records[memory_id] = {
                "id": memory_id,
                "memory": memory,
                "user_id": self.scope["user_id"],
                "agent_id": self.scope["agent_id"],
                "metadata": {key: self.scope[key] for key in ("machine_id", "project_id", "data_class")},
            }
            return self._json({"results": [{"id": memory_id, "event": "ADD"}]})

        if method == "POST" and parsed.path == "/search":
            assert body["filters"] == self.scope
            if "2025" in body["query"]:
                return self._json({"results": [self.records["synthetic-kyoto"]]})
            return self._json({"results": [self.records["synthetic-osaka"]]})

        if method == "POST" and parsed.path == "/memories/audit":
            assert body == {"filters": self.scope, "limit": 100}
            return self._json({"scanned": 2, "summary": {"duplicates": 0}, "issues": {"duplicates": []}})

        if method == "POST" and parsed.path == "/memories/consolidate":
            assert body == {"filters": self.scope, "limit": 100, "dry_run": True, "strategy": "safe"}
            return self._json({"dry_run": True, "strategy": "safe", "actions": []})

        if method == "POST" and parsed.path == "/memories/synthetic-osaka/feedback":
            assert body == {"signal": "helpful"}
            return self._json({"message": "Feedback recorded successfully!", "memory_id": "synthetic-osaka"})

        if method == "GET" and parsed.path == "/memories":
            assert {key: values[-1] for key, values in parse_qs(parsed.query).items() if key != "top_k"} == self.scope
            rows = list(self.records.values())
            if self.fail_path:
                rows.append(
                    {
                        "id": "legacy-row",
                        "memory": "private legacy body",
                        "user_id": "someone-else",
                        "agent_id": self.scope["agent_id"],
                        "metadata": {key: self.scope[key] for key in ("machine_id", "project_id", "data_class")},
                    }
                )
            return self._json({"results": rows})

        if method == "DELETE" and parsed.path.startswith("/memories/"):
            self.records.pop(parsed.path.rsplit("/", 1)[-1], None)
            return self._json({"message": "Memory deleted successfully"})

        raise AssertionError(f"unexpected request: {method} {parsed.path}")

    @staticmethod
    def _json(payload: dict) -> bytes:
        return json.dumps(payload).encode()


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "admin-api-key"
    path.write_text("fixture-private-key\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def _assert_output_scalars_are_safe(value) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            _assert_output_scalars_are_safe(nested)
        return
    assert isinstance(value, (bool, int, float))


def test_live_verifier_exercises_scoped_lifecycle_and_emits_only_aggregate_evidence(
    key_file: Path, monkeypatch
) -> None:
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    api = FakeLiveAPI()

    report = LiveVerifier(api_key_file=key_file, opener=api, timeout=1).run()

    assert report["ok"] is True
    assert report["checks"] == {
        "auth_file_private": True,
        "scope_complete": True,
        "kyoto_added": True,
        "osaka_corrected": True,
        "current_excludes_kyoto": True,
        "current_includes_osaka": True,
        "historical_includes_kyoto": True,
        "audit_completed": True,
        "consolidation_dry_run": True,
        "helpful_feedback": True,
        "cleanup_complete": True,
    }
    assert report["counts"] == {
        "created": 2,
        "current_recall": 1,
        "historical_recall": 1,
        "audit_scanned": 2,
        "audit_issues": 0,
        "consolidation_actions": 0,
        "cleanup_discovered": 2,
        "cleanup_deleted": 2,
        "cleanup_remaining": 0,
    }
    _assert_output_scalars_are_safe(report)
    rendered = json.dumps(report)
    assert all(secret not in rendered for secret in ("fixture-private-key", "Kyoto", "Osaka", "synthetic-"))

    first_add = api.requests[0]
    scope = {key: first_add[3]["metadata"][key] for key in REQUIRED_SCOPE}
    assert set(scope) == {"user_id", "agent_id", "machine_id", "project_id", "data_class"}
    assert all(scope_value.endswith(scope["user_id"].rsplit("-", 1)[-1]) for scope_value in scope.values())
    assert first_add[3]["metadata"]["created_at"] == "2025-01-01T00:00:00Z"
    assert api.requests[1][3]["metadata"]["created_at"] == "2026-01-01T00:00:00Z"
    for method, path, _query, body, headers in api.requests:
        assert headers["X-api-key"] == "fixture-private-key"
        assert "Authorization" not in headers
        if method == "POST" and path in {"/search", "/memories/audit", "/memories/consolidate"}:
            assert body["filters"] == scope
    assert all(path != "/memories" for method, path, *_rest in api.requests if method == "DELETE")


def test_failure_still_cleans_only_ids_verified_in_the_unique_scope(key_file: Path, monkeypatch) -> None:
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    api = FakeLiveAPI(fail_path="/memories/audit")

    report = LiveVerifier(api_key_file=key_file, opener=api, timeout=1).run()

    assert report["ok"] is False
    assert report["checks"]["audit_completed"] is False
    assert report["checks"]["cleanup_complete"] is True
    assert report["counts"]["cleanup_discovered"] == 2
    assert report["counts"]["cleanup_deleted"] == 2
    deletes = [path for method, path, *_rest in api.requests if method == "DELETE"]
    assert set(deletes) == {"/memories/synthetic-kyoto", "/memories/synthetic-osaka"}
    assert "/memories/legacy-row" not in deletes
    assert "private-response-body" not in json.dumps(report)


def test_file_only_auth_rejects_inline_or_non_private_credentials(key_file: Path, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEM0_API_KEY", "forbidden-inline-key")
    with pytest.raises(ConfigurationError, match="inline API keys are forbidden"):
        LiveVerifier(api_key_file=key_file, opener=FakeLiveAPI())

    monkeypatch.delenv("MEM0_API_KEY")
    insecure = tmp_path / "insecure-key"
    insecure.write_text("fixture\n", encoding="utf-8")
    os.chmod(insecure, 0o644)
    with pytest.raises(ConfigurationError, match="private regular file"):
        LiveVerifier(api_key_file=insecure, opener=FakeLiveAPI())


@pytest.mark.parametrize(
    "api_url",
    [
        "https://127.0.0.1:28889",
        "http://example.com:28889",
        "http://user:password@127.0.0.1:28889",
        "http://127.0.0.1:28889/api?token=private",
        "http://127.0.0.1",
    ],
)
def test_live_verifier_rejects_non_loopback_or_credential_bearing_endpoints(
    api_url: str, key_file: Path, monkeypatch
) -> None:
    monkeypatch.delenv("MEM0_API_KEY", raising=False)

    with pytest.raises(ConfigurationError, match="loopback HTTP endpoint"):
        LiveVerifier(api_url=api_url, api_key_file=key_file, opener=FakeLiveAPI())


def test_live_verifier_bounds_provider_timeout(key_file: Path, monkeypatch) -> None:
    monkeypatch.delenv("MEM0_API_KEY", raising=False)

    assert LiveVerifier(api_key_file=key_file, timeout=5000, opener=FakeLiveAPI()).timeout == 900
    assert LiveVerifier(api_key_file=key_file, timeout=0, opener=FakeLiveAPI()).timeout == 0.1
