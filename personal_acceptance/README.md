# Bounded Personal Local Acceptance Suite

`personal_acceptance/` is a budget-enforced, content-free acceptance runner
for the state-aware mem0_0 fork. It gives **bounded local confidence** that
the fork behaves correctly across a locked set of scenario families — it is
not, and cannot become, a substitute for official benchmark acceptance.

## What this is

The suite enforces the hard budgets defined in the design
(`docs/superpowers/specs/2026-08-07-mem0-0-stabilization-and-omp-design.md:432-444`),
quoted verbatim:

> - no more than 24 scenario families;
> - no more than 120 total memory mutations, where every write, update, and
>   delete — including cleanup — counts against the budget;
> - no more than 48 provider-backed `infer=true` writes, reserved exclusively
>   for the supersession, corroboration, dispute, reversion, and
>   direct-write-versus-lifecycle families; every other family runs
>   deterministically with `infer=false` or a stubbed LLM and spends none of
>   this budget;
> - a wall-clock cap of 100 minutes on the provider-backed portion of a full
>   run, where exceeding the cap fails the run rather than extending it;
> - one local model configuration, with no paid provider requirement;
> - no retry beyond the existing fail-closed request contract; and
> - no personal memory bodies in reports or committed artifacts.

`BudgetLedger` (`personal_acceptance/budget.py:19-30`) enforces these as
hard defaults — `max_families=24`, `max_mutations=120`, `max_infer_writes=48`,
`provider_wall_clock_seconds=6000.0` (100 minutes) — and every check runs
**before** the action it guards, so an over-budget call raises
`BudgetExceeded` instead of completing.

## What this is NOT

This suite is **not** an official benchmark and must never be presented as,
or substituted for, official benchmark acceptance. The STALE, Memora/FAMA,
LoCoMo, LongMemEval, and BEAM benchmark gate described in
`evaluation_overlay/README.md` is the sole official acceptance gate for this
project, remains unrun and unclaimed for the current candidate, and is
unaffected by anything in this suite.

The naming firewall is enforced structurally, not just by convention: every
report's `acceptance_class` field is fixed to `"bounded_personal_local"`
(`personal_acceptance/runner.py:241`), and no report, log, or document
produced by this suite may name the benchmarks disclaimed above outside of
this section.

## Tiers and family roster

The roster is locked at 21 families — 16 deterministic + 5 provider-backed —
inside the 24-family budget, leaving 3 slots of deliberate headroom
(`personal_acceptance/families.py:22-92`). Deterministic families never
touch the shadow REST endpoint and spend no `infer=true` or mutation budget;
only the five provider families spend budget, and only they are allowed to
(`PROVIDER_ALLOWED`, `personal_acceptance/families.py:7-9`).

### Deterministic tier (16 families, `infer=false`)

| Family | Verified by |
|---|---|
| `supersession_det` | `tests/personal_acceptance/test_lifecycle_families.py::test_supersession_family` |
| `current_vs_historical` | `tests/personal_acceptance/test_lifecycle_families.py::test_current_vs_historical_family` |
| `dispute_det` | `tests/personal_acceptance/test_lifecycle_families.py::test_dispute_family` |
| `corroboration_det` | `tests/personal_acceptance/test_lifecycle_families.py::test_corroboration_family` |
| `reversion_det` | `tests/personal_acceptance/test_lifecycle_families.py::test_reversion_family` |
| `memory_kinds` | `tests/personal_acceptance/test_lifecycle_families.py::test_memory_kinds_family` |
| `malformed_extraction` | `tests/personal_acceptance/test_failure_families.py::test_malformed_extraction_family` |
| `persistence_atomicity` | `tests/personal_acceptance/test_failure_families.py::test_persistence_atomicity_family` |
| `scope_python` | `tests/personal_acceptance/test_scope_families.py::test_scope_python_family` |
| `scope_rest` | `tests/personal_acceptance/test_scope_families.py::test_scope_rest_family` |
| `scope_ts` | jest pattern `memory.add\|memory.crud\|memory.state-aware` (run from `mem0-ts/`) |
| `scope_mcp` | `tests/personal_acceptance/test_scope_families.py::test_scope_mcp_family` |
| `direct_vs_lifecycle_det` | `tests/personal_acceptance/test_lifecycle_families.py::test_direct_vs_lifecycle_family` |
| `legacy_ordering` | `tests/memory/test_state_aware_search.py`, `tests/utils/test_scoring.py` |
| `lexical_unicode_ja` | `tests/personal_acceptance/test_recall_families.py::test_lexical_unicode_ja_family` |
| `context_size` | `tests/personal_acceptance/test_recall_families.py::test_context_size_family` |

### Provider tier (5 families, `infer=true`, planned spend)

| Family | Planned `infer=true` writes | Planned mutations |
|---|---|---|
| `supersession_live` | 2 | 6 |
| `corroboration_live` | 2 | 6 |
| `dispute_live` | 2 | 6 |
| `reversion_live` | 3 | 9 |
| `direct_vs_lifecycle_live` | 1 | 5 |
| **Total** | **10 / 48** | **32 / 120** |

These totals are pinned by `test_planned_budgets_fit_the_ledger`
(`tests/personal_acceptance/test_families.py`), which asserts the sum of
`planned_infer_writes` and `planned_mutations` across `FAMILIES` fits inside
the ledger's `max_infer_writes` and `max_mutations` caps — wide headroom is
deliberate. Provider families run through `AcceptanceFlows`
(`personal_acceptance/provider_flows.py`), a `LiveVerifier` subclass that
drives each flow inside one disposable, uniquely-scoped identity and charges
every write and cleanup delete to the ledger.

The provider runner fails closed: it reserves the complete planned write and
cleanup budget before issuing any provider request, requires the shadow's
OpenAPI schema to advertise the positive `max_extractions` and `max_mutations`
contracts, sends `max_extractions=1` and `max_mutations=2` on every inferred
add, charges that conservative two-mutation ceiling before the request, counts
every returned memory, and requires each synthetic add to return exactly one
memory. It also checks the
provider clock before and after non-cleanup requests and again at the end, and
includes both `budget_ok` and `bounded_write_contract` in the report checks.
Cleanup still runs after a wall-clock overrun, and its reserved capacity keeps
it inside the mutation cap.

## Running the suite

Deterministic tier, via the project's hatch environment:

```bash
uvx hatch run dev_py_3_12:python -m personal_acceptance.runner --tier deterministic
```

Pass `--skip-jest` to skip the TypeScript `scope_ts` family (see
Troubleshooting below) — it is then excluded from `checks` entirely rather
than defaulted true or false, and counted in `counts.jest_skipped` instead.

Full tier (deterministic + provider, requires a reconciliation receipt):

```bash
uvx hatch run dev_py_3_12:python -m personal_acceptance.runner \
  --tier full --receipt <receipt>
```

`--tier provider` runs only the five provider families and also requires
`--receipt`. Other flags (`personal_acceptance/runner.py:251-260`):
`--api-url` (fixed to the canonical default `http://127.0.0.1:28889`;
alternate values are rejected), `--api-key-file` (default
`/home/will/.config/mem0_0-arch/bridge-api-key`), `--history-db`, `--home`
(default `$HOME`), `--output` (default `.personal-acceptance/report.json`,
gitignored).

**Both `--tier provider` and `--tier full` require `--receipt` or the run
exits 2** before doing any work — the reconciliation gate is mandatory for
any shadow-touching run (`personal_acceptance/runner.py:263-268`). The
process otherwise exits `0` when the report's `ok` is `true` and `1`
otherwise.

**The provider tier has not yet had a live run in this repository.**
Running it against the shadow deployment is a separately approved
operational step — first live provider-tier run pending separate
operational approval. The currently deployed shadow predates the bounded-write
contracts, so its OpenAPI preflight will fail safely before any write until a
separately approved shadow refresh deploys the tested code.

## Cadence

Per the design: a full-suite run (`--tier full`) is required once per
completed implementation workstream and once before any completion claim.
Per-change verification uses only the deterministic tier
(`--tier deterministic`, `infer=false`) — the provider tier is not part of
routine per-change verification.

## Report schema

`build_report` (`personal_acceptance/runner.py:180-248`) returns a report
whose top-level keys are **exactly** `schema_version`, `acceptance_class`,
`ok`, `tier`, `checks`, `counts`, `timings_ms`, `budget`:

- `schema_version` — `int`, currently `1`.
- `acceptance_class` — fixed string `"bounded_personal_local"`.
- `ok` — `bool`; `true` only when every entry in `checks` is `true`. For the
  provider/full tiers that set includes `reconciled`, so a reconciliation
  failure always fails the run.
- `tier` — the tier that ran (`"deterministic"`, `"provider"`, or `"full"`).
- `checks` — per-family `bool` pass/fail.
- `counts` — `int` counts (e.g. `pytest_passed`, `pytest_failed`,
  `pytest_skipped`, `jest_skipped`, `created`, `cleanup_deleted`). A skipped,
  xfailed, xpassed, or deselected required pytest node fails its family rather
  than being accepted as a pass.
- `timings_ms` — `float` subprocess/flow timings.
- `budget` — the ledger snapshot (`personal_acceptance/budget.py:82-88`):
  `families`, `mutations`, `infer_writes` (all `int`), and
  `provider_elapsed_seconds` (`float`).

For provider runs, `budget.mutations` is deliberately conservative: each
inferred request is charged its server-enforced ceiling of two primary-memory
mutations before dispatch, even if the request ultimately performs only one.
Raw writes and cleanup deletes are charged one-for-one.

Every leaf under `checks`, `counts`, `timings_ms`, and `budget` is a `bool`,
`int`, or `float` — the report never carries a memory body, ID, or scope
value.

## Reconciliation gate

Any run that touches the shadow deployment (provider or full tier) must end
with `reconcile_shadow` (`deployment/mem0_0_arch/backup_restore.py:488-517`),
which proves the canonical stores match one quiesced legacy receipt without
mutating either store. `reconcile()` (`personal_acceptance/runner.py:161-177`)
returns `true` only when the result's `status` is `"reconciled"`; a
`BackupVerificationError` or `MigrationConflictError` is caught and turned
into `false` rather than re-raised, so a shadow-touching run always finishes
with a report instead of crashing. `checks["reconciled"]` participates in
`ok` like every other check.

## Troubleshooting

- **Key file must be `0600` (or stricter).** `--api-key-file` must be a
  private regular file with no group/other permissions
  (`deployment/mem0_0_arch/live_verify.py:131-135`); anything looser raises
  `ConfigurationError: API key must be a private regular file`. Fix with
  `chmod 600 <key-file>`.
- **`MEM0_API_KEY` must be unset.** If it is set in the environment,
  construction fails immediately with `ConfigurationError: inline API keys
  are forbidden; use a private key file`
  (`deployment/mem0_0_arch/live_verify.py:94-95`). Unset it before running
  the provider or full tier.
- **Canonical shadow only.** `--api-url` must remain exactly
  `http://127.0.0.1:28889`. Other loopback endpoints are rejected so provider
  behavior and receipt reconciliation cannot accidentally describe different
  stores. The inherited verifier also rejects credentials, queries, and
  fragments (`deployment/mem0_0_arch/live_verify.py:101-111`).
- **Jest needs `mem0-ts/node_modules`.** The `scope_ts` family shells out to
  `pnpm run test -- <pattern>` with `cwd=mem0-ts`; if dependencies haven't
  been installed there (`pnpm install` in `mem0-ts/`), that subprocess
  fails. Either install them first or pass `--skip-jest` to exclude
  `scope_ts` from `checks` for that run.
