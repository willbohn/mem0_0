# Bounded Personal Local Acceptance Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the bounded personal local acceptance suite defined by the amended stabilization design — a budget-enforced, content-free acceptance runner with 21 scenario families (16 deterministic, 5 provider-backed) that provides bounded local confidence and can never be mistaken for official benchmark acceptance.

**Architecture:** A new top-level `personal_acceptance/` package (sibling of `evaluation_overlay/`, excluded from the wheel by hatch's `only-include=["mem0"]`). A `BudgetLedger` enforces the design's hard budgets. A families registry maps each family to either pytest node IDs (deterministic tier — new scenario tests under `tests/personal_acceptance/` built on the repo's proven doubles) or provider flows (a `LiveVerifier` subclass reusing its unique-scope, exact-cleanup, and SQLite-drain machinery against the shadow REST endpoint). The runner CLI executes a tier, enforces the wall-clock cap, ends shadow-touching runs with `reconcile_shadow`, and emits a scalar-only JSON report.

**Tech Stack:** Python 3.9+ stdlib only for the package (pytest/jest invoked as subprocesses); reuses `deployment.mem0_0_arch.live_verify`, `deployment.mem0_0_arch.backup_restore`, and the existing test fixture patterns.

## Global Constraints

- Budgets, verbatim from the design (docs/superpowers/specs/2026-08-07-mem0-0-stabilization-and-omp-design.md): **no more than 24 scenario families; no more than 120 total memory mutations where every write, update, and delete — including cleanup — counts; no more than 48 provider-backed `infer=true` writes, reserved exclusively for the supersession, corroboration, dispute, reversion, and direct-write-versus-lifecycle families; a wall-clock cap of 100 minutes on the provider-backed portion, where exceeding the cap fails the run rather than extending it; one local model configuration, no paid provider; no retry beyond the existing fail-closed request contract; no personal memory bodies in reports or committed artifacts.**
- The suite runs against an isolated store scope and never against the migrated legacy data. Any run touching the shadow deployment ends with receipt reconciliation against the current migration receipt, and that reconciliation must pass for the run to count.
- Naming firewall: every report carries `"acceptance_class": "bounded_personal_local"`; no report, doc, or log may mention STALE, Memora, FAMA, LoCoMo, LongMemEval, or BEAM except to disclaim them.
- Reports contain ONLY booleans, ints, and floats as leaf values (the `live_verify` convention) — no memory bodies, IDs, scope values, or key material.
- Provider tier: loopback HTTP only; `X-API-Key` from a 0600 key file; construction refuses if env `MEM0_API_KEY` is set (inherited from `LiveVerifier`); shadow endpoint default `http://127.0.0.1:28889`; no push, no deployment change, no legacy-data access.
- No new mandatory dependency; Ruff line length 120; the hatch test script is `pytest tests/ {args}` — use `-k` to narrow. The lint hook auto-runs per edit; fix only what it reports in changed code.
- Deterministic tests must pass without any provider, network, Redis, or Postgres (the known environmental failures — Redis FT._LIST without the fixture, JWT_SECRET order-coupling in whole-tree runs — must not be triggered by the new tests in `-k personal_acceptance` runs).
- Before each commit: `git add` exact files, `git diff --cached --stat`, `git diff --cached --check`; multi-file commits run the sweep gate `bash "${XDG_DATA_HOME:-$HOME/.local/share}/agent-harness/scripts/agent-glue-check.sh"` (known environmental exit-1 on pre-existing YAML outside your files; gitleaks must be clean).
- End every commit message with: `Claude-Session: https://claude.ai/code/session_01DFFDonysTLJjm22kqmh44f`
- Pinning-test discipline (families that pin verified existing behavior — dispute, corroboration, reversion, kinds): these must pass by inspecting and asserting the ACTUAL current lifecycle behavior. If observed behavior looks wrong, STOP and report BLOCKED — never adjust production code or write a test that asserts what you guess "should" happen.

## The Family Roster (locked — 21 of 24 allowed)

Deterministic tier (16): `supersession_det`, `current_vs_historical`, `dispute_det`, `corroboration_det`, `reversion_det`, `memory_kinds` (preference/event/relationship/instruction/procedure as one family), `malformed_extraction`, `persistence_atomicity`, `scope_python`, `scope_rest`, `scope_ts` (jest subprocess), `scope_mcp`, `direct_vs_lifecycle_det`, `legacy_ordering` (existing pins), `lexical_unicode_ja`, `context_size`.
Provider tier (5, the only ones allowed to spend `infer=true` writes): `supersession_live` (2 writes), `corroboration_live` (2), `dispute_live` (2), `reversion_live` (3), `direct_vs_lifecycle_live` (1 `infer=true` + 1 `infer=false`).
Planned provider spend: 10 `infer=true` writes of 48; ~25 total mutations (writes + per-ID cleanup deletes) of 120 — wide headroom is deliberate.

---

### Task 1: Package scaffold and BudgetLedger

**Files:**
- Create: `personal_acceptance/__init__.py`
- Create: `personal_acceptance/budget.py`
- Create: `tests/personal_acceptance/__init__.py` (empty)
- Test: `tests/personal_acceptance/test_budget.py`

**Interfaces:**
- Produces: `BudgetLedger(max_families=24, max_mutations=120, max_infer_writes=48, provider_wall_clock_seconds=6000.0)` with methods `register_family(name: str) -> None`, `record_mutation(kind: str) -> None` (kind in {"write", "update", "delete"}), `record_infer_write() -> None` (also counts a mutation), `start_provider_clock(now: float) -> None`, `check_provider_clock(now: float) -> None`, `snapshot() -> dict` (scalar-only counts); exception `BudgetExceeded(RuntimeError)` carrying `.limit` (str). All checks are enforced BEFORE the action (register/record raise when the action would exceed the limit).

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run, verify FAIL** — `uvx hatch run dev_py_3_12:test -q -k test_budget` → ModuleNotFoundError.

- [ ] **Step 3: Implement**

`personal_acceptance/__init__.py`:

```python
"""Bounded personal local acceptance suite.

Provides bounded local confidence for the state-aware fork. This suite is
NOT an official benchmark and must never be presented as official
benchmark acceptance.
"""
```

`personal_acceptance/budget.py`:

```python
"""Hard budget enforcement for the bounded personal local acceptance suite."""

from __future__ import annotations

_MUTATION_KINDS = frozenset({"write", "update", "delete"})


class BudgetExceeded(RuntimeError):
    """Raised before any action that would exceed a suite budget."""

    def __init__(self, limit: str, message: str):
        super().__init__(message)
        self.limit = limit


class BudgetLedger:
    """Tracks the design's hard budgets; every check runs before the action."""

    def __init__(
        self,
        *,
        max_families: int = 24,
        max_mutations: int = 120,
        max_infer_writes: int = 48,
        provider_wall_clock_seconds: float = 6000.0,
    ) -> None:
        self.max_families = int(max_families)
        self.max_mutations = int(max_mutations)
        self.max_infer_writes = int(max_infer_writes)
        self.provider_wall_clock_seconds = float(provider_wall_clock_seconds)
        self._families: set[str] = set()
        self._mutations = 0
        self._infer_writes = 0
        self._provider_started: float | None = None
        self._provider_elapsed = 0.0

    def register_family(self, name: str) -> None:
        if name in self._families:
            raise ValueError(f"family already registered: {name}")
        if len(self._families) + 1 > self.max_families:
            raise BudgetExceeded("families", f"family budget of {self.max_families} would be exceeded")
        self._families.add(name)

    def record_mutation(self, kind: str) -> None:
        if kind not in _MUTATION_KINDS:
            raise ValueError(f"unknown mutation kind: {kind}")
        if self._mutations + 1 > self.max_mutations:
            raise BudgetExceeded("mutations", f"mutation budget of {self.max_mutations} would be exceeded")
        self._mutations += 1

    def record_infer_write(self) -> None:
        if self._infer_writes + 1 > self.max_infer_writes:
            raise BudgetExceeded("infer_writes", f"infer=true budget of {self.max_infer_writes} would be exceeded")
        self.record_mutation("write")
        self._infer_writes += 1

    def start_provider_clock(self, now: float) -> None:
        self._provider_started = float(now)

    def check_provider_clock(self, now: float) -> None:
        if self._provider_started is None:
            return
        self._provider_elapsed = float(now) - self._provider_started
        if self._provider_elapsed > self.provider_wall_clock_seconds:
            raise BudgetExceeded(
                "provider_wall_clock",
                f"provider wall-clock cap of {self.provider_wall_clock_seconds}s exceeded; the run fails",
            )

    def snapshot(self) -> dict:
        return {
            "families": len(self._families),
            "mutations": self._mutations,
            "infer_writes": self._infer_writes,
            "provider_elapsed_seconds": float(self._provider_elapsed),
        }
```

- [ ] **Step 4: Run, verify PASS** — same command.
- [ ] **Step 5: Commit** — `git add personal_acceptance/ tests/personal_acceptance/ && git commit -m "feat(acceptance): add personal-acceptance package with budget ledger"` (sweep gate first).

### Task 2: Families registry with budget-arithmetic pins

**Files:**
- Create: `personal_acceptance/families.py`
- Test: `tests/personal_acceptance/test_families.py`

**Interfaces:**
- Produces: `FAMILIES: tuple[Family, ...]` where `Family` is a frozen dataclass with fields `name: str`, `tier: str` ("deterministic" | "provider"), `pytest_nodes: tuple[str, ...]` (deterministic Python families), `jest_pattern: str | None` (the `scope_ts` family only), `planned_infer_writes: int`, `planned_mutations: int`; helpers `deterministic_families() -> tuple[Family, ...]`, `provider_families() -> tuple[Family, ...]`, `PROVIDER_ALLOWED = frozenset({"supersession_live", "corroboration_live", "dispute_live", "reversion_live", "direct_vs_lifecycle_live"})`.
- Consumes: `BudgetLedger` defaults from Task 1.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run, verify FAIL** — ModuleNotFoundError for families.

- [ ] **Step 3: Implement** `personal_acceptance/families.py` with the locked roster. Deterministic `pytest_nodes` reference the test files Tasks 3-7 create (file-level node IDs are sufficient: e.g. `"tests/personal_acceptance/test_lifecycle_families.py::test_supersession_family"`), plus these EXISTING pins reused verbatim: `legacy_ordering` → `("tests/memory/test_state_aware_search.py", "tests/utils/test_scoring.py")`; `scope_ts` → `jest_pattern="memory.add|memory.crud|memory.state-aware"`. Provider families carry the planned spend from the roster table (2/2/2/3 infer writes and `direct_vs_lifecycle_live` with `planned_infer_writes=1, planned_mutations=2` for its extra `infer=false` write) plus per-family cleanup mutations (each provider family plans `writes + writes` mutations: every write is later deleted one-by-one). Full literal:

```python
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
    Family("supersession_det", "deterministic", ("tests/personal_acceptance/test_lifecycle_families.py::test_supersession_family",)),
    Family("current_vs_historical", "deterministic", ("tests/personal_acceptance/test_lifecycle_families.py::test_current_vs_historical_family",)),
    Family("dispute_det", "deterministic", ("tests/personal_acceptance/test_lifecycle_families.py::test_dispute_family",)),
    Family("corroboration_det", "deterministic", ("tests/personal_acceptance/test_lifecycle_families.py::test_corroboration_family",)),
    Family("reversion_det", "deterministic", ("tests/personal_acceptance/test_lifecycle_families.py::test_reversion_family",)),
    Family("memory_kinds", "deterministic", ("tests/personal_acceptance/test_lifecycle_families.py::test_memory_kinds_family",)),
    Family("malformed_extraction", "deterministic", ("tests/personal_acceptance/test_failure_families.py::test_malformed_extraction_family",)),
    Family("persistence_atomicity", "deterministic", ("tests/personal_acceptance/test_failure_families.py::test_persistence_atomicity_family",)),
    Family("scope_python", "deterministic", ("tests/personal_acceptance/test_scope_families.py::test_scope_python_family",)),
    Family("scope_rest", "deterministic", ("tests/personal_acceptance/test_scope_families.py::test_scope_rest_family",)),
    Family("scope_ts", "deterministic", jest_pattern="memory.add|memory.crud|memory.state-aware"),
    Family("scope_mcp", "deterministic", ("tests/personal_acceptance/test_scope_families.py::test_scope_mcp_family",)),
    Family("direct_vs_lifecycle_det", "deterministic", ("tests/personal_acceptance/test_lifecycle_families.py::test_direct_vs_lifecycle_family",)),
    Family("legacy_ordering", "deterministic", ("tests/memory/test_state_aware_search.py", "tests/utils/test_scoring.py")),
    Family("lexical_unicode_ja", "deterministic", ("tests/personal_acceptance/test_recall_families.py::test_lexical_unicode_ja_family",)),
    Family("context_size", "deterministic", ("tests/personal_acceptance/test_recall_families.py::test_context_size_family",)),
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
```

- [ ] **Step 4: Run, verify PASS** — `uvx hatch run dev_py_3_12:test -q -k test_families`.
- [ ] **Step 5: Commit** — `git add personal_acceptance/families.py tests/personal_acceptance/test_families.py && git commit -m "feat(acceptance): lock the 21-family roster with budget-arithmetic pins"`.

### Task 3: Deterministic lifecycle families

**Files:**
- Create: `tests/personal_acceptance/conftest.py` (the shared in-process harness)
- Test: `tests/personal_acceptance/test_lifecycle_families.py`

**Interfaces:**
- Produces (conftest): `class InMemoryVectorStore` (copy the exact class from tests/memory/test_state_aware_end_to_end.py:9-35 — `_mem0_supports_complete_list = True`, `records: dict`, `insert(vectors, ids, payloads)` storing `SimpleNamespace(id=str(memory_id), score=0.9, payload=dict(payload))`, `update/delete/get`, `search(**_)`/`keyword_search(**_)` returning `list(self.records.values())`, `list(**_)` returning `(values, None)`); `extraction_json(*items) -> str` returning `json.dumps({"memory": list(items)})`; `extraction_item(text, value, *, kind="state", subject="user", predicate="residence", excerpt=None, relation="supersedes", extra=None) -> dict` building `{"id": "0", "text": text, "kind": kind, "subject": subject, "predicate": predicate, "value": value, "attributed_to": "user", "evidence": {"message_index": 0, "excerpt": excerpt or value}}` plus `relation` when not None and `**(extra or {})`; fixture `scripted_memory(monkeypatch)` — a factory `def _build(*responses: str) -> Memory` that constructs `Memory.__new__(Memory)` exactly per tests/memory/test_state_aware_end_to_end.py:59-79 (MemoryConfig(), custom_instructions=None, api_version="v1.1", MagicMock llm with `generate_response.side_effect = list(responses)`, MagicMock embedder with `embed.return_value=[0.1, 0.2]` and `embed_batch.side_effect = lambda texts, _action: [[0.1, 0.2] for _ in texts]`, `InMemoryVectorStore()`, MagicMock db with `get_last_messages.return_value=[]`, `_entity_store=None`, `reranker=None`) and monkeypatches `mem0.memory.main.capture_event`, `mem0.memory.main.display_first_run_notice`, `mem0.memory.main.extract_entities` to no-ops.
- Consumes: nothing from other tasks (pure test tier).

- [ ] **Step 1: Write conftest.py exactly as specified above** (it is shared infrastructure; no test yet). Then write the six family tests. `test_supersession_family` and `test_current_vs_historical_family` mirror tests/memory/test_state_aware_end_to_end.py:81-107 with fresh content (state "residence": "Sapporo" superseded by "Fukuoka"; current search returns only the correction; dated historical search returns only the original; no `_mem0_status` in any public result repr). `test_direct_vs_lifecycle_family`: one `infer=False` add (raw event kind, inspect `store.records` payload `_mem0_kind == "event"`, no supersession fields) versus one scripted `infer=True` add (kind "state"); assert the raw add never triggered `llm.generate_response`. `test_memory_kinds_family`: one scripted add with five extraction items (kinds preference/event/relationship/instruction/procedure, distinct predicates, `relation=None`) then assert five records stored with the expected `_mem0_kind` payload values and that `search` returns all five without `_mem0_*` keys in metadata.

For the three PINNING tests (dispute, corroboration, reversion), follow the pinning discipline from Global Constraints: script the canonical scenario, then assert the ACTUAL stored `_mem0_status`/`_mem0_relation` dispositions after inspecting `mem0/memory/lifecycle.py`'s dispute/corroboration/reversion handling (grep `_mem0_status` transitions and the `disputed` literal). Scenario scripts:

```python
def test_dispute_family(scripted_memory):
    # Two adds, same subject/predicate ("residence"), different values, NO explicit relation.
    memory = scripted_memory(
        extraction_json(extraction_item("User lives in Nara", "Nara", relation=None)),
        extraction_json(extraction_item("User lives in Otaru", "Otaru", relation=None)),
    )
    memory.add([{"role": "user", "content": "I live in Nara"}], user_id="u1")
    memory.add([{"role": "user", "content": "I live in Otaru"}], user_id="u1")
    statuses = sorted(
        record.payload.get("_mem0_status") for record in memory.vector_store.records.values()
    )
    # PIN the actual disposition (expected from the handover: ambiguous same-key values become disputed).
    assert statuses == ["disputed", "disputed"]
```

`test_corroboration_family`: second add repeats the SAME value ("Nara") with `relation="corroborates"`; assert no second competing active state (pin the actual outcome: either a single active record with strengthened evidence — check `_mem0_source_hashes` length — or an active original plus a corroborating record; assert whichever the code actually does, with a comment stating the pinned contract). `test_reversion_family`: three adds A→B→A (relation "supersedes" each time); assert the final active canonical value is A's text, the middle value's record is superseded, and current search returns only the reverted-to value.

- [ ] **Step 2: Run — pinning tests must pass immediately; scenario tests RED only if conftest is wrong.** `uvx hatch run dev_py_3_12:test -q -k "test_lifecycle_families"`. Expected: all six pass (this tier pins verified behavior). Any assertion failure = STOP, report BLOCKED with the observed disposition.
- [ ] **Step 3: Commit** — `git add tests/personal_acceptance/ && git commit -m "test(acceptance): add deterministic lifecycle scenario families"`.

### Task 4: Failure families (malformed extraction, persistence atomicity)

**Files:**
- Test: `tests/personal_acceptance/test_failure_families.py`

**Interfaces:** consumes conftest from Task 3; consumes `ExtractionParseError` and `MemoryPersistenceError` from `mem0.exceptions`.

- [ ] **Step 1: Write the failing-then-passing tests**

```python
import json

import pytest

from mem0.exceptions import ExtractionParseError, MemoryPersistenceError


def test_malformed_extraction_family(scripted_memory):
    # Non-object response -> typed parse error, nothing persisted.
    memory = scripted_memory('"bare string"')
    with pytest.raises(ExtractionParseError):
        memory.add([{"role": "user", "content": "hello"}], user_id="u1")
    assert memory.vector_store.records == {}

    # Mixed valid/invalid items -> valid persisted, skip count disclosed.
    memory = scripted_memory(
        json.dumps({"memory": ["junk", 42, extraction_item("User prefers tea", "tea", kind="preference",
                                                            predicate="drink", relation=None,
                                                            excerpt="prefer tea")]})
    )
    response = memory.add([{"role": "user", "content": "I prefer tea"}], user_id="u1")
    assert response["skipped_invalid_extractions"] == 2
    assert len(response["results"]) == 1


def test_persistence_atomicity_family(scripted_memory):
    # Embedding failure mid-batch must not leave partial writes (complete-embedding-before-persistence).
    memory = scripted_memory(
        extraction_json(
            extraction_item("User lives in Toyama", "Toyama", relation=None),
            extraction_item("User works remotely", "remote", predicate="workstyle", relation=None,
                            excerpt="Toyama"),
        )
    )
    memory.embedding_model.embed_batch.side_effect = RuntimeError("embedder down")
    with pytest.raises(Exception):
        memory.add([{"role": "user", "content": "I moved to Toyama"}], user_id="u1")
    assert memory.vector_store.records == {}
```

(For the atomicity test, first read how the state-aware batch handles embed failure — the fork raises a typed error per `mem0/memory/main.py`'s complete-embedding-before-persistence block, pinned by tests/memory/test_state_aware_writes.py:244-292; tighten `pytest.raises(Exception)` to the ACTUAL exception type (`EmbeddingError` or `MemoryPersistenceError`) once observed, and add a second scenario copying the partial-insert compensation pattern from tests/memory/test_state_aware_writes.py:244-292 — insert succeeds for one ID then fails, assert compensating deletion ran via the store double's `delete` calls and `MemoryPersistenceError.planned_ids`.)

- [ ] **Step 2: Run** — `uvx hatch run dev_py_3_12:test -q -k test_failure_families` → PASS (pins the stabilization behavior). STOP if anything fails.
- [ ] **Step 3: Commit** — `git add tests/personal_acceptance/test_failure_families.py && git commit -m "test(acceptance): add malformed-extraction and atomicity families"`.

### Task 5: Scope families (Python, REST, MCP)

**Files:**
- Test: `tests/personal_acceptance/test_scope_families.py`

**Interfaces:**
- Consumes: conftest harness (Python family); the FakeMem0 bridge harness pattern from tests/deployment/mem0_0_arch/test_bridge.py:19-193 (import the bridge module the same way: `importlib.util.spec_from_file_location("mem0_0_arch_bridge", BRIDGE_PATH)`); the server TestClient fixture chain pattern from tests/test_server_params.py:26-73 (with `pytest.importorskip("fastapi")` and the env dict `{"OPENAI_API_KEY": "fake-key", "ADMIN_API_KEY": "", "AUTH_DISABLED": "true"}` in place BEFORE importing/reloading server.main, plus the server-dir `sys.path.insert` that tests/test_api_keys_router.py:22-26 documents).

- [ ] **Step 1: Write the three family tests**

`test_scope_python_family`: build a `scripted_memory` with two `infer=False` adds under `user_id="tenant-a"` and `user_id="tenant-b"`; assert `get_all(filters={"user_id": "tenant-a"})` returns only tenant-a's row and that `add(..., run_id="r1", metadata={"user_id": "tenant-b"})` does NOT place a row into tenant-b's scope (the stabilized identity-strip); zero cross-scope rows in both directions.

`test_scope_rest_family`: replicate the `_mock_memory`/`client` fixture chain locally (copy tests/test_server_params.py:26-68 into this file as fixtures, including `pytest.importorskip("fastapi")` at module top and the `sys.path` server-dir insert BEFORE `import server.main`); drive `GET /memories` scoped vs admin with a payload containing `text_lemmatized` and `_mem0_status` on the mocked `vector_store.list`; assert the admin listing's `metadata` contains neither, and scoped requests forward exact filters to `Memory.get_all` (`mock_memory.get_all.call_args`).

`test_scope_mcp_family`: with the FakeMem0 harness (ephemeral-port ThreadingHTTPServer, 0600 key file), add one memory in scope A (the five-field fixture scope) and one in scope B (different `project_id`); assert `search_memories`/`get_memories` under scope A never return scope B's row; `get_memory` on B's ID under scope A raises `BridgeError` with code `scope_mismatch`; `delete_memory` without `confirm` raises `confirmation_required`; a secret-bearing query raises `secret_detected`.

- [ ] **Step 2: Run** — `uvx hatch run dev_py_3_12:test -q -k test_scope_families` → PASS immediately (pins). STOP on any failure.
- [ ] **Step 3: Commit** — `git add tests/personal_acceptance/test_scope_families.py && git commit -m "test(acceptance): add Python, REST, and MCP scope-isolation families"`.

### Task 6: Recall families (legacy ordering pointer, Japanese lexical, context size)

**Files:**
- Test: `tests/personal_acceptance/test_recall_families.py`

**Interfaces:** consumes conftest; consumes `unicode_lexical_terms` from `mem0.memory.lifecycle` and the `make_memory(mode=...)` search-double pattern from tests/memory/test_state_aware_search.py:11-47 (copy `stored()` and `CompleteSearchVectorStore` locally rather than importing across test modules).

- [ ] **Step 1: Write the two new tests** (legacy ordering itself is covered by the registry pointing at the existing pinned files — no new test).

`test_lexical_unicode_ja_family`: store two rows whose `data` are Japanese strings (synthetic fixtures, e.g. `"ユーザーは京都に住んでいる"` and `"ユーザーは紅茶が好き"`); with `mem0.memory.main.lemmatize_for_bm25` monkeypatched to the REAL no-spaCy fallback path (monkeypatch `mem0.utils.lemmatization.get_nlp_lemma` — check the actual accessor name in mem0/utils/lemmatization.py:30-36 — to return None so `unicode_lexical_terms` engages), run `_search_vector_store` with the Japanese query `"京都"` under state-aware mode and assert the residence row ranks/returns and the tea row does not leak `_mem0_*` metadata; additionally assert `unicode_lexical_terms("京都に住む")` yields n-gram terms containing `"京都"` (direct unit pin of the fallback).

`test_context_size_family`: (a) SDK cumulative budget — via `reciprocal_rank_fusion` mirroring tests/memory/test_lifecycle.py:205-221's invocation with `max_chars` equal to the first text's length: assert the second result is excluded and a shorter third result IS admitted (skip-not-break); (b) bridge budget — with the FakeMem0 harness, one long and one short memory, `search_memories` with `max_chars=100`: assert the long row omitted whole, the short row present, `omitted_for_budget == 1`, and the REST body carried no `max_chars`.

- [ ] **Step 2: Run** — `uvx hatch run dev_py_3_12:test -q -k test_recall_families` → PASS. STOP on failure.
- [ ] **Step 3: Commit** — `git add tests/personal_acceptance/test_recall_families.py && git commit -m "test(acceptance): add Japanese lexical and context-size families"`.

### Task 7: Provider flows (LiveVerifier subclass)

**Files:**
- Create: `personal_acceptance/provider_flows.py`
- Test: `tests/personal_acceptance/test_provider_flows.py`

**Interfaces:**
- Produces: `class AcceptanceFlows(LiveVerifier)` (import `LiveVerifier` and its constants from `deployment.mem0_0_arch.live_verify` via the same `importlib` file-location pattern the bridge tests use IF the deployment package imports cleanly — first try plain `from deployment.mem0_0_arch.live_verify import LiveVerifier, ConfigurationError, VerificationError` (the package has `__init__.py`; readiness is invoked as `python -m deployment.mem0_0_arch.readiness`, so plain import works)). Constructor `AcceptanceFlows(*, ledger: BudgetLedger, clock: Callable[[], float] = time.monotonic, **kwargs)` — stores the ledger/clock, passes `**kwargs` (api_url, api_key_file, history_db_path, timeout, opener) to `LiveVerifier.__init__`. Overrides `_run_flow(self) -> None` to execute the five provider families sequentially, each as a private method returning None and setting `self.checks[...]` booleans; every REST write goes through a guarded helper `_infer_add(self, content: str, *, created_at: str) -> str` that calls `self.ledger.check_provider_clock(self.clock())`, `self.ledger.record_infer_write()`, then the inherited `_add(content, created_at=created_at)` and returns the created memory ID. `run()` is inherited — it already guarantees cleanup of the unique scope even on failure; cleanup deletes are counted by overriding `_cleanup` to wrap the parent and add `record_mutation("delete")` per deleted ID afterwards from the reported count (read `self.counts` the parent maintains — verify the exact attribute in live_verify.py:303-349 and mirror it).
- Report checks produced (exact keys): `supersession_live_ok`, `corroboration_live_ok`, `dispute_live_ok`, `reversion_live_ok`, `direct_vs_lifecycle_live_ok`, plus the inherited cleanup/history booleans.
- Family flows (10 `infer=true` writes total + 1 `infer=false`): supersession — add "City preference is Kumamoto" then "City preference changed to Matsue"; search current must include matsue-text and exclude kumamoto-text; historical must include kumamoto. corroboration — add same durable fact twice; current search returns exactly one canonical result. dispute — add two conflicting same-key facts (no correction language); current search must NOT present both as a single canonical answer (assert result count and that audit completes). reversion — A, then B, then A again; current returns A-text. direct_vs_lifecycle — one `infer=true` durable state add + one `infer=false` raw add (`_request("POST", "/memories", body={..., "infer": False})` mirroring the inherited `_add` but with `infer: False` — count via `record_mutation("write")`, not `record_infer_write`); search returns both; the raw one is retrievable verbatim.
- All content is synthetic city/beverage fixtures; NO body text ever enters `self.checks`/`self.counts`.

- [ ] **Step 1: Write the offline tests** using the injected-opener seam (`opener=fake`) exactly like tests/deployment/mem0_0_arch/test_live_verify.py's `FakeLiveAPI` (build a minimal callable fake in this file: routes POST /memories → `{"results": [{"id": f"pa-{n}"}]}`, POST /search → canned per-query results the flows expect, GET /memories → scoped listing for cleanup, DELETE → success; record every request). Tests: (a) a full `run()` with the fake sets all five family checks True, `ok` True, and the ledger snapshot shows exactly 11 writes (10 infer + 1 raw) and delete mutations equal to created IDs; (b) a fake whose current-search returns the superseded text makes `supersession_live_ok` False and `ok` False, yet cleanup still ran (deletes recorded); (c) a `clock` advancing past the cap between writes raises nothing outward but fails the run with `checks["budget_ok"] is False` — decide: `_infer_add` lets `BudgetExceeded` propagate to `_run_flow`'s caller; the inherited `run()` catches flow exceptions and still cleans up (verify against live_verify.py:446-456), so assert `ok` False + cleanup ran + ledger snapshot's `provider_elapsed_seconds > cap`; (d) constructing with env `MEM0_API_KEY` set raises `ConfigurationError` (inherited); (e) the rendered `json.dumps` of the report contains none of the synthetic city strings.

- [ ] **Step 2: Run, verify FAIL** — ModuleNotFoundError → implement `provider_flows.py` per the interface above (read live_verify.py first; reuse `_add`, `_search_texts`-equivalent helpers — the parent's `_results`/`_memory_texts` — and DO NOT reimplement cleanup).
- [ ] **Step 3: Run, verify PASS** — `uvx hatch run dev_py_3_12:test -q -k test_provider_flows`.
- [ ] **Step 4: Commit** — `git add personal_acceptance/provider_flows.py tests/personal_acceptance/test_provider_flows.py && git commit -m "feat(acceptance): add budget-guarded provider flows on the live-verifier seam"` (sweep gate first).

### Task 8: Runner CLI, content-free report, reconciliation hook

**Files:**
- Create: `personal_acceptance/runner.py`
- Modify: `.gitignore` (add one line: `.personal-acceptance/`)
- Test: `tests/personal_acceptance/test_runner.py`

**Interfaces:**
- Produces: `python -m personal_acceptance.runner --tier {deterministic,provider,full} [--api-url URL] [--api-key-file PATH] [--history-db PATH] [--receipt PATH] [--home PATH] [--skip-jest] [--output PATH]`. Functions: `run_deterministic(families, *, python_executable=sys.executable, runner=subprocess.run) -> dict` — one pytest subprocess (`[python_executable, "-m", "pytest", "-q", *all_node_ids]`) plus, unless `--skip-jest`, one jest subprocess (`["pnpm", "run", "test", "--", jest_pattern]` with `cwd="mem0-ts"`); returns per-family booleans from return codes (pytest node failures map to families by node prefix — run pytest ONCE with `--tb=no -q` and parse the summary line only for pass/fail counts; on any failure re-run per-family node lists to attribute, still content-free). `run_provider(families, ledger, **flow_kwargs) -> dict` — constructs `AcceptanceFlows`, calls `run()`, merges its report. `reconcile(receipt: Path, home: Path) -> bool` — calls `deployment.mem0_0_arch.backup_restore.reconcile_shadow(RuntimePaths.for_home(home), receipt, shadow_postgres_container="mem0_0-arch-shadow-postgres-1")` and returns True on `status == "reconciled"`, False on any `BackupVerificationError`/`MigrationConflictError` (never re-raises; failure fails the run). `build_report(...) -> dict` — top-level keys exactly `{"schema_version": 1, "acceptance_class": "bounded_personal_local", "ok": bool, "tier": str, "checks": {...bools}, "counts": {...ints}, "timings_ms": {...floats}, "budget": ledger.snapshot()}`; `ok` requires every check True AND (for provider/full tiers) `reconciled` True. `main(argv=None) -> int` prints `json.dumps(report, indent=2, sort_keys=True)` and returns 0/1; `--output` additionally writes the same JSON under the given path (default parent `.personal-acceptance/`, gitignored).
- Provider tier is OPT-IN by flags: it runs only when `--receipt` is provided (the reconciliation gate is mandatory for shadow-touching runs, so no receipt = deterministic only, even with `--tier provider` → exit 2 with a one-line stderr message).

- [ ] **Step 1: Write the failing tests** — all offline: (a) `build_report` schema pin: exact top-level key set, `acceptance_class == "bounded_personal_local"`, scalar-only leaves (reuse live_verify's test helper idea: walk the dict, assert every leaf is bool/int/float — copy `_assert_output_scalars_are_safe` semantics from tests/deployment/mem0_0_arch/test_live_verify.py:129-134); (b) the rendered report never contains the strings "STALE", "LoCoMo", "LongMemEval", "BEAM", "Memora" (naming firewall) nor any synthetic fixture body; (c) `run_deterministic` with a stubbed `runner` recording commands: asserts ONE pytest invocation containing every registered node ID, jest invoked with the registry pattern, `--skip-jest` honored; (d) `main(["--tier", "provider"])` without `--receipt` exits 2; (e) `reconcile` returning False forces `ok` False even when all checks pass (stub `reconcile_shadow` via monkeypatch to raise `BackupVerificationError`); (f) provider tier with a fake opener + tmp receipt path + monkeypatched `reconcile_shadow` returning the golden dict → `ok` True and `checks["reconciled"]` True.
- [ ] **Step 2: Run, verify FAIL** — then implement `runner.py` (stdlib only: argparse, json, pathlib, subprocess, time).
- [ ] **Step 3: Run, verify PASS** — `uvx hatch run dev_py_3_12:test -q -k test_runner`.
- [ ] **Step 4: Full deterministic tier smoke, for real:** `uvx hatch run dev_py_3_12:python -m personal_acceptance.runner --tier deterministic --skip-jest` → prints a report with `ok: true` (jest skipped in this smoke; the endgame task runs it). Paste the report in your report file.
- [ ] **Step 5: Commit** — `git add personal_acceptance/runner.py tests/personal_acceptance/test_runner.py .gitignore && git commit -m "feat(acceptance): add tiered runner with content-free report and reconciliation gate"` (sweep gate first).

### Task 9: Documentation

**Files:**
- Create: `personal_acceptance/README.md`
- Modify: `evaluation_overlay/README.md` (the "Acceptance classes" section only — update class 2's sentence "implementation is a separate follow-up plan" to point at `personal_acceptance/`)

- [ ] **Step 1:** Write `personal_acceptance/README.md`: what the suite is (bounded personal local acceptance; the design's budgets verbatim); what it is NOT (never official benchmark acceptance; the firewall sentence); the two tiers and the 21-family roster table with per-family planned spend; run commands (`python -m personal_acceptance.runner --tier deterministic`, `--tier full --receipt <receipt> [--api-url http://127.0.0.1:28889]`); cadence (full run once per completed implementation workstream and once before any completion claim; per-change verification uses the deterministic tier only); report schema and the scalar-only/content-free rules; the reconciliation requirement for shadow-touching runs; troubleshooting (0600 key file, `MEM0_API_KEY` must be unset, loopback only).
- [ ] **Step 2:** Update the overlay README's class-2 paragraph: replace "the implementation is a separate follow-up plan" with "implemented in `personal_acceptance/` (see its README)". Nothing else in the file changes.
- [ ] **Step 3: Commit** — `git add personal_acceptance/README.md evaluation_overlay/README.md && git commit -m "docs(acceptance): document the bounded personal local acceptance suite"`.

### Task 10: Full verification and closure

No file changes except the handover. Steps in order, raw output pasted in the task report:

- [ ] **Step 1:** Full deterministic tier INCLUDING jest: `uvx hatch run dev_py_3_12:python -m personal_acceptance.runner --tier deterministic` (node_modules exists in mem0-ts). Expected `ok: true`.
- [ ] **Step 2:** Full Python suite with the Redis fixture (podman run/rm exactly as in the previous plan's Task 20): expected 2,143+N passed with only the known pre-existing JWT_SECRET order-coupling failures; N = the new personal-acceptance tests. Classify anything else against the plan-base commit via a disposable worktree before concluding.
- [ ] **Step 3:** Full TS jest + build unchanged from baseline (1,461/1,503 + the registry's pattern already ran in Step 1).
- [ ] **Step 4:** PROVIDER TIER — DO NOT RUN in this plan's execution. It requires the live shadow endpoint and spends real local-model minutes; running it is a separately approved operational step (record the exact command in the report: `uvx hatch run dev_py_3_12:python -m personal_acceptance.runner --tier full --receipt /home/will/.local/share/mem0_0-arch/backups/mem0_0-migration-20260805T182614Z.receipt.json`). The offline opener tests are this plan's evidence for the provider tier.
- [ ] **Step 5:** Fresh verifier subagent (cold context): re-run Steps 1-3 plus `-k "personal_acceptance"`; evidence only.
- [ ] **Step 6:** Final whole-branch review (most capable model) over this plan's commit range, pointed at the ledger's deferred minors; ONE fix wave maximum, one scoped re-review.
- [ ] **Step 7:** Update `docs/designs/mem0-0-state-aware-handover.md` with a "Personal acceptance suite checkpoint" (date/purpose, HEAD, unchanged shadow candidate, evidence summaries, the not-yet-run provider-tier command as the next operational step, remaining gates) and commit: `docs: record personal acceptance suite checkpoint`.
- [ ] **Step 8:** Report final HEAD, clean/dirty status, commits, evidence, and the single next recommended bounded task (run the provider tier against the shadow, on explicit approval).

---

## Self-review notes (spec coverage)

- Design budgets → Task 1 (ledger defaults 24/120/48/6000 s) + Task 2 (arithmetic pins over the locked roster; only the five allowed families spend `infer=true`).
- Scenario coverage list → Tasks 3-6 map every design bullet: corrections/supersession, current-vs-historical, dispute/corroboration/reversion (pinning discipline), the five kinds (one family), malformed extraction + atomicity, Python/REST/TS/MCP scope isolation, direct-vs-lifecycle, exact legacy ordering (existing pins referenced, not duplicated), Unicode/Japanese fallback, context-size enforcement. OMP families are explicitly absent (pending installation, per the design).
- Isolation + reconciliation → Task 7 (unique five-field scope + inherited exact cleanup + SQLite drain) and Task 8 (`reconcile` mandatory for shadow-touching runs; no receipt → provider tier refuses).
- Content-free + naming firewall → report schema pins in Task 8; scalar-only walker; the provider flows never place bodies in checks/counts.
- Cadence + "never presented as official" → Task 9 README; `acceptance_class` field.
- "No candidate-only regression in the full supported suites" → Task 10 Steps 2-3.
- The provider tier's first REAL run is deliberately out of scope (Task 10 Step 4) — it is an operational action against the live shadow requiring separate approval, exactly like the design's separation of tested code from running state.
