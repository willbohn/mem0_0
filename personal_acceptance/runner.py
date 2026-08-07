"""Tiered runner CLI, content-free report, and reconciliation gate (PA Task 8).

Three tiers:

- ``deterministic`` -- runs the deterministic-tier families' pytest node IDs
  in one subprocess, plus (unless ``--skip-jest``) the TypeScript scope
  family via one jest subprocess. Never touches the shadow REST endpoint.
- ``provider`` -- runs the five provider-tier families through
  ``AcceptanceFlows`` against the shadow REST endpoint, then proves the
  canonical stores match one quiesced legacy receipt via ``reconcile_shadow``.
  Requires ``--receipt``: the reconciliation gate is mandatory for any
  shadow-touching run, so provider (and full) tiers refuse without it.
- ``full`` -- both of the above.

The report is scalar-only (bool/int/float leaves under ``checks``,
``counts``, ``timings_ms``, and ``budget``) and never carries a memory body,
ID, or scope value -- see ``build_report``'s docstring for the schema.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from deployment.mem0_0_arch.backup_restore import BackupVerificationError, reconcile_shadow
from deployment.mem0_0_arch.live_verify import DEFAULT_API_URL, DEFAULT_KEY_FILE
from deployment.mem0_0_arch.migration import MigrationConflictError, RuntimePaths
from personal_acceptance.budget import BudgetLedger
from personal_acceptance.families import FAMILIES, Family
from personal_acceptance.provider_flows import AcceptanceFlows

SHADOW_POSTGRES_CONTAINER = "mem0_0-arch-shadow-postgres-1"
DEFAULT_OUTPUT_PATH = Path(".personal-acceptance/report.json")
_PYTEST_SUMMARY_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|error|errors)\b")


def _parse_pytest_summary_counts(stdout: str) -> tuple[int, int]:
    """Extract only pass/fail counts from a pytest ``-q`` summary line, never test names or content."""
    passed = failed = 0
    for count, word in _PYTEST_SUMMARY_COUNT_RE.findall(stdout or ""):
        if word == "passed":
            passed += int(count)
        else:
            failed += int(count)
    return passed, failed


def run_deterministic(
    families: Iterable[Family],
    *,
    python_executable: str = sys.executable,
    runner: Callable[..., Any] = subprocess.run,
    skip_jest: bool = False,
) -> dict[str, Any]:
    """Run the deterministic-tier families and return per-family pass/fail booleans.

    All registered pytest node IDs run in ONE subprocess for speed. If that
    aggregate run fails, each family's own node list is re-run individually
    to attribute the failure -- only return codes are inspected, so
    attribution never requires reading test output content.
    """
    node_families = [family for family in families if family.pytest_nodes]
    jest_families = [family for family in families if family.jest_pattern]

    checks: dict[str, bool] = {}
    counts: dict[str, int] = {
        "pytest_nodes": 0,
        "pytest_passed": 0,
        "pytest_failed": 0,
        "jest_skipped": 0,
    }
    timings_ms: dict[str, float] = {}

    all_node_ids = [node_id for family in node_families for node_id in family.pytest_nodes]
    counts["pytest_nodes"] = len(all_node_ids)

    if all_node_ids:
        started = time.monotonic()
        result = runner(
            [python_executable, "-m", "pytest", "-q", "--tb=no", *all_node_ids],
            capture_output=True,
            text=True,
        )
        timings_ms["pytest_ms"] = round((time.monotonic() - started) * 1000, 3)
        passed, failed = _parse_pytest_summary_counts(getattr(result, "stdout", "") or "")
        counts["pytest_passed"] = passed
        counts["pytest_failed"] = failed

        if result.returncode == 0:
            for family in node_families:
                checks[family.name] = True
        else:
            for family in node_families:
                family_result = runner(
                    [python_executable, "-m", "pytest", "-q", "--tb=no", *family.pytest_nodes],
                    capture_output=True,
                    text=True,
                )
                checks[family.name] = family_result.returncode == 0

    if jest_families:
        if skip_jest:
            counts["jest_skipped"] = len(jest_families)
        else:
            patterns = {family.jest_pattern for family in jest_families if family.jest_pattern is not None}
            pattern = "|".join(sorted(patterns))
            started = time.monotonic()
            jest_result = runner(
                ["pnpm", "run", "test", "--", pattern],
                cwd="mem0-ts",
                capture_output=True,
                text=True,
            )
            timings_ms["jest_ms"] = round((time.monotonic() - started) * 1000, 3)
            for family in jest_families:
                checks[family.name] = jest_result.returncode == 0

    return {"checks": checks, "counts": counts, "timings_ms": timings_ms}


def run_provider(families: Iterable[Family], ledger: BudgetLedger, **flow_kwargs: Any) -> dict[str, Any]:
    """Run the five provider-tier families through one disposable ``AcceptanceFlows`` scope.

    Callers own the ledger's wall-clock gate: ``ledger.start_provider_clock(...)``
    must already have been called (``build_report`` does this before invoking
    ``run_provider``) -- without it the wall-clock cap silently never fires.
    """
    del families  # provider scenario coverage is intrinsic to AcceptanceFlows; kept for a uniform run_* signature
    flows = AcceptanceFlows(ledger=ledger, **flow_kwargs)
    report = flows.run()
    return {
        "checks": dict(report["checks"]),
        "counts": dict(report["counts"]),
        "timings_ms": dict(report["timings_ms"]),
    }


def reconcile(receipt: Path, home: Path) -> bool:
    """Prove the canonical stores match one quiesced legacy receipt; never re-raises.

    Returns True only when ``reconcile_shadow`` reports ``status == "reconciled"``.
    Any ``BackupVerificationError``/``MigrationConflictError`` fails the run
    (returns False) rather than propagating -- a shadow-touching run must
    always finish with a report, never crash on a reconciliation failure.
    """
    try:
        result = reconcile_shadow(
            RuntimePaths.for_home(Path(home)),
            Path(receipt),
            shadow_postgres_container=SHADOW_POSTGRES_CONTAINER,
        )
    except (BackupVerificationError, MigrationConflictError):
        return False
    return result.get("status") == "reconciled"


def build_report(
    *,
    tier: str,
    families: tuple[Family, ...] = FAMILIES,
    ledger: BudgetLedger | None = None,
    python_executable: str = sys.executable,
    subprocess_runner: Callable[..., Any] = subprocess.run,
    skip_jest: bool = False,
    receipt: Path | None = None,
    home: Path | None = None,
    clock: Callable[[], float] = time.monotonic,
    provider_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one tier and assemble the content-free, scalar-only report.

    Top-level keys are exactly ``schema_version``, ``acceptance_class``,
    ``ok``, ``tier``, ``checks``, ``counts``, ``timings_ms``, ``budget``.
    ``ok`` requires every entry in ``checks`` to be True -- for the
    provider/full tiers that set includes ``reconciled``, so a reconciliation
    failure always fails the run.
    """
    if tier not in {"deterministic", "provider", "full"}:
        raise ValueError(f"unknown tier: {tier!r}")

    ledger = ledger if ledger is not None else BudgetLedger()
    provider_kwargs = dict(provider_kwargs or {})

    checks: dict[str, bool] = {}
    counts: dict[str, int] = {}
    timings_ms: dict[str, float] = {}

    if tier in {"deterministic", "full"}:
        det_families = [family for family in families if family.tier == "deterministic"]
        for family in det_families:
            ledger.register_family(family.name)
        det_report = run_deterministic(
            det_families,
            python_executable=python_executable,
            runner=subprocess_runner,
            skip_jest=skip_jest,
        )
        checks.update(det_report["checks"])
        counts.update(det_report["counts"])
        timings_ms.update(det_report["timings_ms"])

    if tier in {"provider", "full"}:
        if receipt is None or home is None:
            raise ValueError("provider/full tiers require both receipt and home for the reconciliation gate")
        prov_families = [family for family in families if family.tier == "provider"]
        for family in prov_families:
            ledger.register_family(family.name)
        ledger.start_provider_clock(clock())
        prov_report = run_provider(prov_families, ledger, clock=clock, **provider_kwargs)
        checks.update(prov_report["checks"])
        counts.update(prov_report["counts"])
        timings_ms.update(prov_report["timings_ms"])
        checks["reconciled"] = reconcile(receipt, home)

    ok = bool(checks) and all(checks.values())
    return {
        "schema_version": 1,
        "acceptance_class": "bounded_personal_local",
        "ok": ok,
        "tier": tier,
        "checks": checks,
        "counts": counts,
        "timings_ms": timings_ms,
        "budget": ledger.snapshot(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the bounded personal local acceptance suite")
    parser.add_argument("--tier", choices=("deterministic", "provider", "full"), required=True)
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--api-key-file", type=Path, default=DEFAULT_KEY_FILE)
    parser.add_argument("--history-db", type=Path, default=None)
    parser.add_argument("--receipt", type=Path, default=None)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--skip-jest", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args(argv)

    if args.tier in {"provider", "full"} and args.receipt is None:
        print(
            "error: --receipt is required for provider/full tiers (the reconciliation gate is mandatory)",
            file=sys.stderr,
        )
        return 2

    report = build_report(
        tier=args.tier,
        ledger=BudgetLedger(),
        skip_jest=args.skip_jest,
        receipt=args.receipt,
        home=args.home,
        provider_kwargs={
            "api_url": args.api_url,
            "api_key_file": args.api_key_file,
            "history_db_path": args.history_db,
        },
    )

    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
