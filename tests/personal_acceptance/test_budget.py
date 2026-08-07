import pytest

from personal_acceptance.budget import BudgetExceeded, BudgetLedger


def test_family_limit_enforced_before_registration():
    ledger = BudgetLedger(max_families=2)
    ledger.register_family("a")
    ledger.register_family("b")
    with pytest.raises(BudgetExceeded) as excinfo:
        ledger.register_family("c")
    assert excinfo.value.limit == "families"
    assert ledger.snapshot()["families"] == 2


def test_duplicate_family_registration_rejected():
    ledger = BudgetLedger()
    ledger.register_family("a")
    with pytest.raises(ValueError):
        ledger.register_family("a")


def test_mutation_and_infer_budgets():
    ledger = BudgetLedger(max_mutations=3, max_infer_writes=1)
    ledger.record_infer_write()          # 1 infer, 1 mutation
    ledger.record_mutation("delete")     # 2 mutations
    with pytest.raises(BudgetExceeded) as excinfo:
        ledger.record_infer_write()      # infer limit hit first
    assert excinfo.value.limit == "infer_writes"
    ledger.record_mutation("write")      # 3 mutations
    with pytest.raises(BudgetExceeded) as excinfo:
        ledger.record_mutation("update")
    assert excinfo.value.limit == "mutations"


def test_mutation_kind_validated():
    ledger = BudgetLedger()
    with pytest.raises(ValueError):
        ledger.record_mutation("upsert")


def test_provider_wall_clock_fails_run_not_extends():
    ledger = BudgetLedger(provider_wall_clock_seconds=100.0)
    ledger.start_provider_clock(now=1000.0)
    ledger.check_provider_clock(now=1099.9)
    with pytest.raises(BudgetExceeded) as excinfo:
        ledger.check_provider_clock(now=1100.1)
    assert excinfo.value.limit == "provider_wall_clock"


def test_snapshot_is_scalar_only():
    ledger = BudgetLedger()
    ledger.register_family("a")
    ledger.record_infer_write()
    snap = ledger.snapshot()
    assert set(snap) == {"families", "mutations", "infer_writes", "provider_elapsed_seconds"}
    assert all(isinstance(value, (bool, int, float)) for value in snap.values())
