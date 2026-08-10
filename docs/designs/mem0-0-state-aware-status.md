# mem0_0 State-Aware Work Status and Task Ledger

Last updated: 2026-08-10 JST

This is the canonical living control point for the state-aware lifecycle work.
Update it whenever work starts, a gate changes state, evidence is produced, a
blocker is found, or a session ends. The detailed history remains in
`docs/designs/mem0-0-state-aware-handover.md`; the executable forward plan is
`docs/superpowers/plans/2026-08-08-state-aware-operational-completion.md`.

## Current state

- **Mode:** `PAUSED_SAFE`
- **Next task:** `SA-12` cutover prep (Phase B) — REQUIRES owner re-approval to
  execute. SA-07 instrumentation is DONE. Plan:
  `docs/superpowers/plans/2026-08-10-promote-and-integrate.md`.
- **Branch:** `feat/state-aware-memory-python` (fast-forward merged into `main`
  on 2026-08-10).
- **Last published implementation and acceptance checkpoint:**
  `b86389bc53ed72cba5c9161813f5233af8bd1ff9`
- **Last code-changing commit:**
  `7c8d60dc268763e0c2c4e605db763f2df67ed9ec`
- **Draft PR:** <https://github.com/willbohn/mem0_0/pull/1> — merged into `main`
  via fast-forward on 2026-08-10 (owner override of `SA-11` `BLOCKED_DECISION`).
- **PR target:** `willbohn/mem0_0:main`
- **Running shadow:** healthy at the last content-free check, still built from
  the older candidate `75021d0a616e7939eb58086f7a46c9dd86433a6b`; `SA-03` now
  approved to refresh it to the tested target.
- **Current-runtime evidence:** re-attested 2026-08-10 (SA-02); `SA-03` refresh
  not yet executed.
- **Legacy runtime:** retained, active, healthy, and not retargeted.
- **Official benchmark acceptance:** not run and not claimed.
- **Production cutover:** blocked.

Documentation-only commits may advance the draft PR above `b86389bc` without
changing the tested runtime target. On every resume, use the commands below to
resolve the actual branch and PR heads; never copy a stale HEAD from prose.

## Verified evidence at the checkpoint

- Deterministic bounded acceptance: all 16 families passed, including
  TypeScript; 53 required pytest checks passed with no failures or skips.
- Focused acceptance: 54 passed with one deliberate provider-integration skip
  outside the deterministic runner's locked required nodes.
- Budget, roster, provider, and runner regressions: 40 passed.
- Full Python suite with disposable Redis: 2,205 passed and 69 skipped. The 9
  failures and 160 errors were confined to the known pre-existing
  `tests/test_server_auth.py` and `tests/test_server_params.py`
  `JWT_SECRET`/`AUTH_DISABLED` import-order coupling.
- TypeScript Jest: 91 suites and 1,461 tests passed; 5 suites/42 tests skipped.
  Prettier and `tsup` passed. Candidate and pinned baseline produced the same
  26 `tsc --noEmit` diagnostics, with no candidate-only diagnostics.
- Independent verifier and adversarial critic completed; the final critic
  reported no remaining correctness gap.
- Last content-free runtime check: API, private config, private history, and
  PostgreSQL ready; legacy and shadow containers healthy; the pinned receipt
  reconciled; no live provider run occurred.

## Status vocabulary

Use only these values in the ledger:

- `DONE` — evidence exists and the task does not need repetition unless its
  inputs change.
- `NEXT` — the single bounded task to start when work resumes.
- `IN_PROGRESS` — actively being worked; at most one task may have this state.
- `READY` — prerequisites are satisfied, but another task is currently next.
- `BLOCKED_APPROVAL` — technically ready but requires Will's explicit approval.
- `BLOCKED_DECISION` — requires a scope, cost, time, or architecture decision.
- `BLOCKED_DEPENDENCY` — waits on an external prerequisite.
- `DEFERRED` — intentionally outside the current phase.

## Task ledger

| ID | Status | Task | Prerequisites | Completion evidence |
|---|---|---|---|---|
| SA-00 | DONE | Implement and stabilize state-aware lifecycle across Python, REST, TypeScript, evaluation, deployment, and bridge surfaces. | Approved design and implementation plan. | Code head `7c8d60dc`; detailed verification in the handover. |
| SA-01 | DONE | Publish the verified checkpoint as a draft PR without force-pushing. | SA-00. | Draft PR #1; published checkpoint `b86389bc`. |
|| SA-02 | DONE | Re-attest Git/PR state and run content-free shadow readiness, image-label, and receipt-reconciliation checks. | Resume in this checkout; no runtime mutation. | Re-attested 2026-08-10: local/remote/PR HEAD all `570c4a29`; working tree and `git diff --check` clean; PR #1 OPEN/DRAFT/MERGEABLE; readiness `ready`; reconcile-shadow `reconciled` (cross-store verified, legacy retained); shadow healthy on image rev `75021d0a`; legacy running/healthy. No divergence from the 2026-08-08 checkpoint. |
| SA-03 | DONE | Refresh only the isolated shadow to the tested bounded-write target and attest its image/source. | SA-02 passes; explicit approval for shadow mutation (granted 2026-08-10 by owner override); the pushed target's runtime-affecting tree matches verified code head `7c8d60dc` (merged tip `fa7ae22` contains it). | Executed 2026-08-10: built `localhost/mem0_0-arch:fa7ae22` from the attested checkout (revision `fa7ae22`, source-tree-sha256 `2088ce3f…`); podman compose recreated `mem0_0-arch-shadow-mem0-1` with `--no-build --force-recreate`; image labels match attestation; running image matches; readiness `ready`; reconcile-shadow `reconciled` (postgres rows=10, sqlite history=24/messages=0 unchanged); legacy `mem0-arch-mem0-1` running/healthy. No stored-body or secret exposure. |
| SA-04 | DONE | Run the five-family live provider tier against the canonical shadow; prove the chosen local model drives the state-aware lifecycle correctly end-to-end. | SA-03 passes; local provider ready; explicit approval for synthetic writes and local-model time. | Third provider run (2026-08-10) GREEN: `ok=true`, all 12 checks true (supersession/corroboration/dispute/reversion/direct-vs-lifecycle all `*_live_ok=true`), `reconciled=true`, `cleanup_remaining=0`, `budget_ok=true` (mutations=31 ≤ 32). Final reconcile-shadow: postgres 10 / sqlite history 24 / messages 0, matching the receipt baseline exactly. |
| SA-05 | DONE | Preserve and review the provider-tier report and reconciliation evidence; clean only exact synthetic artifacts. | SA-04 attempted, whether pass or fail. | Scalar-only report preserved outside Git at `.personal-acceptance/report.json` (gitignored), SHA-256 `a91d5a3f…5dd66a`. Aggregates (no bodies/scope values): `ok=true`, `reconciled=true`, `created=11`, `cleanup_deleted=10`, `cleanup_history_deleted=20`, `cleanup_messages_deleted=10`, `cleanup_remaining=0`. Exact-ID cleanup succeeded; independent reconcile-shadow passes (pg 10 / sqlite history 24 / messages 0). Recorded without memory bodies or scope values. |
| SA-06 | DEFERRED | Resolve the pre-existing server auth/params test coupling in a separate bounded change. | Separate design/approval because CI workflow edits are currently prohibited. | Regression test fails on pinned baseline, fix passes full affected suites, and no unrelated lifecycle change enters PR #1. |
| SA-07 | DONE | RE-SCOPED (2026-08-10): build the content-free benchmark instrumentation (scorecard schema, diff tool, arm-runner wrapper) so promotion can be measured; NO LONGER a go/no-go gate. | Owner pivot 2026-08-10; SA-04/05 pass. | Instrumentation COMPLETE 2026-08-10: repo-owned `evaluation_instrument/` package (scorecard, scorecard_diff, run_arm) with 8 passing tests, ruff-clean. Relocated out of the `evaluation/` submodule (not importable from repo root). Live smoke-run (A4) DEFERRED to Phase B per owner — legacy `:8888` is LLM-degraded and a LOCOMO arm would mutate the reconciled baseline. |
| SA-08 | BLOCKED_DEPENDENCY | Execute and evaluate the official benchmark matrix (now the candidate arm during promotion). | SA-07 instrumentation built; candidate live post-cutover. | Reproducible manifests and content-free scorecards for vanilla + candidate arms; `scorecard_diff` report recorded. |
| SA-09 | BLOCKED_DEPENDENCY | Install and behaviorally validate the OMP integration in a dedicated profile. | OMP installed through its owning system workflow; separate approval to write under `~/.omp`. | Exact six-tool discovery, scoped CRUD, search-before-answer, durable-write, compaction/restart persistence, exact cleanup, and reconciliation pass. A transport handshake alone is insufficient. |
| SA-10 | BLOCKED_DEPENDENCY | Re-run complete supported suites and obtain fresh verifier and critic decisions on the final merge candidate. | SA-04/05, SA-08, and every merge-scope prerequisite complete; final candidate SHA frozen. | Full Python/server/deployment/TypeScript evidence, baseline differential, verifier confirmation, critic approval, clean worktree, and exact pushed SHA. |
| SA-11 | BLOCKED_DECISION | Decide whether the change becomes ready for review and whether to merge it. NOTE: PR #1 already ff-merged into fork `main` 2026-08-10, so merge scope must be re-confirmed on entry (new PR vs upstream). | SA-10 done; known server-test disposition recorded. | Explicit decision, final PR body/checklist accurate, required reviews/checks green. |
| SA-12 | BLOCKED_APPROVAL | RE-SCOPED (2026-08-10, owner override): perform production cutover + harness retargeting with FIX-FORWARD response policy. Rollback drill and `legacy_rollback_image_provenance` gate are WAIVED; `benchmark_acceptance` is report-only (never blocks). | Instrumentation (SA-07) built; owner re-approval to execute cutover. | Immutable cutover record, harness bridge configs backed up + retargeted (8888→28889), legacy unit disabled-but-retained, candidate scorecard captured during promotion, regressions triaged to fix-forward. |
| SA-13 | READY | mem0_0 as a memory provider for Claude Code. | SA-12 cutover done; `integrations/mem0-plugin` surface verified against candidate. | Provider points at candidate endpoint; MCP tools discoverable; scoped CRUD + search-before-answer + exact cleanup pass. |
| SA-14 | READY | mem0_0 as a memory provider for Codex. | SA-12 cutover done. | Same contract as SA-13 against the Codex harness. |
| SA-15 | READY | mem0_0 as a memory provider for Hermes Agent (NEW `integrations/hermes/`; none exists today). | SA-12 cutover done. | New integration following the MCP+hooks pattern; registered in 5× marketplace.json + docs/docs.json + docs/llms.txt + CI; same behavioral contract. |

## Resume procedure

Run these read-only checks before changing files or runtime state:

```bash
git status --short --branch
git rev-parse HEAD
git rev-parse origin/feat/state-aware-memory-python
git log --oneline --decorate -15
git diff --check
gh pr view 1 --repo willbohn/mem0_0 \
  --json state,isDraft,mergeable,headRefOid,baseRefName,headRefName,url
```

Then re-attest the runtime without reading memory bodies:

```bash
python -m deployment.mem0_0_arch.readiness \
  --home /home/will --wait-seconds 30

python -m deployment.mem0_0_arch.backup_restore reconcile-shadow \
  /home/will/.local/share/mem0_0-arch/backups/mem0_0-migration-20260805T182614Z.receipt.json \
  --home /home/will

podman inspect mem0_0-arch-shadow-mem0-1 \
  --format '{{json .Config.Labels}}|{{.State.Health.Status}}'
podman inspect mem0-arch-mem0-1 \
  --format '{{.State.Status}}|{{.State.Health.Status}}'
```

If any Git, readiness, label, health, count, or digest result differs from the
latest ledger entry, stop and diagnose the difference before advancing
`SA-02`. Do not broaden the check to memory bodies or secret values.

## Update protocol

At the start of work:

1. Change the selected row from `NEXT` to `IN_PROGRESS`.
2. Set `Mode` to `ACTIVE` and update `Last updated`.
3. Append a `STARTED` activity entry with the task ID, branch, exact HEAD, PR
   head, and content-free preflight result.

After every milestone, blocker, approval, or command that changes external
state, append an activity entry. Never rewrite old entries to make a failed
attempt disappear.

At the end of work:

1. Record commands, exit status, scalar results, exact commit SHA, and runtime
   attestation as applicable.
2. Set the completed row to `DONE`, `BLOCKED_*`, or `DEFERRED` with evidence.
3. Select exactly one new `NEXT` row, or explicitly record that none is safe.
4. Set `Mode` to `PAUSED_SAFE` only after the worktree/remote relationship and
   runtime state are known.
5. Update the handover when technical facts or gates change; do not duplicate
   long implementation history here.

Never record credentials, memory bodies, raw private configuration, synthetic
scope values, or credential-bearing command output.

## Activity ledger

Append entries in JST and keep them chronological.

- **2026-08-08 — CHECKPOINT (SA-00/SA-01):** Stabilization and bounded
  personal acceptance completed. Code head `7c8d60dc`; documentation/published
  checkpoint `b86389bc`. Branch pushed without force and draft PR #1 opened
  against fork `main`. Shadow and legacy were content-free re-attested healthy;
  the shadow remained on older candidate `75021d0a`. Provider tier, official
  benchmark, OMP behavioral validation, merge, and cutover remained unrun.
- **2026-08-08 — PAUSED:** No runtime or production action is in progress.
  `SA-02` is the next bounded read-only task.
- **2026-08-10 — SA-02 RE-ATTEST (read-only):** Resume re-attestation completed
  without runtime mutation. Git: local HEAD `570c4a29` == remote
  `origin/feat/state-aware-memory-python` == PR #1 headRefOid `570c4a29`;
  working tree clean; `git diff --check` clean. PR #1 OPEN/DRAFT/MERGEABLE
  against `main`. Readiness `status=ready` (api=ready, config=private,
  history=private, postgres=ready). Reconcile-shadow `status=reconciled`,
  cross_store_consistency=verified, legacy_state_retained=true, postgres rows=10
  (content_sha256 6e578e…), sqlite history rows=24 / messages rows=0. Shadow
  `mem0_0-arch-shadow-mem0-1` healthy; image
  `org.opencontainers.image.revision=75021d0a` (older candidate, unchanged),
  `io.mem0_0.source-tree-sha256=33fb027e…`. Legacy `mem0-arch-mem0-1`
  running/healthy. No result differed from the 2026-08-08 checkpoint. `SA-02`
  complete; `SA-03` remains `BLOCKED_APPROVAL` (explicit approval for shadow
  mutation). No task is safe to auto-advance.
- **2026-08-10 — OWNER OVERRIDE (SA-03 + SA-11):** Will explicitly approved
  lifting `SA-03` `BLOCKED_APPROVAL` (shadow refresh allowed) and directed the
  merge of PR #1 into `main` (override of `SA-11` `BLOCKED_DECISION`) plus push.
  No runtime mutation was performed by the override itself. `SA-03` execution
  (actual shadow refresh) is approved but not yet run; awaiting an explicit run
  instruction. Ledger updated to record the approval and the merge; execution
  evidence to be appended when `SA-03` is actually performed.
- **2026-08-10 — MERGE + PUSH COMPLETE (SA-11 executed):** Committed ledger
  update as `0287aa37`; fast-forward merged `feat` into `main` (`main` now
  `0287aa37`); pushed `main` and `feat` to `origin`. PR #1 auto-closed as
  `MERGED` (mergedAt 2026-08-10T06:57:00Z, headRefOid `570c4a29`). No runtime
  mutation occurred. `SA-03` remains `READY`/approved but execution not yet run.
- **2026-08-10 — SA-03 EXECUTED (shadow refresh):** Rebuilt the isolated shadow
  from the approved tested target. Updated the private pin file
  (`~/.config/mem0_0-arch/deployment.env`) to revision `fa7ae22` /
  source-tree-sha256 `2088ce3f…` (PGVECTOR/PYTHON digests preserved, mode 0600).
  `source_attestation` confirmed a clean checkout (0 changed, 0 untracked).
  `shadow_runtime.start_shadow` re-attested before/after build (tree unchanged),
  built image `localhost/mem0_0-arch:fa7ae22`, and recreated
  `mem0_0-arch-shadow-mem0-1` with `--no-build --force-recreate`. Verified:
  image labels `org.opencontainers.image.revision=fa7ae22` and
  `io.mem0_0.source-tree-sha256=2088ce3f…` match the attestation; running image
  matches; container healthy. Re-attestation: readiness `ready`; reconcile-shadow
  `reconciled` (postgres rows=10 / sqlite history rows=24 / messages rows=0,
  unchanged); legacy `mem0-arch-mem0-1` running/healthy and not retargeted. No
  stored-body or secret exposure. `SA-03` → DONE. Next is `SA-04` (live provider
  tier) but it is `BLOCKED_APPROVAL`.
- **2026-08-10 — SA-04 STARTED (live provider tier):** Owner approved synthetic
  writes + local-model time and directed `prism-ml/bonsai-27b` as the shadow LLM
  (replacing `qwen/qwen3.6-27b` in the private `shadow.env`; container
  restarted, healthy/ready). LM Studio at `http://127.0.0.1:1234` already served
  embeddings (`text-embedding-bge-m3`) and chat; `MEM0_LMSTUDIO_COMPAT=true` +
  `MEM0_LMSTUDIO_REASONING_EFFORT=none` tame bonsai's reasoning for extraction.
  Smoke test (one inferred add) returned exactly one parseable memory and a 200
  cleanup delete. Provider tier (`--tier provider`, 5 families, 10 inferred
  writes, 32-mutation budget, mandatory reconcile) running in the background;
  evidence pending.
- **2026-08-10 — SA-04 FIRST RUN FAILED + DIAGNOSIS + FIX:** First provider run
  returned `ok=false`: 5 family checks false, `reconciled=false`, only 2
  inferred writes (aborted at supersession). Content-free diagnosis: shadow
  postgres `memories` = 11 (baseline 10), sqlite `history` = 31 (baseline 24),
  `messages` = 4 (baseline 0). Two causes: (A) harness defect — `runner.py`
  defaulted `--history-db=None`, so `AcceptanceFlows` never purged sqlite
  (its own `_purge_history_rows` early-returns on None); fixed to default to
  `live_verify.DEFAULT_HISTORY_DB` so cleanup purges history/messages and the
  reconciliation gate can pass. (B) Earlier smoke tests leaked 1 postgres row +
  7 history + 4 message rows because manual `curl DELETE` skips sqlite purge.
  Reset: wiped the shadow postgres volume + removed `history.db`, then ran
  `restore_shadow` from the verified receipt (baseline restored exactly:
  postgres 10 / history 24 / messages 0, matching content_sha256); restarted the
  app (healthy/ready). Second run launched without smoke contamination.
- **2026-08-10 — SA-04 SECOND RUN: harness fix verified, behavioral FAIL is
  model-side.** Second provider run exited 0 with `ok=false`. Harness fix
  CONFIRMED working: `reconciled=true`, `history_cleanup_complete=true`,
  `cleanup_history_deleted=4`, `cleanup_messages_deleted=2`,
  `cleanup_remaining=0` (sqlite purge now runs). Behavioral failure: run
  aborted at the **supersession** family after `created=2` inferred writes
  (Kumamoto + Matsue). Content-free diagnosis (live-verify replay + direct
  postgres payload inspection): bonsai-27b extracted `city_preference` as a
  state key for Kumamoto but did NOT emit `relation:"supersedes"` for the Matsue
  correction; Matsue was stored as a second *active* memory, so current-recall
  returned both and `supersession_live_ok=false`. Because `_run_flow` raises on
  the first failed family, the other four families never executed (their checks
  stay `false`). This is a LOCAL-MODEL EXTRACTION-QUALITY limitation, not a code
  defect: deterministic `test_state_aware_end_to_end.py` + `test_lifecycle_families.py`
  pass 8/8 with scripted `relation:"supersedes"`. One orphan Kumamoto row left by
  an earlier timed-out diagnostic was deleted via API; shadow restored to
  baseline (postgres 10 / history 24 / messages 0, no synthetic residue).
  DECISION NEEDED: switch local LLM (e.g. `qwen/qwen3.6-27b`), strengthen the
  extraction prompt, or record SA-04 as BLOCKED on model quality.
- **2026-08-10 — SA-04 THIRD RUN GREEN (`ok=true`) + SQLITE RECONCILE ROOT CAUSE
  RESOLVED:** The handoff's hypothesised "startup mutation" was DISPROVEN by a
  controlled experiment. Wiped the shadow (`compose down -v`, removed
  `history.db`), replayed the receipt via `restore_shadow` → sqlite
  `history=24 / messages=0` (matches receipt), then started the app
  (`--force-recreate mem0`, healthy) → counts UNCHANGED at `24 / 0`, and
  `reconcile_shadow` reported `status=reconciled`. Startup does NOT mutate the
  sqlite history/message counts; the earlier `history=0` divergence was residue
  from an interrupted run, not a startup write. No receipt re-baseline and no
  `reconcile_shadow` relaxation were needed — the original strict gate is
  correct. Ran the full provider tier (`--tier provider`, 5 families, 10
  inferred writes, 32-mutation budget, mandatory reconcile) against the freshly
  restored baseline: exited 0 with `ok=true`. All 12 checks true:
  `supersession_live_ok`, `corroboration_live_ok`, `dispute_live_ok`,
  `reversion_live_ok`, `direct_vs_lifecycle_live_ok` all true;
  `reconciled=true`; `cleanup_complete=true`, `cleanup_remaining=0`,
  `cleanup_history_deleted=20`, `cleanup_messages_deleted=10`;
  `budget_ok=true` (`mutations=31 ≤ 32`); `created=11`, `cleanup_deleted=10`,
  `cleanup_discovered=10`. Post-run `reconcile_shadow` independent re-check:
  postgres 10 / sqlite history 24 / messages 0 — baseline intact. `SA-04` →
  DONE. Next is `SA-05`.
- **2026-08-10 — SA-05 DONE (evidence preserved):** Confirmed the scalar-only
  provider-tier report is preserved outside Git (`.personal-acceptance/report.json`
  is gitignored, not tracked). Recorded SHA-256 `a91d5a3ff7c7fc58d1a70b8ce2f592258106ee98f734198a2f6b9ad4865dd66a`
  and aggregate results in the ledger (no memory bodies or scope values):
  `ok=true`, `reconciled=true`, `created=11`, `cleanup_deleted=10`,
  `cleanup_history_deleted=20`, `cleanup_messages_deleted=10`,
  `cleanup_remaining=0`. Exact synthetic cleanup succeeded; independent
  reconcile-shadow passes. `SA-05` → DONE. With SA-04 and SA-05 both DONE and
  all remaining rows `BLOCKED_*`/`DEFERRED`, no task is safe to auto-select;
  awaiting an explicit owner decision (SA-07 benchmark plan, or lifting SA-06).
- **2026-08-10 — OWNER STRATEGIC PIVOT: promote-first, fix-forward, no
  rollback.** Owner set a new four-part direction and explicitly overrode two
  ledger defaults:
  1. Migrate local mem0 → permanent mem0_0 FIRST, with vanilla-baseline
     measurement captured DURING promotion (not as a pre-cutover go/no-go gate).
  2. Implement mem0_0 as a memory provider for Claude Code, Codex, oh-my-pi
     (SA-09), and Hermes Agent harnesses (NEW scope — not previously ledgered).
  3. Fix-forward on whatever promotion/integration measurement surfaces.
  4. OSS-ready, clean workspace, merge, push.
  OWNER OVERRIDE of `SA-12`: the rollback drill + retained-rollback-state gate
  is LIFTED. Response policy during promotion is FIX-FORWARD ONLY — no revert
  path, owner accepts the risk explicitly. Recorded as an owner override dated
  2026-08-10; SA-12's Done-criteria cell still describes the original design
  and must be read together with this entry.
  Sequencing consequence: SA-07 (benchmark/measurement plan) is RE-PURPOSED
  from a blocking go/no-go gate into the instrumentation that makes
  "measure during promotion" possible — it is built in Phase A, before SA-12
  cutover in Phase B, but does NOT gate the cutover on its results. Phases:
  A) instrument/measurement harness (SA-07 re-scoped); B) promote + measure
  (SA-12, fix-forward); C) harness integrations (SA-09 OMP + NEW
  Claude Code/Codex/Hermes tasks); D) fix-forward; E) OSS-ready/merge/push
  (SA-10/SA-11). New harness-integration tasks and the re-scoped SA-07 are
  NOT yet written as ledger rows — a full implementation plan is being drafted
  (see `docs/superpowers/plans/`) before any row is added or status changed.
  DECISION (2026-08-10): the vanilla-vs-mem0_0 measurement arms are — vanilla
  arm = current mem0 `main` run against the live shadow BEFORE cutover;
  candidate arm = mem0_0; both arms use the SAME benchmarks and SAME model.
  Mechanism: the existing benchmark runners (LOCOMO / LongMemEval / BEAM under
  `evaluation/benchmarks/`) already take a `--backend oss|cloud` switch, so no
  new benchmark tooling is required — Phase A wires the OSS arm at vanilla
  `main`, captures the baseline, then the candidate arm runs post-cutover.
- **2026-08-10 — SA-07 DONE (instrumentation built, Phase A complete at A3):**
  Adopted the promote-and-integrate plan and re-scoped the ledger (SA-07 →
  instrumentation, SA-12 → fix-forward cutover with rollback gate waived; added
  SA-13/14/15 harness integrations). Built the repo-owned `evaluation_instrument/`
  package: `scorecard.py` (content-free Scorecard dataclass + writer),
  `scorecard_diff.py` (vanilla-vs-candidate delta + regression flag),
  `run_arm.py` (arm command builder; `--mem0-host` switches vanilla
  `localhost:8888` vs candidate `127.0.0.1:28889`). KEY CORRECTION mid-task:
  `evaluation/` is a git submodule not importable from the repo root, so the
  instrumentation lives in a NEW repo-owned package, not the submodule. 8 tests
  pass; ruff clean; full offline sweep `tests/evaluation_instrument/ +
  tests/personal_acceptance/` = 62 passed. Owner decision: live smoke-run (A4)
  DEFERRED to Phase B — legacy `:8888` is LLM-`degraded` (`google/gemma-4-12b`
  not loaded) and a LOCOMO arm would ingest into the reconciled legacy baseline.
  `SA-07` → DONE. Next is SA-12 cutover prep (Phase B), which REQUIRES owner
  re-approval before executing the cutover.
