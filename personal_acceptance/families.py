"""Locked scenario-family roster for the bounded personal local acceptance suite."""

from __future__ import annotations

from dataclasses import dataclass, field

PROVIDER_ALLOWED = frozenset(
    {"supersession_live", "corroboration_live", "dispute_live", "reversion_live", "direct_vs_lifecycle_live"}
)


@dataclass(frozen=True)
class Family:
    name: str
    tier: str
    pytest_nodes: tuple = field(default_factory=tuple)
    jest_pattern: str | None = None
    planned_infer_writes: int = 0
    planned_mutations: int = 0


FAMILIES: tuple = (
    Family(
        "supersession_det",
        "deterministic",
        ("tests/personal_acceptance/test_lifecycle_families.py::test_supersession_family",),
    ),
    Family(
        "current_vs_historical",
        "deterministic",
        ("tests/personal_acceptance/test_lifecycle_families.py::test_current_vs_historical_family",),
    ),
    Family(
        "dispute_det", "deterministic", ("tests/personal_acceptance/test_lifecycle_families.py::test_dispute_family",)
    ),
    Family(
        "corroboration_det",
        "deterministic",
        ("tests/personal_acceptance/test_lifecycle_families.py::test_corroboration_family",),
    ),
    Family(
        "reversion_det",
        "deterministic",
        ("tests/personal_acceptance/test_lifecycle_families.py::test_reversion_family",),
    ),
    Family(
        "memory_kinds",
        "deterministic",
        ("tests/personal_acceptance/test_lifecycle_families.py::test_memory_kinds_family",),
    ),
    Family(
        "malformed_extraction",
        "deterministic",
        ("tests/personal_acceptance/test_failure_families.py::test_malformed_extraction_family",),
    ),
    Family(
        "persistence_atomicity",
        "deterministic",
        ("tests/personal_acceptance/test_failure_families.py::test_persistence_atomicity_family",),
    ),
    Family(
        "scope_python", "deterministic", ("tests/personal_acceptance/test_scope_families.py::test_scope_python_family",)
    ),
    Family(
        "scope_rest", "deterministic", ("tests/personal_acceptance/test_scope_families.py::test_scope_rest_family",)
    ),
    Family("scope_ts", "deterministic", jest_pattern="memory.add|memory.crud|memory.state-aware"),
    Family("scope_mcp", "deterministic", ("tests/personal_acceptance/test_scope_families.py::test_scope_mcp_family",)),
    Family(
        "direct_vs_lifecycle_det",
        "deterministic",
        ("tests/personal_acceptance/test_lifecycle_families.py::test_direct_vs_lifecycle_family",),
    ),
    Family(
        "legacy_ordering", "deterministic", ("tests/memory/test_state_aware_search.py", "tests/utils/test_scoring.py")
    ),
    Family(
        "lexical_unicode_ja",
        "deterministic",
        ("tests/personal_acceptance/test_recall_families.py::test_lexical_unicode_ja_family",),
    ),
    Family(
        "context_size",
        "deterministic",
        ("tests/personal_acceptance/test_recall_families.py::test_context_size_family",),
    ),
    Family("supersession_live", "provider", planned_infer_writes=2, planned_mutations=4),
    Family("corroboration_live", "provider", planned_infer_writes=2, planned_mutations=4),
    Family("dispute_live", "provider", planned_infer_writes=2, planned_mutations=4),
    Family("reversion_live", "provider", planned_infer_writes=3, planned_mutations=6),
    Family("direct_vs_lifecycle_live", "provider", planned_infer_writes=1, planned_mutations=4),
)


def deterministic_families() -> tuple:
    return tuple(family for family in FAMILIES if family.tier == "deterministic")


def provider_families() -> tuple:
    return tuple(family for family in FAMILIES if family.tier == "provider")
