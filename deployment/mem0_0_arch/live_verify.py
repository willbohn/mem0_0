#!/usr/bin/env python3
"""Run a content-safe, scoped verification against the canonical shadow API."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen


DEFAULT_API_URL = "http://127.0.0.1:28889"
DEFAULT_KEY_FILE = Path("/home/will/.config/mem0_0-arch/bridge-api-key")
MAX_BODY_BYTES = 512 * 1024
MAX_SCOPE_RESULTS = 100
REQUIRED_SCOPE = ("user_id", "agent_id", "machine_id", "project_id", "data_class")
MEMORY_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")


class ConfigurationError(ValueError):
    """Raised when live verification cannot start safely."""


class VerificationError(RuntimeError):
    """Raised for bounded verification failures that must not expose API content."""


def _default_open(request: Request, timeout: float) -> bytes:
    with urlopen(request, timeout=timeout) as response:
        content = response.read(MAX_BODY_BYTES + 1)
    if len(content) > MAX_BODY_BYTES:
        raise VerificationError("response exceeds the size limit")
    return content


def _empty_report() -> dict[str, Any]:
    return {
        "ok": False,
        "checks": {
            "auth_file_private": False,
            "scope_complete": False,
            "kyoto_added": False,
            "osaka_corrected": False,
            "current_excludes_kyoto": False,
            "current_includes_osaka": False,
            "historical_includes_kyoto": False,
            "audit_completed": False,
            "consolidation_dry_run": False,
            "helpful_feedback": False,
            "cleanup_complete": False,
        },
        "counts": {
            "created": 0,
            "current_recall": 0,
            "historical_recall": 0,
            "audit_scanned": 0,
            "audit_issues": 0,
            "consolidation_actions": 0,
            "cleanup_discovered": 0,
            "cleanup_deleted": 0,
            "cleanup_remaining": 0,
        },
        "timings_ms": {},
    }


class LiveVerifier:
    """Exercise state-aware REST behavior inside one disposable identity scope."""

    def __init__(
        self,
        *,
        api_url: str = DEFAULT_API_URL,
        api_key_file: str | Path = DEFAULT_KEY_FILE,
        timeout: float = 600.0,
        opener: Callable[[Request, float], bytes] = _default_open,
    ) -> None:
        if os.environ.get("MEM0_API_KEY"):
            raise ConfigurationError("inline API keys are forbidden; use a private key file")
        parsed_url = urlsplit(api_url)
        try:
            parsed_port = parsed_url.port
        except ValueError as exc:
            raise ConfigurationError("API URL must be a valid loopback HTTP endpoint") from exc
        if (
            parsed_url.scheme != "http"
            or parsed_url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed_port is None
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.path not in {"", "/"}
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ConfigurationError("API URL must be a credential-free loopback HTTP endpoint with an explicit port")
        self.api_url = api_url.rstrip("/")
        self.api_key_file = Path(api_key_file)
        self.timeout = max(0.1, min(float(timeout), 900.0))
        self.opener = opener
        self.api_key = self._load_private_key()
        token = uuid.uuid4().hex
        self.scope = {
            "user_id": f"live-user-{token}",
            "agent_id": f"live-agent-{token}",
            "machine_id": f"live-machine-{token}",
            "project_id": f"live-project-{token}",
            "data_class": f"live-synthetic-{token}",
        }
        self.report = _empty_report()
        self.report["checks"]["auth_file_private"] = True
        self.report["checks"]["scope_complete"] = set(self.scope) == set(REQUIRED_SCOPE) and all(self.scope.values())

    def _load_private_key(self) -> str:
        try:
            mode = self.api_key_file.stat().st_mode
            if not self.api_key_file.is_file() or mode & 0o077:
                raise ConfigurationError("API key must be a private regular file")
            key = self.api_key_file.read_text(encoding="utf-8").strip()
        except ConfigurationError:
            raise
        except (OSError, UnicodeError) as exc:
            raise ConfigurationError("API key must be a readable private regular file") from exc
        if not key or "\n" in key or len(key) > 4096:
            raise ConfigurationError("API key file has an invalid format")
        return key

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.api_url}{path}"
        if query:
            url = f"{url}?{urlencode({key: str(value) for key, value in query.items()})}"
        raw = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        request = Request(
            url,
            data=raw,
            method=method,
            headers={
                "Accept": "application/json",
                "X-API-Key": self.api_key,
                **({"Content-Type": "application/json"} if raw is not None else {}),
            },
        )
        try:
            content = self.opener(request, self.timeout)
        except VerificationError:
            raise
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise VerificationError("local API request failed") from exc
        except Exception as exc:
            raise VerificationError("local API request failed") from exc
        if len(content) > MAX_BODY_BYTES:
            raise VerificationError("response exceeds the size limit")
        try:
            return json.loads(content.decode()) if content else {}
        except (UnicodeError, json.JSONDecodeError, TypeError) as exc:
            raise VerificationError("local API returned invalid JSON") from exc

    def _step(self, name: str, operation: Callable[[], Any]) -> Any:
        started = time.monotonic()
        try:
            return operation()
        finally:
            self.report["timings_ms"][name] = round((time.monotonic() - started) * 1000, 3)

    @staticmethod
    def _results(response: Any) -> list[dict[str, Any]]:
        if not isinstance(response, dict) or not isinstance(response.get("results"), list):
            raise VerificationError("local API returned an invalid result envelope")
        return [row for row in response["results"] if isinstance(row, dict)]

    @staticmethod
    def _memory_texts(rows: list[dict[str, Any]]) -> list[str]:
        return [str(row.get("memory", "")) for row in rows if row.get("memory")]

    def _add(self, content: str, *, created_at: str) -> list[dict[str, Any]]:
        response = self._request(
            "POST",
            "/memories",
            body={
                "messages": [{"role": "user", "content": content}],
                "user_id": self.scope["user_id"],
                "agent_id": self.scope["agent_id"],
                "metadata": {**self.scope, "created_at": created_at},
                "infer": True,
            },
        )
        rows = self._results(response)
        if not rows:
            raise VerificationError("add produced no result")
        return rows

    def _search(self, query: str) -> list[dict[str, Any]]:
        return self._results(
            self._request(
                "POST",
                "/search",
                body={"query": query, "filters": dict(self.scope), "top_k": 20, "threshold": 0.0},
            )
        )

    def _row_matches_scope(self, row: dict[str, Any]) -> bool:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        return (
            row.get("user_id") == self.scope["user_id"]
            and row.get("agent_id") == self.scope["agent_id"]
            and all(metadata.get(key) == self.scope[key] for key in ("machine_id", "project_id", "data_class"))
        )

    def _discover_scope_ids(self) -> set[str]:
        query = {**self.scope, "top_k": MAX_SCOPE_RESULTS}
        rows = self._results(self._request("GET", "/memories", query=query))
        return {
            str(row["id"])
            for row in rows
            if self._row_matches_scope(row)
            and isinstance(row.get("id"), str)
            and MEMORY_ID_RE.fullmatch(str(row["id"]))
        }

    def _cleanup(self) -> None:
        started = time.monotonic()
        discovered: set[str] = set()
        deleted = 0
        discovery_succeeded = False
        try:
            discovered = self._discover_scope_ids()
            discovery_succeeded = True
            for memory_id in sorted(discovered):
                try:
                    self._request("DELETE", f"/memories/{quote(memory_id, safe='')}")
                    deleted += 1
                except Exception:
                    continue
            remaining = self._discover_scope_ids()
        except Exception:
            remaining = discovered
        self.report["counts"]["cleanup_discovered"] = len(discovered)
        self.report["counts"]["cleanup_deleted"] = deleted
        self.report["counts"]["cleanup_remaining"] = len(remaining)
        self.report["checks"]["cleanup_complete"] = discovery_succeeded and deleted == len(discovered) and not remaining
        self.report["timings_ms"]["cleanup"] = round((time.monotonic() - started) * 1000, 3)

    def _run_flow(self) -> None:
        kyoto_rows = self._step(
            "add_kyoto",
            lambda: self._add("During 2025, the residence was Kyoto.", created_at="2025-01-01T00:00:00Z"),
        )
        self.report["counts"]["created"] += len(kyoto_rows)
        self.report["checks"]["kyoto_added"] = True

        osaka_rows = self._step(
            "correct_osaka",
            lambda: self._add(
                "Starting in 2026, the corrected current residence is Osaka.",
                created_at="2026-01-01T00:00:00Z",
            ),
        )
        self.report["counts"]["created"] += len(osaka_rows)
        self.report["checks"]["osaka_corrected"] = True

        current_rows = self._step("current_recall", lambda: self._search("What is the current residence now?"))
        current_texts = self._memory_texts(current_rows)
        self.report["counts"]["current_recall"] = len(current_rows)
        self.report["checks"]["current_excludes_kyoto"] = bool(current_texts) and all(
            "kyoto" not in text.casefold() for text in current_texts
        )
        self.report["checks"]["current_includes_osaka"] = any("osaka" in text.casefold() for text in current_texts)
        if not (self.report["checks"]["current_excludes_kyoto"] and self.report["checks"]["current_includes_osaka"]):
            raise VerificationError("current recall did not satisfy the synthetic assertion")

        historical_rows = self._step(
            "historical_recall", lambda: self._search("Where was the residence on 2025-06-01?")
        )
        historical_texts = self._memory_texts(historical_rows)
        self.report["counts"]["historical_recall"] = len(historical_rows)
        self.report["checks"]["historical_includes_kyoto"] = any(
            "kyoto" in text.casefold() for text in historical_texts
        )
        if not self.report["checks"]["historical_includes_kyoto"]:
            raise VerificationError("historical recall did not satisfy the synthetic assertion")

        audit = self._step(
            "audit",
            lambda: self._request(
                "POST", "/memories/audit", body={"filters": dict(self.scope), "limit": MAX_SCOPE_RESULTS}
            ),
        )
        if not isinstance(audit, dict) or not isinstance(audit.get("scanned"), int):
            raise VerificationError("audit returned an invalid envelope")
        summary = audit.get("summary") if isinstance(audit.get("summary"), dict) else {}
        self.report["counts"]["audit_scanned"] = audit["scanned"]
        self.report["counts"]["audit_issues"] = sum(
            value for value in summary.values() if isinstance(value, int) and not isinstance(value, bool)
        )
        self.report["checks"]["audit_completed"] = True

        consolidation = self._step(
            "consolidate",
            lambda: self._request(
                "POST",
                "/memories/consolidate",
                body={
                    "filters": dict(self.scope),
                    "limit": MAX_SCOPE_RESULTS,
                    "dry_run": True,
                    "strategy": "safe",
                },
            ),
        )
        if not isinstance(consolidation, dict) or not isinstance(consolidation.get("actions"), list):
            raise VerificationError("consolidation returned an invalid envelope")
        self.report["counts"]["consolidation_actions"] = len(consolidation["actions"])
        self.report["checks"]["consolidation_dry_run"] = consolidation.get("dry_run") is True
        if not self.report["checks"]["consolidation_dry_run"]:
            raise VerificationError("consolidation was not a dry run")

        helpful_candidates = [
            row
            for row in current_rows
            if self._row_matches_scope(row)
            and isinstance(row.get("id"), str)
            and MEMORY_ID_RE.fullmatch(str(row["id"]))
            and "osaka" in str(row.get("memory", "")).casefold()
        ]
        if len(helpful_candidates) != 1:
            raise VerificationError("current recall did not identify one exact synthetic memory")
        memory_id = str(helpful_candidates[0]["id"])
        feedback = self._step(
            "feedback",
            lambda: self._request(
                "POST", f"/memories/{quote(memory_id, safe='')}/feedback", body={"signal": "helpful"}
            ),
        )
        if not isinstance(feedback, dict):
            raise VerificationError("feedback returned an invalid envelope")
        self.report["checks"]["helpful_feedback"] = True

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        try:
            self._run_flow()
        except Exception:
            pass
        finally:
            self._cleanup()
            self.report["timings_ms"]["total"] = round((time.monotonic() - started) * 1000, 3)
        self.report["ok"] = all(self.report["checks"].values())
        return self.report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the scoped canonical shadow REST verification gate")
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument(
        "--api-key-file",
        type=Path,
        default=Path(os.environ.get("MEM0_API_KEY_FILE", DEFAULT_KEY_FILE)),
    )
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args(argv)
    try:
        report = LiveVerifier(
            api_url=args.api_url,
            api_key_file=args.api_key_file,
            timeout=args.timeout,
        ).run()
    except Exception:
        report = _empty_report()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
