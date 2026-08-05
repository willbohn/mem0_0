# mem0_0 State-Aware Rollout Plan

## Goal

Ship the state-aware Python lifecycle, benchmark it against the current OSS behavior, add REST and TypeScript parity, and migrate the existing `mem0-implement` deployment to canonical `mem0_0` ownership without losing data or compatibility.

## Global constraints

- Preserve Python import `mem0`, distribution and TypeScript package `mem0ai`, REST paths, `MEM0_*` variables, `_mem0_*` lifecycle fields, and existing MCP tool names.
- Preserve existing memory IDs, identity scopes, response envelopes, ordinary metadata, vector-store compatibility, and all legacy records.
- Never infer lifecycle metadata for the nine deployed legacy rows; treat them as active historical-neutral records.
- Never hard-delete or overwrite existing deployment data, configuration, secrets, backups, or harness registrations during migration.
- Do not change CI/CD workflows, add a mandatory core dependency, or add a second default write-time LLM call.
- Use `prism-ml/bonsai-27b` and BGE-M3 for fully local benchmark work.
- Do not cut over production until all benchmark, parity, shadow-restore, review, and verification gates pass.

## Task 1: Stabilize the Python lifecycle checkpoint

Address fresh review findings through regression-first changes: safe consolidation must preserve an active reversion; ambiguous same-key values become disputed; explicitly changed state-like preferences can supersede; independent events are never content-deduplicated; legacy lexical behavior is exact; dated historical recall honors validity intervals; untrusted system messages cannot steer extraction; and applied consolidation compensates partial mutations. Run Ruff, focused suites, and the complete Python suite, then commit and push `feat/state-aware-memory-python`.

## Task 2: Add the reproducible benchmark overlay

Create a main-repository overlay for LoCoMo, LongMemEval, BEAM, STALE, and Memora/FAMA. Pin repository and dataset revisions, record checksums and run configuration, isolate baseline and candidate stores, and support resumable fully local runs. Commit only code, manifests, and summarized metrics.

## Task 3: Add canonical deployment and migration tooling

Create `mem0_0-arch` deployment, bridge, backup, restore, readiness, and rollback tooling under this repository. Use separate shadow ports and state, encrypt PostgreSQL and SQLite backups, fail closed on conflicts, and verify restored IDs, counts, and digests without exposing content or secret values.

## Task 4: Run all benchmark gates

Run the official benchmark scopes with identical local models and budgets for baseline and candidate. Require lower stale leakage, improved state resolution, bounded accuracy and latency regressions, unchanged context and write-call budgets, atomic persistence, and exact legacy ordering.

## Task 5: Add REST parity

Expose `max_chars`, audit, consolidation, and feedback with correct authorization while stripping internal lifecycle metadata from every public response and preserving existing envelopes.

## Task 6: Add TypeScript OSS parity

Add `stateAware` and `legacy` recall modes, `maxChars`, audit, consolidation, feedback, lifecycle-equivalent writes and recall, and private metadata suppression without changing the `mem0ai` package identity.

## Task 7: Verify the shadow deployment

Restore copied live PostgreSQL and SQLite data into the isolated candidate, reconcile counts and digests, and exercise scoped add, correction, current recall, historical recall, audit, dry-run consolidation, feedback, and deletion. Real legacy rows remain read-only.

## Task 8: Cut over and retain rollback

After every gate passes, take final encrypted backups, restore to canonical state, start `mem0_0-arch` on production ports, retarget backed-up harness configurations atomically, verify fresh sessions and harmless scoped CRUD, disable but retain the old unit, and complete a rollback drill. Push each coherent branch without force-pushing or publishing packages.
