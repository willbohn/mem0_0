"""Offline tests for the budget-guarded provider flows (PA Task 7).

These tests never touch the network: the REST opener is a minimal callable
fake modeled on ``tests/deployment/mem0_0_arch/test_live_verify.py``'s
``FakeLiveAPI``. They exercise the client-side wiring (query shape, ledger
bookkeeping, cleanup, and the naming firewall) -- the actual extraction
semantics (corroboration folding, dispute cross-linking, etc.) are already
pinned against the real ``Memory`` engine in
``tests/personal_acceptance/test_lifecycle_families.py``.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from deployment.mem0_0_arch.live_verify import REQUIRED_SCOPE, ConfigurationError
from personal_acceptance.budget import BudgetLedger
from personal_acceptance.provider_flows import AcceptanceFlows

CONTENT_FIXTURES = (
    "Kumamoto",
    "Matsue",
    "oolong",
    "senbei",
    "castella",
    "genmaicha",
    "houjicha",
    "notification",
    "settings toggle",
)


class FakeProviderAPI:
    """Minimal callable opener stubbing the five provider-flow REST calls.

    Scope-aware: the provider flows now give each family its own isolated scope,
    so the fake tracks records per scope and matches search/discovery/audit
    requests against the exact scope they carry.
    """

    def __init__(
        self,
        *,
        break_supersession: bool = False,
        ignore_deletes: bool = False,
        blank_dispute: bool = False,
        extra_first_add_result: bool = False,
        clock_state: dict[str, float] | None = None,
        advertise_bounded_writes: bool = True,
    ) -> None:
        self.break_supersession = break_supersession
        self.ignore_deletes = ignore_deletes
        self.blank_dispute = blank_dispute
        self.extra_first_add_result = extra_first_add_result
        self.clock_state = clock_state
        self.advertise_bounded_writes = advertise_bounded_writes
        self.requests: list[tuple] = []
        self.records: dict[str, dict] = {}
        self.scopes: set[str] = set()
        self._next_id = 1

    @staticmethod
    def _scope_key(metadata_or_filters: dict) -> str:
        # Records nest machine_id/project_id/data_class under "metadata"; query
        # filters carry them at top level. Accept either location.
        def _val(key: str) -> str:
            return metadata_or_filters.get(key) or (metadata_or_filters.get("metadata") or {}).get(key) or ""

        return "|".join(_val(k) for k in REQUIRED_SCOPE)

    def __call__(self, request, timeout: float) -> bytes:
        del timeout
        method = request.get_method()
        parsed = urlparse(request.full_url)
        body: dict = json.loads(request.data) if request.data else {}
        self.requests.append((method, parsed.path, parse_qs(parsed.query), body))

        if method == "GET" and parsed.path == "/openapi.json":
            properties = (
                {
                    "max_extractions": {"anyOf": [{"minimum": 1, "type": "integer"}, {"type": "null"}]},
                    "max_mutations": {"anyOf": [{"minimum": 1, "type": "integer"}, {"type": "null"}]},
                }
                if self.advertise_bounded_writes
                else {}
            )
            return self._json({"components": {"schemas": {"MemoryCreate": {"properties": properties}}}})

        if method == "POST" and parsed.path == "/memories":
            if body["infer"] is True:
                assert body["max_extractions"] == 1
                assert body["max_mutations"] == 2
            scope = {key: body["metadata"][key] for key in REQUIRED_SCOPE}
            key = self._scope_key(scope)
            self.scopes.add(key)
            memory_id = f"pa-{self._next_id}"
            self._next_id += 1
            content = body["messages"][0]["content"]
            self.records[memory_id] = {
                "id": memory_id,
                "memory": content,
                "user_id": scope["user_id"],
                "agent_id": scope["agent_id"],
                "metadata": {k: scope[k] for k in ("machine_id", "project_id", "data_class")},
            }
            results = [{"id": memory_id, "event": "ADD"}]
            if self.extra_first_add_result and self._next_id == 2:
                extra_id = f"pa-{self._next_id}"
                self._next_id += 1
                self.records[extra_id] = {**self.records[memory_id], "id": extra_id}
                results.append({"id": extra_id, "event": "ADD"})
            return self._json({"results": results})

        if method == "POST" and parsed.path == "/search":
            scope_key = self._scope_key(body["filters"])
            return self._json({"results": self._search_results(body["query"], scope_key)})

        if method == "POST" and parsed.path == "/memories/audit":
            return self._json({"scanned": len(self.records), "summary": {"disputed": 2}})

        if method == "GET" and parsed.path == "/memories":
            query_scope = {key: values[-1] for key, values in parse_qs(parsed.query).items() if key != "top_k"}
            scope_key = self._scope_key(query_scope)
            top_k = int(parse_qs(parsed.query)["top_k"][-1])
            results = [r for r in self.records.values() if self._scope_key(r) == scope_key][:top_k]
            if not results and self.clock_state is not None:
                self.clock_state["now"] = 1011.0
            return self._json({"results": results})

        if method == "DELETE" and parsed.path.startswith("/memories/"):
            if not self.ignore_deletes:
                self.records.pop(parsed.path.rsplit("/", 1)[-1], None)
            return self._json({"message": "Memory deleted successfully"})

        raise AssertionError(f"unexpected request: {method} {parsed.path}")

    def _search_results(self, query: str, scope_key: str) -> list[dict]:
        scoped = [r for r in self.records.values() if self._scope_key(r) == scope_key]

        def rows_with(substring: str) -> list[dict]:
            return [record for record in scoped if substring.lower() in record["memory"].lower()]

        if query == "What is the current city preference?":
            return rows_with("Kumamoto") if self.break_supersession else rows_with("Matsue")
        if query == "What was the city preference on 2025-06-01?":
            return rows_with("Kumamoto")
        if query == "What is the current beverage preference?":
            return rows_with("oolong")[:1]
        if query == "What is the current snack preference?":
            return [] if self.blank_dispute else rows_with("Snack preference")
        if query == "What is the current tea preference?":
            return rows_with("reverted to genmaicha")
        if query == "What are the current notification-related records?":
            return rows_with("notification")
        raise AssertionError(f"unexpected search query: {query!r}")

    @staticmethod
    def _json(payload: dict) -> bytes:
        return json.dumps(payload).encode()


def _clock_sequence(*values: float):
    remaining = list(values)

    def _clock() -> float:
        if remaining:
            return remaining.pop(0)
        return values[-1]

    return _clock


def _assert_output_scalars_are_safe(value) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            _assert_output_scalars_are_safe(nested)
        return
    assert isinstance(value, (bool, int, float))


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "provider-api-key"
    path.write_text("fixture-private-key\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def test_provider_flows_run_all_five_families_and_charge_the_ledger_exactly(key_file: Path, monkeypatch) -> None:
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    ledger = BudgetLedger()
    ledger.start_provider_clock(now=0.0)
    api = FakeProviderAPI()

    report = AcceptanceFlows(ledger=ledger, clock=lambda: 1.0, api_key_file=key_file, opener=api, timeout=1).run()

    assert report["ok"] is True
    assert report["checks"] == {
        "auth_file_private": True,
        "scope_complete": True,
        "budget_ok": True,
        "bounded_write_contract": True,
        "supersession_live_ok": True,
        "corroboration_live_ok": True,
        "dispute_live_ok": True,
        "reversion_live_ok": True,
        "direct_vs_lifecycle_live_ok": True,
        "cleanup_complete": True,
        "history_cleanup_complete": True,
    }
    assert report["counts"]["created"] == 11
    assert report["counts"]["cleanup_discovered"] == 11
    assert report["counts"]["cleanup_deleted"] == 11
    assert report["counts"]["cleanup_remaining"] == 0

    snapshot = ledger.snapshot()
    assert snapshot["infer_writes"] == 10
    assert snapshot["mutations"] == 32  # 20 inferred mutation ceiling + 1 raw write + 11 deletes
    assert api.records == {}
    _assert_output_scalars_are_safe(report)


def test_provider_flows_fail_closed_when_supersession_search_returns_the_superseded_text(
    key_file: Path, monkeypatch
) -> None:
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    ledger = BudgetLedger()
    ledger.start_provider_clock(now=0.0)
    api = FakeProviderAPI(break_supersession=True)

    report = AcceptanceFlows(ledger=ledger, clock=lambda: 1.0, api_key_file=key_file, opener=api, timeout=1).run()

    assert report["ok"] is False
    assert report["checks"]["supersession_live_ok"] is False
    # Nothing after the raising family ran.
    assert report["checks"]["corroboration_live_ok"] is False
    assert report["checks"]["cleanup_complete"] is True
    assert report["counts"]["cleanup_discovered"] == 2
    assert report["counts"]["cleanup_deleted"] == 2
    assert ledger.snapshot()["mutations"] == 6  # 2 inferred calls x 2-mutation ceiling + 2 deletes


def test_provider_flows_dispute_check_fails_closed_when_search_returns_no_results(key_file: Path, monkeypatch) -> None:
    """Regression guard: a fake that drops both dispute memories must not pass vacuously.

    ``len(current_rows) != 1`` was true for 0 rows too, so a regression that
    silently lost both conflicting memories still reported ``dispute_live_ok``
    True. This pins the fixed contract: exactly the two conflicting facts,
    never fewer.
    """
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    ledger = BudgetLedger()
    ledger.start_provider_clock(now=0.0)
    api = FakeProviderAPI(blank_dispute=True)

    report = AcceptanceFlows(ledger=ledger, clock=lambda: 1.0, api_key_file=key_file, opener=api, timeout=1).run()

    assert report["ok"] is False
    assert report["checks"]["supersession_live_ok"] is True
    assert report["checks"]["corroboration_live_ok"] is True
    assert report["checks"]["dispute_live_ok"] is False
    # Nothing after the raising family ran.
    assert report["checks"]["reversion_live_ok"] is False
    assert report["checks"]["direct_vs_lifecycle_live_ok"] is False
    assert report["checks"]["cleanup_complete"] is True


def test_provider_flows_never_delete_past_the_mutation_budget(key_file: Path, monkeypatch) -> None:
    """An undersized budget must reject the plan before creating anything."""
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    ledger = BudgetLedger(max_mutations=3, max_infer_writes=48)
    ledger.start_provider_clock(now=0.0)
    api = FakeProviderAPI()

    report = AcceptanceFlows(ledger=ledger, clock=lambda: 1.0, api_key_file=key_file, opener=api, timeout=1).run()

    assert isinstance(report, dict)
    assert report["ok"] is False
    assert report["checks"]["budget_ok"] is False
    assert report["checks"]["cleanup_complete"] is True
    assert report["counts"]["cleanup_deleted"] == 0
    assert not any(method == "POST" and path.endswith("/memories") for method, path, *_ in api.requests)
    assert not any(method == "DELETE" for method, *_ in api.requests)
    assert ledger.snapshot()["mutations"] == 0


def test_provider_flows_reject_old_shadow_before_any_write(key_file: Path, monkeypatch) -> None:
    """Unknown request fields must not let an older server bypass the cardinality cap."""
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    ledger = BudgetLedger()
    ledger.start_provider_clock(now=0.0)
    api = FakeProviderAPI(advertise_bounded_writes=False)

    report = AcceptanceFlows(ledger=ledger, clock=lambda: 1.0, api_key_file=key_file, opener=api, timeout=1).run()

    assert report["ok"] is False
    assert report["checks"]["bounded_write_contract"] is False
    assert report["checks"]["cleanup_complete"] is True
    assert not any(method == "POST" and path == "/memories" for method, path, *_ in api.requests)
    assert ledger.snapshot()["mutations"] == 0


def test_provider_flows_count_and_tolerate_multiple_memories_from_one_add(key_file: Path, monkeypatch) -> None:
    """One synthetic add returning two records is tolerated (>=1 accepted), not rejected.

    The local model may occasionally over-extract a single add into two records;
    SA-04 validates behavior via search recall rather than an exact row count, so
    the flow accepts the non-empty result and charges each returned row.
    """
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    ledger = BudgetLedger()
    ledger.start_provider_clock(now=0.0)
    api = FakeProviderAPI(extra_first_add_result=True)

    report = AcceptanceFlows(ledger=ledger, clock=lambda: 1.0, api_key_file=key_file, opener=api, timeout=1).run()

    assert report["ok"] is True
    assert report["checks"]["supersession_live_ok"] is True
    # The first synthetic add returns two records; both are counted, charged, and
    # cleaned up (the flow tolerates >=1 result per add).
    assert report["counts"]["created"] == 12  # 11 normal adds + 1 extra from the first
    assert report["counts"]["cleanup_deleted"] == 12
    assert api.records == {}


def test_provider_flows_fail_the_run_without_extending_past_the_wall_clock_cap(key_file: Path, monkeypatch) -> None:
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    ledger = BudgetLedger(provider_wall_clock_seconds=10.0)
    ledger.start_provider_clock(now=1000.0)
    api = FakeProviderAPI()
    clock = _clock_sequence(1000.0, 1000.0, 1015.0)

    report = AcceptanceFlows(ledger=ledger, clock=clock, api_key_file=key_file, opener=api, timeout=1).run()

    assert report["ok"] is False
    assert report["checks"]["cleanup_complete"] is True
    assert report["checks"]["budget_ok"] is False
    assert ledger.snapshot()["provider_elapsed_seconds"] > 10.0


def test_provider_flows_check_the_wall_clock_after_cleanup(key_file: Path, monkeypatch) -> None:
    """A run crossing the cap after its final write must not report success."""
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    ledger = BudgetLedger(provider_wall_clock_seconds=10.0)
    ledger.start_provider_clock(now=1000.0)
    clock_state = {"now": 1000.0}
    api = FakeProviderAPI(clock_state=clock_state)

    report = AcceptanceFlows(
        ledger=ledger,
        clock=lambda: clock_state["now"],
        api_key_file=key_file,
        opener=api,
        timeout=1,
    ).run()

    assert report["ok"] is False
    assert report["checks"]["budget_ok"] is False
    assert ledger.snapshot()["provider_elapsed_seconds"] > 10.0


def test_provider_flows_reject_noncanonical_loopback_endpoint(key_file: Path, monkeypatch) -> None:
    """Provider results and receipt reconciliation must refer to the same shadow."""
    monkeypatch.delenv("MEM0_API_KEY", raising=False)

    with pytest.raises(ConfigurationError, match="canonical shadow endpoint"):
        AcceptanceFlows(
            ledger=BudgetLedger(),
            api_url="http://127.0.0.1:29999",
            api_key_file=key_file,
            opener=FakeProviderAPI(),
        )


def test_provider_flows_reject_inline_api_key_env(key_file: Path, monkeypatch) -> None:
    monkeypatch.setenv("MEM0_API_KEY", "forbidden-inline-key")
    with pytest.raises(ConfigurationError, match="inline API keys are forbidden"):
        AcceptanceFlows(ledger=BudgetLedger(), api_key_file=key_file, opener=FakeProviderAPI())


def test_provider_flows_report_never_leaks_synthetic_content(key_file: Path, monkeypatch) -> None:
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    ledger = BudgetLedger()
    ledger.start_provider_clock(now=0.0)
    api = FakeProviderAPI()

    report = AcceptanceFlows(ledger=ledger, clock=lambda: 1.0, api_key_file=key_file, opener=api, timeout=1).run()

    rendered = json.dumps(report)
    assert "fixture-private-key" not in rendered
    for fixture in CONTENT_FIXTURES:
        assert fixture not in rendered
