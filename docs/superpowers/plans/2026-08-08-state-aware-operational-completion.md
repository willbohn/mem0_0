# State-Aware Operational Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Advance the verified state-aware lifecycle from its safe draft checkpoint through isolated shadow acceptance, benchmark evidence, final review, and an explicitly approved production cutover without weakening safety or compatibility gates.

**Architecture:** Treat repository code, the isolated shadow, official benchmark infrastructure, installed harnesses, and production cutover as separate gated systems. Each task consumes content-free evidence from the prior task, mutates only its named scope, reconciles state afterward, and stops before the next approval boundary.

**Tech Stack:** Git/GitHub CLI, Python 3.12 with Hatch, TypeScript with pnpm/Jest/tsup, Podman/systemd user units, PostgreSQL/SQLite/Qdrant, local OpenAI-compatible model endpoints, repository-owned deployment and evaluation tooling.

## Global Constraints

- The canonical task state is `docs/designs/mem0-0-state-aware-status.md`; update it at task start, every gate transition, and task end.
- Preserve the `legacy` recall mode, five explicit scope fields, fail-closed persistence, content-free evidence, and non-destructive deployment model.
- Never print or persist credentials, memory bodies, private configuration values, synthetic scope values, or credential-bearing command output.
- Never change CI/CD workflows, publish packages, retarget installed harnesses, disable legacy, delete retained state, force-push, or cut over without the separately documented approval and gate.
- The bounded personal suite is not official benchmark acceptance.
- The last fully verified code head is `7c8d60dc268763e0c2c4e605db763f2df67ed9ec`. At shadow-refresh time, deploy the exact clean, pushed branch head only after proving every change above that code head is confined to the four documentation paths allowlisted in Task 2. Any other changed path requires complete re-verification before deployment.
- The canonical reconciliation receipt is `/home/will/.local/share/mem0_0-arch/backups/mem0_0-migration-20260805T182614Z.receipt.json` until a later quiesced verified receipt is explicitly adopted.

---

### Task 1: Re-attest the durable checkpoint

**Files:**
- Modify: `docs/designs/mem0-0-state-aware-status.md`
- Read: `AGENTS.md`
- Read: `docs/designs/mem0-0-state-aware-handover.md`

**Interfaces:**
- Consumes: the local branch, remote draft PR #1, canonical migration receipt, and both retained runtimes.
- Produces: one content-free `SA-02` activity entry and a go/no-go decision for shadow refresh.

- [ ] **Step 1: Mark `SA-02` in progress**

Change `SA-02` from `NEXT` to `IN_PROGRESS`, set the document mode to
`ACTIVE`, and append a `STARTED` entry containing the exact local and PR heads.

- [ ] **Step 2: Verify Git and draft PR identity**

Run:

```bash
git status --short --branch
git rev-parse HEAD
git rev-parse origin/feat/state-aware-memory-python
git diff --check
gh pr view 1 --repo willbohn/mem0_0 \
  --json state,isDraft,mergeable,headRefOid,baseRefName,headRefName,url
```

Expected: clean worktree before operational work; local, tracking, and PR heads
equal; PR open and draft; base `main`; head `feat/state-aware-memory-python`.

- [ ] **Step 3: Verify content-free readiness and reconciliation**

Run:

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

Expected: readiness succeeds for all four checks; reconciliation returns
`reconciled`; both containers are healthy; the shadow still reports its actual
deployed revision rather than being assumed current.

- [ ] **Step 4: Record the gate**

Append only content-free counts, digests, labels, health, commands, and exit
statuses. If any result differs from the last checkpoint, leave `SA-02`
`IN_PROGRESS`, add a `BLOCKED` activity entry, and diagnose before Task 2.
Otherwise set `SA-02` to `DONE`, `SA-03` to `BLOCKED_APPROVAL`, and the mode to
`PAUSED_SAFE` pending explicit shadow-refresh approval.

- [ ] **Step 5: Commit the ledger update**

```bash
git add docs/designs/mem0-0-state-aware-status.md
git commit -m "docs: re-attest state-aware operational checkpoint"
git push origin feat/state-aware-memory-python
```

### Task 2: Refresh only the isolated shadow

**Files:**
- Modify outside repository after explicit approval: `/home/will/.config/mem0_0-arch/deployment.env`
- Read: `deployment/mem0_0_arch/README.md`
- Read: `deployment/mem0_0_arch/systemd/mem0_0-arch.service`
- Modify: `docs/designs/mem0-0-state-aware-status.md`

**Interfaces:**
- Consumes: passing Task 1 evidence and explicit approval for shadow mutation.
- Produces: a healthy isolated shadow whose image labels and source attestation match the exact clean, pushed, documentation-only branch head approved in this task, with legacy unchanged.

- [ ] **Step 1: Freeze and attest the exact target**

Fetch and prove that the current pushed branch head contains no runtime-affecting
change above the fully verified code head:

```bash
git fetch origin feat/state-aware-memory-python
git status --short --branch
git rev-parse HEAD
git rev-parse origin/feat/state-aware-memory-python
git diff --name-only \
  7c8d60dc268763e0c2c4e605db763f2df67ed9ec..origin/feat/state-aware-memory-python \
  -- . \
  ':(exclude)docs/designs/mem0-0-state-aware-handover.md' \
  ':(exclude)docs/designs/mem0-0-state-aware-status.md' \
  ':(exclude)docs/superpowers/plans/2026-08-08-state-aware-operational-completion.md' \
  ':(exclude)personal_acceptance/README.md'
python -m deployment.mem0_0_arch.source_attestation \
  /home/will/Documents/CodingWorkspaces/mem0_0
```

Expected: clean worktree; local and remote heads equal; the excluded-path diff
prints nothing; attestation reports that exact head and a content-free source
digest. Record both. Do not proceed if any other path changed or the revision,
tracking ref, or tree state differs.

- [ ] **Step 2: Request the exact operational approval**

Present the target revision, source digest, current shadow revision, retained
legacy health, reconciliation status, expected interruption to the shadow, and
rollback path. Approval covers only rebuilding/restarting the isolated shadow;
it does not cover provider writes, production cutover, or harness changes.

- [ ] **Step 3: Update the private deployment pins**

After approval, set `MEM0_0_SOURCE_REVISION` in
`/home/will/.config/mem0_0-arch/deployment.env` to the exact target above and
set `MEM0_0_SOURCE_TREE_SHA256` to the attestation output from Step 1. Preserve
every other value byte-for-byte and mode `0600`. Do not copy the file contents
into logs, chat, the repository, or the activity ledger.

- [ ] **Step 4: Rebuild and restart through the repository-owned user unit**

The installed unit reads this exact checkout, so re-confirm that it is clean and
still equals the approved remote head immediately before restarting:

```bash
git status --short --branch
git rev-parse HEAD
git rev-parse origin/feat/state-aware-memory-python
systemctl --user restart mem0_0-arch.service
systemctl --user status --no-pager mem0_0-arch.service
```

Do not enable a new production unit and do not stop the retained legacy unit.
If the repository-owned pre-build or post-build attestation fails, stop and
restore only the prior shadow pins/image.

- [ ] **Step 5: Re-attest the refreshed shadow**

Run Task 1 Step 3 again. Expected: the shadow is healthy and its image labels
match the approved revision and digest; the legacy remains healthy; the receipt
still reconciles exactly.

- [ ] **Step 6: Record and commit the result**

Set `SA-03` to `DONE` only if every result passes. Otherwise set it to
`BLOCKED_DEPENDENCY`, record the content-free failure and rollback state, and do
not run provider acceptance.

```bash
git add docs/designs/mem0-0-state-aware-status.md
git commit -m "docs: record bounded shadow refresh evidence"
git push origin feat/state-aware-memory-python
```

### Task 3: Run bounded live provider acceptance

**Files:**
- Runtime output only: `.personal-acceptance/report.json` (gitignored)
- Modify: `docs/designs/mem0-0-state-aware-status.md`
- Read: `personal_acceptance/README.md`

**Interfaces:**
- Consumes: an attested Task 2 shadow, ready local providers, private file-only admin authentication, and explicit approval for synthetic writes/model time.
- Produces: a scalar-only provider report, exact synthetic cleanup, and mandatory receipt reconciliation.

- [ ] **Step 1: Verify provider-run prerequisites without writing**

Confirm readiness, exact shadow labels, receipt reconciliation, private key-file
mode, and that `MEM0_API_KEY` is unset. Confirm the shadow OpenAPI exposes the
positive `max_extractions` and `max_mutations` request contracts. If any check
fails, no provider request is allowed.

- [ ] **Step 2: Request provider-tier approval**

State that the run uses five provider families, reserves at most 32 mutations
for its bounded plan, uses the local model, may take up to 100 minutes, writes
only synthetic five-field scopes, performs exact-ID cleanup, and must reconcile
the canonical receipt.

- [ ] **Step 3: Run only the provider tier**

```bash
uvx hatch run dev_py_3_12:python -m personal_acceptance.runner \
  --tier provider \
  --receipt /home/will/.local/share/mem0_0-arch/backups/mem0_0-migration-20260805T182614Z.receipt.json
```

Expected: exit 0; `acceptance_class` equals `bounded_personal_local`; `tier`
equals `provider`; `ok=true`; every provider-family check and `reconciled` are
true; scalar budgets remain within the locked limits.

- [ ] **Step 4: Handle every exit path fail-closed**

Whether the run passes or fails, verify exact synthetic cleanup and run the
content-free receipt reconciliation independently. Do not rerun automatically,
increase budgets, change the endpoint, call `delete_all`, or weaken a required
family. Preserve the scalar report outside Git and record its SHA-256 plus
aggregate results in the ledger.

- [ ] **Step 5: Record and commit the result**

Set `SA-04` and `SA-05` to `DONE` only when the run, cleanup, and reconciliation
all pass. Otherwise record the failed gate and set the next task to diagnosis.

```bash
git add docs/designs/mem0-0-state-aware-status.md
git commit -m "docs: record live provider acceptance evidence"
git push origin feat/state-aware-memory-python
```

### Task 4: Decide the official benchmark execution envelope

**Files:**
- Create after the decision: `docs/designs/mem0-0-official-benchmark-decision.md`
- Modify: `docs/designs/mem0-0-state-aware-status.md`
- Read: `evaluation_overlay/README.md`
- Read: `evaluation_overlay/manifest.json`

**Interfaces:**
- Consumes: passing bounded provider evidence and the unchanged official acceptance thresholds.
- Produces: an approved or rejected compute/cost/time/parallelism decision; no benchmark runs occur in this task.

- [ ] **Step 1: Inventory immutable benchmark inputs**

Verify the manifest, pinned repositories, dataset receipts, model receipts,
Qdrant receipt, model endpoints, and all three isolated arms. Record only
versions, hashes, counts, ports, and readiness—not benchmark content.

- [ ] **Step 2: Produce the execution estimate**

Calculate expected writes, serial duration using measured local write latency,
safe parallelism, hardware/provider requirements, expected monetary cost,
checkpoint cadence, and stop conditions for STALE, Memora/FAMA, LoCoMo,
LongMemEval, and BEAM. Preserve the existing acceptance thresholds verbatim.

- [ ] **Step 3: Obtain Will's decision**

Record one of: approve the full matrix as designed; approve a specifically
resourced equivalent matrix; or keep the official gate deferred. A reduced
diagnostic may be approved separately but cannot satisfy or rename the official
gate.

- [ ] **Step 4: Commit the decision record**

```bash
git add docs/designs/mem0-0-official-benchmark-decision.md \
  docs/designs/mem0-0-state-aware-status.md
git commit -m "docs: record official benchmark execution decision"
git push origin feat/state-aware-memory-python
```

### Task 5: Execute and evaluate the official benchmark matrix

**Files:**
- Runtime state: `.evaluation-overlay/` (gitignored)
- Modify: `docs/designs/mem0-0-state-aware-status.md`
- Modify only if behavior changes: `evaluation_overlay/`

**Interfaces:**
- Consumes: an approved Task 4 decision and receipt-verified immutable inputs.
- Produces: reproducible, content-free summaries for baseline, candidate legacy, and candidate state-aware arms plus per-gate comparisons.

- [ ] **Step 1: Preflight every pinned input and endpoint**

Run the repository verification commands in `evaluation_overlay/README.md`.
Fail if any source, dataset, model, binary, image, endpoint, prompt, seed, budget,
or run fingerprint differs.

- [ ] **Step 2: Run identical isolated arms**

For each approved benchmark, run `baseline`, `candidate_legacy`, and `candidate`
with identical pinned inputs. Resume only through matching fingerprints; never
reuse stores across arms.

- [ ] **Step 3: Normalize and compare**

Use the repository normalizer, legacy comparison, merge, summarize, and compare
commands. Retain aggregate IDs, rates, sizes, latency, call counts, and hashes;
remove questions, memories, answers, and judge text from shareable summaries.

- [ ] **Step 4: Apply every acceptance gate**

Require stale-memory leakage reduction, state-resolution gain, per-benchmark
answer-accuracy limits, p95 latency limit, write-call limit, context-size limit,
zero silent partial-write success, and exact legacy ordering/scoring. One failed
or missing metric keeps `SA-08` incomplete.

- [ ] **Step 5: Record and commit only content-free evidence**

Update the ledger with manifest/run fingerprints, summary hashes, scalar gates,
exit statuses, and the exact candidate SHA. Never commit ignored raw runs or
content-bearing upstream results.

```bash
git add docs/designs/mem0-0-state-aware-status.md
git commit -m "docs: record official benchmark gate evidence"
git push origin feat/state-aware-memory-python
```

### Task 6: Validate OMP behavior after installation

**Files:**
- Read: `deployment/mem0_0_arch/harnesses/omp/runbook.md`
- Read: `deployment/mem0_0_arch/harnesses/omp/mcp.json.example`
- Read: `deployment/mem0_0_arch/harnesses/omp/policy-fragment.md`
- Modify after separate approval: the dedicated OMP profile under `/home/will/.omp`
- Modify: `docs/designs/mem0-0-state-aware-status.md`

**Interfaces:**
- Consumes: an OMP installation owned by the system workflow and explicit approval for the dedicated profile.
- Produces: behavioral evidence for exactly one `mem0-local` MCP server with built-in competing memory disabled.

- [ ] **Step 1: Verify the dependency and effective config paths**

Record OMP version, profile, config hashes, and duplicate-server discovery.
Stop if OMP is absent, a second memory backend is active, or another
`mem0-local` definition is effective.

- [ ] **Step 2: Install only the dedicated profile configuration**

After approval, adapt the repository example without embedding credentials.
Use `user_id=will`, `agent_id=omp`, `machine_id=arch-local`,
`project_id=mem0_0`, and the exact data class required by each operation.

- [ ] **Step 3: Exercise transport and fresh-session behavior**

Require initialize, discovery of the six expected tools, fresh-session scoped
CRUD, search-before-answer, durable-write policy, compaction/shutdown/restart
persistence, exact synthetic cleanup, and receipt reconciliation. A stdio
initialize/tools-list handshake alone does not pass this task.

- [ ] **Step 4: Record and commit the behavioral result**

Set `SA-09` to `DONE` only after every behavior passes. Otherwise retain the
profile as non-default or remove only the exact new profile changes, record the
failure, and keep the existing harness configuration unchanged.

```bash
git add docs/designs/mem0-0-state-aware-status.md
git commit -m "docs: record OMP behavioral validation"
git push origin feat/state-aware-memory-python
```

### Task 7: Freeze and verify the merge candidate

**Files:**
- Modify: `docs/designs/mem0-0-state-aware-status.md`
- Modify: `docs/designs/mem0-0-state-aware-handover.md`
- Modify: draft PR #1 description/checklist

**Interfaces:**
- Consumes: every required implementation, provider, benchmark, and applicable OMP gate.
- Produces: one exact pushed candidate SHA with full-suite verifier and critic evidence.

- [ ] **Step 1: Freeze scope and rebase decision**

Fetch current fork and upstream refs read-only. Compare the branch to fork
`main` and pinned upstream; decide explicitly whether selective reconciliation
is necessary. Never merge upstream wholesale into this branch.

- [ ] **Step 2: Run complete supported verification**

Run the full Python suite with disposable Redis, server/deployment selections,
deterministic and full bounded acceptance at the required cadence, TypeScript
Jest/build/Prettier, exact `tsc --noEmit` baseline comparison, Ruff, docs index
coverage, and `git diff --check`. Remove the disposable fixture afterward.

- [ ] **Step 3: Obtain fresh independent verification and adversarial review**

The verifier must run the full suite and exercise changed behavior. The critic
must review scope, secrets, persistence, compatibility, reports, and operational
claims. Iterate on failures; do not waive them because prior checkpoints passed.

- [ ] **Step 4: Update durable records and draft PR**

Record exact commit/tree attestations, commands, exit statuses, remaining known
issues, and all gate decisions. Make the PR checklist match reality. Push
without force only after verifying the exact remote target.

### Task 8: Decide merge and production cutover separately

**Files:**
- Modify: `docs/designs/mem0-0-state-aware-status.md`
- Read: `deployment/mem0_0_arch/README.md`

**Interfaces:**
- Consumes: a verified frozen candidate and accurate draft PR.
- Produces: first a merge decision; only later, if separately approved, an immutable cutover record and rollback evidence.

- [ ] **Step 1: Decide whether the PR is ready for review**

Confirm every merge prerequisite and record the disposition of the pre-existing
server auth/params coupling. Converting from draft is not production approval.

- [ ] **Step 2: Decide and execute merge**

Merge only with explicit approval and green required checks. Preserve the
feature branch and evidence until post-merge verification completes.

- [ ] **Step 3: Re-run the cutover gate in preview mode**

```bash
python -m deployment.mem0_0_arch.cutover
```

Expected before authorization: a nonzero exit and machine-readable unmet gates.
Do not manufacture a cutover record or bypass any reported gate.

- [ ] **Step 4: Request separate cutover approval**

Present the exact merged SHA, quiesced snapshot receipt, live-candidate
reconciliation, rollback-image provenance, canonical state paths, backed-up
harness mutations, planned interruption, and rollback command. Merge approval
does not imply cutover approval.

- [ ] **Step 5: Execute and verify the approved cutover**

Use only the repository recorder/runbook. Retarget backed-up harness configs
atomically, disable but retain legacy, exercise rollback with the exact preview
hash, reconcile both directions, and keep all legacy data and recovery artifacts
until a later explicit retirement decision.
