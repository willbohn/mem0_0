"""Offline tests for the tiered runner CLI, report, and reconciliation gate (PA Task 8).

These tests never touch a real subprocess or network: pytest/jest invocations
go through a recording stub, and provider-tier REST calls go through the same
minimal callable-opener fake pattern as
``tests/personal_acceptance/test_provider_flows.py``'s ``FakeProviderAPI``
(copied locally per this suite's convention).
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from deployment.mem0_0_arch.backup_restore import BackupVerificationError
from deployment.mem0_0_arch.live_verify import REQUIRED_SCOPE
from deployment.mem0_0_arch.migration import MigrationConflictError
from personal_acceptance import runner
from personal_acceptance.budget import BudgetLedger
from personal_acceptance.families import deterministic_families

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

FORBIDDEN_NAMES = ("STALE", "LoCoMo", "LongMemEval", "BEAM", "Memora")

GOLDEN_RECONCILE_RESULT = {
    "schema_version": 1,
    "status": "reconciled",
    "cross_store_consistency": "verified",
    "postgres": {"rows": 3},
    "sqlite": {"rows": 3},
    "legacy_state_retained": True,
}


class FakeProviderAPI:
    """Minimal callable opener stubbing the five provider-flow REST calls.

    Local copy of ``tests/personal_acceptance/test_provider_flows.py``'s
    ``FakeProviderAPI`` (this suite's convention for offline REST doubles).
    Only the happy path is needed here since Task 8's tests exercise the
    runner's reconciliation gate and report shape, not extraction semantics.
    """

    def __init__(self) -> None:
        self.requests: list[tuple] = []
        self.records: dict[str, dict] = {}
        self.scope: dict[str, str] | None = None
        self._next_id = 1

    def __call__(self, request, timeout: float) -> bytes:
        del timeout
        method = request.get_method()
        parsed = urlparse(request.full_url)
        body: dict = json.loads(request.data) if request.data else {}
        self.requests.append((method, parsed.path, parse_qs(parsed.query), body))

        if method == "GET" and parsed.path == "/openapi.json":
            return self._json(
                {
                    "components": {
                        "schemas": {
                            "MemoryCreate": {
                                "properties": {
                                    "max_extractions": {"anyOf": [{"minimum": 1, "type": "integer"}, {"type": "null"}]},
                                    "max_mutations": {"anyOf": [{"minimum": 1, "type": "integer"}, {"type": "null"}]},
                                }
                            }
                        }
                    }
                }
            )

        if method == "POST" and parsed.path == "/memories":
            if body["infer"] is True:
                assert body["max_extractions"] == 1
                assert body["max_mutations"] == 2
            if self.scope is None:
                self.scope = {key: body["metadata"][key] for key in REQUIRED_SCOPE}
            memory_id = f"pa-{self._next_id}"
            self._next_id += 1
            content = body["messages"][0]["content"]
            self.records[memory_id] = {
                "id": memory_id,
                "memory": content,
                "user_id": self.scope["user_id"],
                "agent_id": self.scope["agent_id"],
                "metadata": {key: self.scope[key] for key in ("machine_id", "project_id", "data_class")},
            }
            return self._json({"results": [{"id": memory_id, "event": "ADD"}]})

        if method == "POST" and parsed.path == "/search":
            assert body["filters"] == self.scope
            return self._json({"results": self._search_results(body["query"])})

        if method == "POST" and parsed.path == "/memories/audit":
            assert body == {"filters": self.scope, "limit": 100}
            return self._json({"scanned": len(self.records), "summary": {"disputed": 2}})

        if method == "GET" and parsed.path == "/memories":
            assert {key: values[-1] for key, values in parse_qs(parsed.query).items() if key != "top_k"} == self.scope
            top_k = int(parse_qs(parsed.query)["top_k"][-1])
            return self._json({"results": list(self.records.values())[:top_k]})

        if method == "DELETE" and parsed.path.startswith("/memories/"):
            self.records.pop(parsed.path.rsplit("/", 1)[-1], None)
            return self._json({"message": "Memory deleted successfully"})

        raise AssertionError(f"unexpected request: {method} {parsed.path}")

    def _search_results(self, query: str) -> list[dict]:
        def rows_with(substring: str) -> list[dict]:
            return [record for record in self.records.values() if substring.lower() in record["memory"].lower()]

        if query == "What is the current city preference?":
            return rows_with("Matsue")
        if query == "What was the city preference on 2025-06-01?":
            return rows_with("Kumamoto")
        if query == "What is the current beverage preference?":
            return rows_with("oolong")[:1]
        if query == "What is the current snack preference?":
            return rows_with("Snack preference")
        if query == "What is the current tea preference?":
            return rows_with("reverted to genmaicha")
        if query == "What are the current notification-related records?":
            return rows_with("notification")
        raise AssertionError(f"unexpected search query: {query!r}")

    @staticmethod
    def _json(payload: dict) -> bytes:
        return json.dumps(payload).encode()


class _StubRunner:
    """Recording stand-in for ``subprocess.run`` that never spawns a process."""

    def __init__(self, *, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.calls: list[tuple[list, dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr="")


def _stub_runner(*, returncode: int = 0, stdout: str = "") -> _StubRunner:
    return _StubRunner(returncode=returncode, stdout=stdout)


def _assert_output_scalars_are_safe(value) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            _assert_output_scalars_are_safe(nested)
        return
    assert isinstance(value, (bool, int, float))


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "runner-api-key"
    path.write_text("fixture-private-key\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


@pytest.fixture
def receipt_and_home(tmp_path: Path) -> tuple[Path, Path]:
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    return receipt, home


# (a) build_report schema pin: exact top-level keys, acceptance_class, scalar-only leaves.
def test_build_report_schema_pin_and_scalar_only_leaves():
    stub = _stub_runner(returncode=0, stdout="16 passed in 1.23s\n")

    report = runner.build_report(tier="deterministic", ledger=BudgetLedger(), subprocess_runner=stub, skip_jest=True)

    assert set(report) == {
        "schema_version",
        "acceptance_class",
        "ok",
        "tier",
        "checks",
        "counts",
        "timings_ms",
        "budget",
    }
    assert report["schema_version"] == 1
    assert report["acceptance_class"] == "bounded_personal_local"
    assert report["tier"] == "deterministic"
    assert report["ok"] is True
    _assert_output_scalars_are_safe(report["checks"])
    _assert_output_scalars_are_safe(report["counts"])
    _assert_output_scalars_are_safe(report["timings_ms"])
    _assert_output_scalars_are_safe(report["budget"])


# (b) naming firewall: rendered report never contains forbidden benchmark names or fixture bodies.
def test_rendered_report_never_contains_forbidden_names_or_fixture_bodies(monkeypatch, key_file, receipt_and_home):
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    receipt, home = receipt_and_home
    stub = _stub_runner(returncode=0, stdout="16 passed in 1.23s\n")
    api = FakeProviderAPI()
    monkeypatch.setattr(runner, "reconcile_shadow", lambda *args, **kwargs: GOLDEN_RECONCILE_RESULT)

    report = runner.build_report(
        tier="full",
        ledger=BudgetLedger(),
        subprocess_runner=stub,
        skip_jest=True,
        receipt=receipt,
        home=home,
        clock=lambda: 1.0,
        provider_kwargs={"api_key_file": key_file, "opener": api, "timeout": 1},
    )

    rendered = json.dumps(report)
    for forbidden in FORBIDDEN_NAMES:
        assert forbidden not in rendered
    for fixture in CONTENT_FIXTURES:
        assert fixture not in rendered
    assert "fixture-private-key" not in rendered


# (c) run_deterministic: one pytest invocation with every node ID, jest invoked with the registry pattern,
# and --skip-jest honored.
def test_run_deterministic_runs_pytest_once_with_every_node_id_and_jest_with_registry_pattern():
    stub = _stub_runner(returncode=0, stdout="16 passed in 1.23s\n")
    families = deterministic_families()

    report = runner.run_deterministic(families, python_executable="python3", runner=stub, skip_jest=False)

    pytest_calls = [call for call in stub.calls if call[0][:3] == ["python3", "-m", "pytest"]]
    assert len(pytest_calls) == 1
    all_node_ids = [node_id for family in families for node_id in family.pytest_nodes]
    assert all_node_ids
    for node_id in all_node_ids:
        assert node_id in pytest_calls[0][0]

    jest_calls = [call for call in stub.calls if call[0][:2] == ["pnpm", "run"]]
    assert len(jest_calls) == 1
    expected_pattern = next(family.jest_pattern for family in families if family.jest_pattern)
    assert jest_calls[0][0] == ["pnpm", "run", "test", "--", expected_pattern]
    assert jest_calls[0][1].get("cwd") == "mem0-ts"

    assert all(report["checks"].values())


def test_run_deterministic_skip_jest_runs_only_the_pytest_subprocess():
    stub = _stub_runner(returncode=0, stdout="16 passed in 1.23s\n")
    families = deterministic_families()

    report = runner.run_deterministic(families, python_executable="python3", runner=stub, skip_jest=True)

    assert len(stub.calls) == 1
    assert stub.calls[0][0][:3] == ["python3", "-m", "pytest"]
    assert "scope_ts" not in report["checks"]


def test_run_deterministic_reruns_per_family_to_attribute_failures_content_free():
    """A failing aggregate run must attribute per family without inspecting test output content."""
    families = deterministic_families()
    node_families = [family for family in families if family.pytest_nodes]
    failing_node = node_families[0].pytest_nodes[0]

    def _run(argv, **kwargs):
        del kwargs
        if failing_node in argv:
            return SimpleNamespace(returncode=1, stdout="1 failed in 0.10s\n", stderr="")
        passed = sum(node_id in argv for family in node_families for node_id in family.pytest_nodes)
        return SimpleNamespace(returncode=0, stdout=f"{passed} passed in 0.10s\n", stderr="")

    report = runner.run_deterministic(families, python_executable="python3", runner=_run, skip_jest=True)

    assert report["checks"][node_families[0].name] is False
    for family in node_families[1:]:
        assert report["checks"][family.name] is True


def test_run_deterministic_never_marks_a_skipped_family_as_passed():
    """A collected-but-skipped acceptance node must fail that family."""
    families = deterministic_families()
    node_families = [family for family in families if family.pytest_nodes]
    skipped_family = node_families[0]

    def _run(argv, **kwargs):
        del kwargs
        is_aggregate = sum(node_id in argv for family in node_families for node_id in family.pytest_nodes) > len(
            skipped_family.pytest_nodes
        )
        if is_aggregate:
            return SimpleNamespace(returncode=0, stdout="52 passed, 1 skipped in 1.00s\n", stderr="")
        if any(node_id in argv for node_id in skipped_family.pytest_nodes):
            return SimpleNamespace(returncode=0, stdout="1 skipped in 0.10s\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="1 passed in 0.10s\n", stderr="")

    report = runner.run_deterministic(families, python_executable="python3", runner=_run, skip_jest=True)

    assert report["checks"][skipped_family.name] is False
    assert report["counts"]["pytest_skipped"] == 1


# (d) main refuses the provider tier without --receipt: exit 2, one-line stderr, no side effects.
def test_main_without_receipt_for_provider_tier_exits_two_with_one_line_stderr(capsys):
    exit_code = runner.main(["--tier", "provider"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    stderr_lines = captured.err.strip("\n").splitlines()
    assert len(stderr_lines) == 1


def test_main_without_receipt_for_full_tier_also_exits_two(capsys):
    exit_code = runner.main(["--tier", "full"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.err.strip()


# (e) reconcile() and the ok-forcing behavior when reconciliation fails.
def test_reconcile_returns_false_on_backup_verification_error(tmp_path, monkeypatch):
    def _raise(*args, **kwargs):
        raise BackupVerificationError("shadow reconciliation requires a quiesced verified source receipt")

    monkeypatch.setattr(runner, "reconcile_shadow", _raise)

    assert runner.reconcile(tmp_path / "receipt.json", tmp_path) is False


def test_reconcile_failure_forces_ok_false_even_when_every_other_check_passes(monkeypatch, key_file, receipt_and_home):
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    receipt, home = receipt_and_home
    api = FakeProviderAPI()

    def _raise(*args, **kwargs):
        raise BackupVerificationError("shadow reconciliation requires a quiesced verified source receipt")

    monkeypatch.setattr(runner, "reconcile_shadow", _raise)

    report = runner.build_report(
        tier="provider",
        ledger=BudgetLedger(),
        receipt=receipt,
        home=home,
        clock=lambda: 1.0,
        provider_kwargs={"api_key_file": key_file, "opener": api, "timeout": 1},
    )

    assert report["checks"]["reconciled"] is False
    assert report["ok"] is False
    assert all(value for key, value in report["checks"].items() if key != "reconciled")


# (f) provider tier with a fake opener + tmp receipt + monkeypatched reconcile_shadow -> ok True, reconciled True.
def test_provider_tier_with_reconciled_golden_result_is_ok(monkeypatch, key_file, receipt_and_home):
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    receipt, home = receipt_and_home
    api = FakeProviderAPI()
    monkeypatch.setattr(runner, "reconcile_shadow", lambda *args, **kwargs: GOLDEN_RECONCILE_RESULT)

    report = runner.build_report(
        tier="provider",
        ledger=BudgetLedger(),
        receipt=receipt,
        home=home,
        clock=lambda: 1.0,
        provider_kwargs={"api_key_file": key_file, "opener": api, "timeout": 1},
    )

    assert report["checks"]["reconciled"] is True
    assert report["ok"] is True


# Bonus: the other reconcile() failure branch (MigrationConflictError), never re-raised.
def test_reconcile_returns_false_on_migration_conflict_error(tmp_path, monkeypatch):
    def _raise(*args, **kwargs):
        raise MigrationConflictError("reconciliation target must be the canonical isolated shadow container")

    monkeypatch.setattr(runner, "reconcile_shadow", _raise)

    assert runner.reconcile(tmp_path / "receipt.json", tmp_path) is False


# main()'s happy path: prints valid JSON, exits 0/1 on ok, and --output writes the same JSON to disk.
# run_deterministic is monkeypatched (rather than routed through a subprocess stub) because main()
# intentionally has no CLI knob for injecting a custom subprocess runner.
def test_main_prints_and_writes_the_report_and_exits_zero_when_ok(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        runner,
        "run_deterministic",
        lambda families, **kwargs: {
            "checks": {"stub_family": True},
            "counts": {"stub_count": 1},
            "timings_ms": {"stub_ms": 1.0},
        },
    )
    output_path = tmp_path / "report.json"

    exit_code = runner.main(["--tier", "deterministic", "--skip-jest", "--output", str(output_path)])

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is True
    assert printed["tier"] == "deterministic"
    assert printed["checks"]["stub_family"] is True
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written == printed


def test_main_exits_one_when_a_check_is_false(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        runner,
        "run_deterministic",
        lambda families, **kwargs: {"checks": {"stub_family": False}, "counts": {}, "timings_ms": {}},
    )
    output_path = tmp_path / "report.json"

    exit_code = runner.main(["--tier", "deterministic", "--skip-jest", "--output", str(output_path)])

    assert exit_code == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is False
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written == printed


# A failing jest check in the full tier (without --skip-jest) must fail scope_ts specifically and ok overall.
def test_full_tier_with_a_failing_jest_check_fails_the_run(monkeypatch, key_file, receipt_and_home):
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    receipt, home = receipt_and_home
    api = FakeProviderAPI()
    monkeypatch.setattr(runner, "reconcile_shadow", lambda *args, **kwargs: GOLDEN_RECONCILE_RESULT)

    def _run(argv, **kwargs):
        del kwargs
        if argv[:2] == ["pnpm", "run"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="16 passed in 1.23s\n", stderr="")

    report = runner.build_report(
        tier="full",
        ledger=BudgetLedger(),
        subprocess_runner=_run,
        skip_jest=False,
        receipt=receipt,
        home=home,
        clock=lambda: 1.0,
        provider_kwargs={"api_key_file": key_file, "opener": api, "timeout": 1},
    )

    assert report["checks"]["scope_ts"] is False
    assert report["ok"] is False
    # every other check (deterministic node-based families + provider families + reconciled) still passed
    assert all(value for key, value in report["checks"].items() if key != "scope_ts")
