from personal_acceptance.budget import BudgetLedger
from personal_acceptance.families import (
    FAMILIES,
    PROVIDER_ALLOWED,
    deterministic_families,
    provider_families,
)


def test_family_count_within_design_budget():
    assert 1 <= len(FAMILIES) <= 24
    assert len({family.name for family in FAMILIES}) == len(FAMILIES)


def test_only_allowed_families_spend_infer_writes():
    for family in FAMILIES:
        if family.planned_infer_writes:
            assert family.name in PROVIDER_ALLOWED
            assert family.tier == "provider"


def test_planned_budgets_fit_the_ledger():
    ledger = BudgetLedger()
    for family in FAMILIES:
        ledger.register_family(family.name)
    total_infer = sum(family.planned_infer_writes for family in FAMILIES)
    total_mutations = sum(family.planned_mutations for family in FAMILIES)
    assert total_infer <= ledger.max_infer_writes
    assert total_mutations <= ledger.max_mutations


def test_tiers_partition_and_targets_declared():
    names = {family.name for family in FAMILIES}
    assert {family.name for family in deterministic_families()} | {
        family.name for family in provider_families()
    } == names
    for family in deterministic_families():
        assert family.pytest_nodes or family.jest_pattern
    for family in provider_families():
        assert not family.pytest_nodes and family.jest_pattern is None
