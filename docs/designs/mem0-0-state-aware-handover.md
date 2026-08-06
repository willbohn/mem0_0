# mem0_0 State-Aware Implementation Handover

Last refreshed: 2026-08-06 09:22 JST

This handover is for the next agent continuing the state-aware lifecycle, benchmark, and local deployment work in `/home/will/Documents/CodingWorkspaces/mem0_0`. Treat it as a checkpoint, not as authority over the current repository or runtime: re-run the content-free checks below before changing anything.

## Read first

1. Read the repository `AGENTS.md` in full.
2. Preserve all unrelated changes if the worktree is no longer clean.
3. Never print, commit, copy into chat, or rewrite values from `.env`, key, credential, or private runtime files.
4. Do not change CI/CD workflows.
5. Do not cut over, retarget installed harnesses, disable the legacy service, publish packages, or push a new commit without satisfying the gates in this document and obtaining any required current approval.
6. If Arch provisioning through `paru` becomes necessary, notify Will before installing anything. No `paru` package was needed through this checkpoint.

## Git checkpoint

- Branch: `feat/state-aware-memory-python`
- Implementation candidate: `75021d0a616e7939eb58086f7a46c9dd86433a6b`
- Candidate source-tree SHA-256: `33fb027e79dd8335ebeb01b90d6c655f2941f0bff75ff757caef62b57851832c`
- Base branch at the checkpoint: local `main` at `50bdaaea`, tracking `origin/main`
- The feature branch contains 29 implementation commits beyond that base and has no configured upstream.
- Remote fork: `origin` (`willbohn/mem0_0`); upstream project: `upstream` (`mem0ai/mem0`).
- The final candidate SHA has not been pushed. Earlier push authorization referred to an older SHA, so obtain renewed approval for the exact revision being pushed. Never force-push.

The handover documentation commit, if present above the implementation candidate, is documentation-only. The currently running shadow image is intentionally attested to the implementation candidate, not to a later documentation-only commit.

Start by checking:

```bash
git status --short --branch
git rev-parse HEAD
git log --oneline --decorate -15
git diff --check
```

## What is implemented

### Python OSS

- Evidence-bound, single-call extraction with source-message validation and private `_mem0_*` lifecycle fields.
- State, preference, event, plan, relationship, instruction, and procedure kinds.
- Conservative corroboration, supersession, dispute handling, canonical state chains, reversions, and explicit linked-move state transitions.
- Complete-embedding-before-persistence behavior with compensating deletion and typed persistence failures.
- State-aware recall as the default, with `legacy` mode preserving former ranking semantics.
- Dense, lexical, and entity candidate union followed by weighted RRF, state filtering, canonical deduplication, optional reranking, `explain`, and `max_chars`.
- Unicode-aware lexical fallback and historical-query handling. Boilerplate such as “conversation history” does not by itself make a current-state question historical.
- Sync and async `audit()`, `consolidate()`, and `feedback()` operations.
- Reserved lifecycle metadata is stripped from ordinary public results.

Primary files:

- `mem0/memory/main.py`
- `mem0/memory/lifecycle.py`
- `mem0/configs/base.py`
- `mem0/exceptions.py`

### REST and TypeScript OSS

- REST exposes `max_chars`, explain data, audit, consolidation, and feedback with admin authorization where required.
- TypeScript OSS implements `stateAware` and `legacy` modes, `maxChars`, lifecycle writes and recall, audit, consolidation, feedback, and private metadata suppression.
- Existing Python import name `mem0`, distribution/package identity `mem0ai`, REST paths, `MEM0_*` variables, response envelopes, MCP tool names, and legacy records remain compatible.

Primary files:

- `server/main.py`
- `server/provider_readiness.py`
- `mem0-ts/src/oss/src/memory/index.ts`
- `mem0-ts/src/oss/src/memory/lifecycle.ts`

### Evaluation overlay

- Reproducible baseline/candidate manifests, isolated stores, source/model/dataset provenance, resumable runs, retrieval judging, and lifecycle-specific metrics are under `evaluation_overlay/`.
- The local write path is bounded to one LLM call, 1,024 output tokens, a 1,200-second request timeout, and no retry.
- State benchmark runs fail closed when a labeled current or stale session persists no memories.
- Candidate server/image/source attestation is mandatory.

### Canonical naming and deployment

- A fresh direct Python installation now selects `~/.mem0_0`.
- `MEM0_DIR` still takes precedence. If only `~/.mem0` exists, compatibility fallback keeps using it until explicit migration; nothing silently moves or deletes the legacy directory.
- The canonical shadow deployment lives under `deployment/mem0_0_arch/` and uses separate ports, paths, volumes, credentials, encrypted backup artifacts, source attestation, reconciliation, readiness, live verification, and rollback tooling.
- Package/import/API compatibility deliberately remains `mem0`/`mem0ai`; this is a fork/runtime ownership rename, not a breaking namespace replacement.

## Current local deployment snapshot

Refreshed content-free on 2026-08-06 at 09:22 JST:

| Resource | Legacy | Canonical shadow |
|---|---|---|
| API | `127.0.0.1:8888` | `127.0.0.1:28889` |
| PostgreSQL | `127.0.0.1:15432` | `127.0.0.1:25432` |
| State | `~/.local/share/mem0-arch` | `~/.local/share/mem0_0-arch` |
| Config | `~/.config/mem0-arch` | `~/.config/mem0_0-arch` |

- Legacy app container: running and healthy.
- Shadow app container: running and healthy.
- Shadow readiness: API ready, private config verified, private history verified, PostgreSQL ready.
- Shadow image labels exactly match candidate revision `75021d0a` and tree digest `33fb027e...`.
- Neither production cutover nor installed-harness retargeting has occurred.
- The legacy service, directories, volumes, config, and backups remain intact.
- Do not enable or replace production units yet.

Private runtime files exist locally and are intentionally not reproduced here:

- `~/.config/mem0_0-arch/deployment.env` — non-secret pins, mode `0600`
- `~/.config/mem0_0-arch/shadow.env` — private runtime environment
- `~/.config/mem0_0-arch/bridge-api-key` — file-only admin authentication
- `~/.local/share/mem0_0-arch/runtime/.mem0_0/` — canonical local state

Current verified migration receipt:

```text
/home/will/.local/share/mem0_0-arch/backups/mem0_0-migration-20260805T182614Z.receipt.json
```

Latest reconciliation result:

- Cross-store consistency: verified
- Legacy state retained: true
- PostgreSQL: 10 rows, digest `6e578e1e912cd6850f77d095d02a557ce0522aff68927d434421bfef8aa6b67e`
- SQLite history: 24 rows, digest `18c1f0b2006f95c894ecae6ca0686e3f251c495790052b5f57a1f7a25a71111d`
- SQLite messages: 0 rows, empty-content digest `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`

Refresh without reading memory bodies:

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

Podman currently emits an overlay-versus-vfs storage warning while these operations still succeed. Do not follow its suggestion to delete container storage: that would be destructive and is outside this task.

## Live behavior evidence

The final scoped shadow exercise on candidate `75021d0a` passed:

1. Add a synthetic Kyoto residence.
2. Correct it to Osaka.
3. Confirm current recall includes Osaka and excludes Kyoto.
4. Confirm historical recall includes Kyoto.
5. Run audit.
6. Run safe consolidation as a dry run.
7. Record helpful feedback.
8. Delete only the exact synthetic IDs and drain their exact SQLite history/session rows.

Evidence:

- `ok=true`; every named check true.
- Created 2 memories; current recall returned 1; historical recall returned 1.
- Audit scanned 2 with 0 issues; consolidation proposed 0 unsafe actions.
- Writes took approximately 102.6 and 107.7 seconds on the local model; current and historical searches took approximately 79 and 75 ms.
- Cleanup removed exactly 2 synthetic memories, 4 matching history rows, and 2 matching message rows, with 0 scoped memories remaining.
- Receipt reconciliation passed again after cleanup, proving the migrated baseline was restored exactly.

The verifier defaults to the canonical private history DB and fails cleanup if exact vector, history, or message artifacts remain. It never calls `delete_all`.

## Test and review evidence

Fresh verification on `75021d0a` reported:

- Full Python Hatch suite: 2,055 passed, 73 skipped, 8 warnings in 114.93 seconds.
- Server suite: 94 passed.
- Deployment suite: 61 passed.
- Targeted state/evaluation/live-verifier tests: 151 passed.
- Exact linked-move sync and async regressions: 2 passed.
- Conversation-history current-intent regression: 1 passed.
- Evaluation empty-session and token/timeout budget regressions: 3 passed.
- Live-verifier cleanup module: 13 passed.
- Ruff on changed paths: passed.
- TypeScript Jest: 91 suites and 1,458 tests passed; 5 suites/42 tests skipped.
- TypeScript `tsup` build: passed.
- TypeScript Prettier check: passed.
- Fresh adversarial review of the final delta: no correctness findings; its focused benchmark/deployment selection passed 29 tests.
- Git worktree was clean after verification, and the temporary Redis Stack fixture was stopped and removed.

Known verification limitations:

- `tsc --noEmit` still reports 18 missing Jest/LangChain declaration errors that were classified as pre-existing/unrelated to this branch. Reproduce on the base tree before deciding whether to fix them here.
- `pnpm run test` failed in the wrapper environment with `unable to open database file`; invoking the installed Jest binary directly passed the suite. Diagnose the wrapper separately and do not hide this distinction.

Useful Python verification command:

```bash
REDIS_HOST=127.0.0.1 REDIS_PORT=16379 uvx hatch run dev_py_3_12:test
```

The full suite requires a Redis Stack fixture at the stated port. Do not leave a temporary fixture running after verification.

## Benchmark status and hard gate

The implementation and bounded local smokes pass, but the official acceptance benchmark has not run. Do not claim benchmark acceptance or perform cutover.

Bounded STALE smoke evidence:

- Public synthetic transition: Seattle to Austin.
- Three current-state questions each returned one current memory, zero stale leakage, no false supersession, and 84 retrieved characters.
- Search latencies were approximately 242, 223, and 220 ms.
- Write latencies were approximately 71.8 and 83.2 seconds.
- The unique Qdrant scope was deleted and verified empty afterward.

The official scopes include STALE, Memora/FAMA, LoCoMo, LongMemEval, and BEAM. STALE and Memora alone imply more than 143,000 writes across the required arms. At the measured local write rate, serial execution on the current endpoint is not operationally practical. Before running them, agree with Will on an acceptable compute plan, parallelism, provider budget, or explicitly revised gate. Do not silently weaken the acceptance criteria.

Acceptance still requires:

- At least 50% lower stale-memory leakage.
- At least 10 percentage points higher state-resolution accuracy.
- At most 1 percentage point answer-accuracy regression on LoCoMo, LongMemEval, or BEAM.
- At most 15% p95 search-latency regression.
- No increase in default write-time LLM calls.
- No increase in mean retrieved context size.
- Zero silent partial-write success.
- Exact legacy ordering and scoring in legacy mode.

## Recommended continuation order

1. Re-attest Git and runtime state using the content-free commands above.
2. Reproduce the TypeScript declaration and `pnpm run test` wrapper failures on both `main` and the feature candidate; record whether they are truly pre-existing.
3. Present a concrete official-benchmark compute/cost/time proposal to Will. Extra paid compute or a changed acceptance scope requires an explicit decision.
4. Run baseline, candidate-legacy, and candidate state-aware arms with identical pinned models, prompts, seeds, budgets, and isolated stores. Preserve manifests and summarized metrics only.
5. Evaluate every acceptance gate. If any gate fails, diagnose and iterate without touching the migrated legacy data.
6. Run the full Python, server, deployment, and TypeScript suites again, then use fresh verifier and critic agents.
7. Obtain approval for the exact final SHA and push the feature branch without force.
8. Only after all gates pass, take a new quiesced encrypted snapshot, reconcile it, exercise the cutover recorder and rollback drill, retarget backed-up harness configs atomically, and disable but retain the old unit.

## Commands that remain intentionally blocked

The cutover command should still return a machine-readable list of unmet gates:

```bash
python -m deployment.mem0_0_arch.cutover
```

Do not manufacture a cutover record or bypass those gates. Production-port changes, installed-harness changes, service disabling, package publishing, and data deletion are not authorized by this checkpoint.

## Key documentation

- `docs/designs/mem0-0-state-aware-rollout.md` — approved rollout plan and invariants
- `deployment/mem0_0_arch/README.md` — migration, readiness, live verification, cutover, and rollback runbook
- `evaluation_overlay/README.md` — benchmark overlay and provenance rules
- `docs/open-source/configuration.mdx` — state-aware and legacy configuration
- `docs/core-concepts/memory-operations/search.mdx` — search, audit, consolidation, and feedback API behavior
