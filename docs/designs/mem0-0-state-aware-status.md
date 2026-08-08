# mem0_0 State-Aware Work Status and Task Ledger

Last updated: 2026-08-08 JST

This is the canonical living control point for the state-aware lifecycle work.
Update it whenever work starts, a gate changes state, evidence is produced, a
blocker is found, or a session ends. The detailed history remains in
`docs/designs/mem0-0-state-aware-handover.md`; the executable forward plan is
`docs/superpowers/plans/2026-08-08-state-aware-operational-completion.md`.

## Current state

- **Mode:** `PAUSED_SAFE`
- **Next task:** `SA-02` — re-attest the branch, draft PR, shadow readiness,
  image labels, and migration-receipt reconciliation without changing runtime
  state.
- **Branch:** `feat/state-aware-memory-python`
- **Last published implementation and acceptance checkpoint:**
  `b86389bc53ed72cba5c9161813f5233af8bd1ff9`
- **Last code-changing commit:**
  `7c8d60dc268763e0c2c4e605db763f2df67ed9ec`
- **Draft PR:** <https://github.com/willbohn/mem0_0/pull/1>
- **PR target:** `willbohn/mem0_0:main`
- **Running shadow:** healthy at the last content-free check, but still built
  from the older candidate `75021d0a616e7939eb58086f7a46c9dd86433a6b`.
- **Current-runtime evidence:** not refreshed during this documentation-only
  session; `SA-02` deliberately owns that read-only re-attestation.
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
| SA-02 | NEXT | Re-attest Git/PR state and run content-free shadow readiness, image-label, and receipt-reconciliation checks. | Resume in this checkout; no runtime mutation. | Record exact branch/HEAD/remote SHA, PR state, readiness result, image revision/digest, reconciliation status, and counts/digests in the activity ledger. |
| SA-03 | BLOCKED_APPROVAL | Refresh only the isolated shadow to the tested bounded-write target and attest its image/source. | SA-02 passes; explicit approval for shadow mutation; the pushed target's runtime-affecting tree matches verified code head `7c8d60dc`. | Shadow healthy; labels and source attestation match the exact approved branch head; legacy unchanged; content-free receipt still reconciles. |
| SA-04 | BLOCKED_APPROVAL | Run the five-family live provider tier against the canonical shadow. | SA-03 passes; local provider ready; explicit approval for synthetic writes and local-model time. | Content-free report has `acceptance_class=bounded_personal_local`, `tier=provider`, `ok=true`, all provider checks true, budgets within limits, and `checks.reconciled=true`. |
| SA-05 | READY | Preserve and review the provider-tier report and reconciliation evidence; clean only exact synthetic artifacts. | SA-04 attempted, whether pass or fail. | Scalar-only report saved outside Git; exact cleanup succeeds; reconciliation passes; ledger records result without memory bodies or scope values. |
| SA-06 | DEFERRED | Resolve the pre-existing server auth/params test coupling in a separate bounded change. | Separate design/approval because CI workflow edits are currently prohibited. | Regression test fails on pinned baseline, fix passes full affected suites, and no unrelated lifecycle change enters PR #1. |
| SA-07 | BLOCKED_DECISION | Approve an official benchmark compute, cost, time, and parallelism plan. | SA-04 and SA-05 pass. | Written decision covers STALE, Memora/FAMA, LoCoMo, LongMemEval, BEAM, identical arms, provider budget, estimated duration/cost, stop conditions, and unchanged acceptance thresholds. |
| SA-08 | BLOCKED_DEPENDENCY | Execute and evaluate the official benchmark matrix. | SA-07 approved; pinned datasets/models/runtimes ready. | Reproducible manifests and content-free summaries satisfy every gate; no benchmark claim is made before this row is `DONE`. |
| SA-09 | BLOCKED_DEPENDENCY | Install and behaviorally validate the OMP integration in a dedicated profile. | OMP installed through its owning system workflow; separate approval to write under `~/.omp`. | Exact six-tool discovery, scoped CRUD, search-before-answer, durable-write, compaction/restart persistence, exact cleanup, and reconciliation pass. A transport handshake alone is insufficient. |
| SA-10 | BLOCKED_DEPENDENCY | Re-run complete supported suites and obtain fresh verifier and critic decisions on the final merge candidate. | SA-04/05, SA-08, and every merge-scope prerequisite complete; final candidate SHA frozen. | Full Python/server/deployment/TypeScript evidence, baseline differential, verifier confirmation, critic approval, clean worktree, and exact pushed SHA. |
| SA-11 | BLOCKED_DECISION | Decide whether PR #1 becomes ready for review and whether to merge it into fork `main`. | SA-10 done; known server-test disposition recorded. | Explicit decision, final PR body/checklist accurate, required reviews/checks green. |
| SA-12 | BLOCKED_APPROVAL | Perform production cutover, harness retargeting, rollback drill, and legacy-unit disablement while retaining rollback state. | SA-11 merged plus every cutover gate emitted by `deployment.mem0_0_arch.cutover` satisfied. | Immutable cutover record, quiesced encrypted snapshot, exact reconciliation, atomic backed-up harness changes, successful rollback drill, and explicit cutover approval. |

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
