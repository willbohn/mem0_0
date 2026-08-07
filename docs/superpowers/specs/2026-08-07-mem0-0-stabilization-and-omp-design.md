# mem0_0 Bounded Stabilization and OMP Compatibility Design

## Goal

Finish a bounded correctness and compatibility pass before standardizing on the
state-aware `mem0_0` fork for personal local use. The pass will selectively
incorporate relevant upstream Mem0 fixes, close confirmed local correctness and
scope-safety gaps, apply only low-risk lessons from Mnemopi, and prepare an
Oh My Pi (OMP) integration contract that can be validated after OMP is installed.

This work does not replace Mem0 with Mnemopi, run the official research benchmark,
or authorize production cutover.

## Current Checkpoint

- Feature branch: `feat/state-aware-memory-python`
- Documentation HEAD at design approval: `32d5d3212fbe7a18f968291f95d1d4e1d94e6a10`
- Running shadow implementation candidate: `75021d0a616e7939eb58086f7a46c9dd86433a6b`
- Running source-tree digest:
  `33fb027e79dd8335ebeb01b90d6c655f2941f0bff75ff757caef62b57851832c`
- Legacy and shadow deployments are healthy and reconciled. The legacy deployment
  remains authoritative and retained.
- The official STALE, Memora/FAMA, LoCoMo, LongMemEval, and BEAM acceptance run has
  not occurred and remains explicitly deferred.

The implementation checkout and running shadow image may intentionally identify
different commits while documentation or un-deployed stabilization work exists.
No document may describe un-deployed code as the running candidate.

## Research Basis

The design is based on a 2026-08-07 read-only comparison against official upstream
Mem0, its newly raised issues, the authoritative Mnemopi implementation inside
`can1357/oh-my-pi`, and OMP's native MCP configuration documentation.

Primary references:

- [Mem0 releases](https://github.com/mem0ai/mem0/releases)
- [Agent-scoped custom instructions PR](https://github.com/mem0ai/mem0/pull/6809)
- [Malformed local-model extraction issue](https://github.com/mem0ai/mem0/issues/6724)
- [Ollama embedding-dimension issue](https://github.com/mem0ai/mem0/issues/6734)
- [TypeScript caller-filter mutation issue](https://github.com/mem0ai/mem0/issues/6796)
- [Import-time config-directory issue](https://github.com/mem0ai/mem0/issues/6812)
- [REST internal-index metadata issue](https://github.com/mem0ai/mem0/issues/6825)
- [Mnemopi backend documentation](https://github.com/can1357/oh-my-pi/blob/main/docs/mnemosyne-memory-backend.md)
- [Mnemopi retain semantics](https://github.com/can1357/oh-my-pi/blob/main/docs/tools/retain.md)
- [Mnemopi project-bank fragmentation issue](https://github.com/can1357/oh-my-pi/issues/2412)
- [OMP MCP configuration](https://github.com/can1357/oh-my-pi/blob/main/docs/mcp-config.md)

External reports are inputs to local verification, not proof that a bug exists or
that a proposed fix is correct in this fork. Every adopted item requires a local
regression test.

## Design Principles

1. Preserve the existing Mem0 architecture, public package names, REST paths,
   response envelopes, MCP tool names, five-field scope, state-aware default, and
   exact `legacy` recall mode.
2. Selectively port behavior, never merge upstream wholesale into the state-aware
   implementation.
3. Fail closed on scope ambiguity, malformed extraction, secrets, and incomplete
   persistence.
4. Keep ordinary local-memory use inexpensive. Direct `infer=false` writes remain
   the default; provider-backed lifecycle extraction remains explicit.
5. Bound retrieved context before it reaches a harness.
6. Keep OMP integration additive and disabled until the harness exists and its
   fresh-session behavior is verified.
7. Classify the small personal validation suite honestly. It provides bounded local
   confidence, not official benchmark acceptance.

## Workstream 1: Selective Upstream Reconciliation

The implementation plan will compare each candidate fix against the feature branch
and port only missing behavior. At minimum, the reconciliation covers:

- Python identity-scope stripping from caller-supplied metadata.
- TypeScript OSS identity-scope stripping from caller-supplied metadata.
- Ollama embedder import failure as a normal `ImportError`, without interactive
  installation or process exit during module import.
- FastEmbed return normalization to an ordinary list.
- Hugging Face OpenAI-compatible endpoint API-key forwarding.
- Non-mutating dictionary/config merges and reranker fallback results.

The following post-base upstream work is intentionally deferred:

- `agent_custom_instructions` / `agentCustomInstructions`, because the introducing
  PR described its backing Platform field as not yet live and it does not solve the
  local OSS lifecycle contract by itself.
- CLI option parity, Oracle support, hosted Platform features, release-version
  bumps, CI workflow changes, and unrelated documentation.

Upstream reconciliation must not rewrite state-aware logic to match an older
upstream code shape. When both branches changed the same path, tests define the
required combined behavior.

## Workstream 2: Confirmed Correctness and Scope Hardening

### Extraction shape validation

Sync and async state-aware writes will validate that the decoded `memory` field is
a list and that each item is a mapping with a non-empty text value and valid exact
evidence. Bare strings, arrays, numbers, and malformed mappings will never reach
`.get()` calls or persistence.

Invalid individual items will be logged without their bodies and skipped. If no
valid grounded items remain, the call returns the existing no-extraction result and
persists only the eligible message history according to the current contract. A
provider or parser failure remains distinguishable from a valid empty extraction.

### Identity and caller-owned input

- Caller metadata cannot set or override `user_id`, `agent_id`, `run_id`, or
  `actor_id` in Python or TypeScript.
- TypeScript `add()` clones `filters` before normalization and never mutates caller
  objects.
- Reused config, filter, document, and reranker inputs remain byte-for-byte
  equivalent after calls unless the public API explicitly documents mutation.

### Search and listing edges

- Non-finite thresholds, including NaN and infinities, are rejected before embedding
  or vector-store work.
- `get_all(top_k=0)` and the corresponding REST route return zero rows.
- Existing valid boundaries and `legacy` ordering remain unchanged.

### Private metadata boundary

The REST admin serializer will exclude `text_lemmatized` and every `_mem0_*` field
from public metadata, matching ordinary SDK reads. Tests will cover scoped and
admin list, get, search, update, and lifecycle-operation response paths.

### Import and local-model behavior

- Importing `mem0` must not fail merely because the resolved local config directory
  is absent or read-only. Directory creation and config writes remain best-effort
  and happen only when needed.
- Ollama's default `nomic-embed-text` dimension becomes 768.
- The optional spaCy model becomes explicitly configurable while preserving
  `en_core_web_sm` as the default.
- Languages without a noun-chunk iterator fall back without raising, and an empty
  lemma falls back to the original token before the existing Unicode lexical path.

No new mandatory core dependency is introduced.

## Workstream 3: Bounded Harness Recall

Mnemopi's bounded injection is useful, but its storage and lifecycle model will not
be imported. The existing REST `max_chars` support will instead be carried through
the local MCP bridge.

For `search_memories`:

- Add an optional integer `max_chars` tool argument.
- Use 12,000 characters when the caller omits it.
- Reject booleans, negative values, and values above the hard limit of 16,384.
- Forward the accepted value to REST and verify the returned cumulative memory text
  does not exceed it.
- Retain the existing 4,096-character query limit, result-count limit, five required
  scope fields, and secret rejection.

This is additive to the MCP tool schema and does not rename tools or alter the REST
envelope.

Direct `infer=false` bridge writes remain the default because they are deterministic,
fast, and do not call the local LLM. Harness policy will permit `infer=true` only when
all of the following are true:

1. the local provider is ready;
2. the content is a durable state, preference, relationship, instruction, or
   procedure for which lifecycle extraction is useful; and
3. the caller accepts the measured local write latency.

For direct writes, corrections should update a known scoped memory ID when possible.
Adding an unlinked corrective raw memory must not be described as automatic
state-aware supersession.

## Workstream 4: OMP Compatibility Artifacts

OMP integration will use its native stdio MCP transport and the existing Python
bridge. Repository-owned artifacts will live under
`deployment/mem0_0_arch/harnesses/omp/` and contain no credential values.

The artifacts will include:

- an `mcp.json.example` using an absolute bridge command, local API URL, and
  `MEM0_API_KEY_FILE` path only;
- an OMP behavioral-policy fragment covering when to search, when to write, the five
  explicit scope fields, secret exclusion, confirmation for deletion, and
  `infer=false` versus `infer=true`;
- a runbook for a dedicated OMP profile, duplicate-server discovery checks, built-in
  memory disablement, validation, rollback, and removal; and
- content-free validation commands and expected tool names.

The intended OMP state is:

- a dedicated profile for local Mem0 validation;
- exactly one `mem0-local` server sourced from the OMP-native config;
- OMP's built-in Mnemopi/Hindsight memory backend set to `off` so there is no second
  automatic retain or prompt-injection path;
- hosted Mem0 configurations discovered from Claude, Codex, or other tools disabled
  for that profile; and
- `agent_id=omp`, `machine_id=arch-local`, and a stable explicit repository
  `project_id`, never a project identifier derived from a mutable Git-root heuristic.

The tracked example is not an installed harness configuration. No file under
`~/.omp`, no shell profile, and no neighboring `system-init_sync` file will be
changed during this workstream.

### Installation-dependent validation gate

After `system-init_sync` has installed OMP and the user separately approves local
harness configuration, validation must:

1. record the OMP version, active profile, effective config paths, and config hashes;
2. prove there is exactly one enabled local Mem0 MCP definition;
3. run OMP's MCP connection test and confirm the six expected tools;
4. exercise synthetic add, search, list, get, update, and confirmed delete with all
   five scope fields;
5. prove missing, wildcard, mismatched, secret-bearing, and unconfirmed-delete calls
   fail closed;
6. prove there is no cross-scope recall;
7. verify search result context respects `max_chars`;
8. verify a fresh OMP session follows search-before-answer and durable-write policy;
9. verify compaction, shutdown, and restart do not drop acknowledged writes; and
10. clean up only the exact synthetic IDs and reconcile the baseline.

Until that gate runs, documentation must say "OMP transport compatible; harness
behavior pending installation" rather than "OMP verified."

## Mnemopi Adoption Boundary

### Apply now

- Bounded recall context.
- Stable explicit project identity and fragmentation diagnostics.
- Flush/durability verification before shutdown claims.
- Clear differentiation between inexpensive direct retention and model-backed
  extraction.

The fork already has stronger fail-closed persistence, evidence-bound lifecycle
metadata, canonical supersession and reversion chains, dispute handling, audit,
feedback, exact legacy recall, and source attestation. Those invariants remain.

### Evaluate later

- Explicit structured `query_time` or as-of recall.
- Polyphonic vector, graph, fact, and temporal retrieval.
- Additional trust-tier or validator attestations.
- Tiered recall caching, episodic degradation, proactive graph linking, and recall
  counters.

Each item requires its own design and bounded comparison. None is part of this
stabilization pass.

### Reject

- Reporting requested write counts when individual writes failed.
- CWD- or Git-root-derived memory-bank identity without migration diagnostics.
- Destructive automatic embedding reconciliation.
- Running Mnemopi and mem0_0 as simultaneous default memory backends in OMP.

## Personal Local Acceptance Suite

The official benchmark gate remains unchanged and unrun. A separate suite will be
named **bounded personal local acceptance** and must never be presented as STALE,
Memora, LoCoMo, LongMemEval, or BEAM acceptance.

The suite will use deterministic synthetic or public fixtures and enforce these
budgets across the entire run:

- no more than 24 scenario families;
- no more than 120 total memory mutations;
- no more than 48 provider-backed `infer=true` writes;
- one local model configuration, with no paid provider requirement;
- no retry beyond the existing fail-closed request contract; and
- no personal memory bodies in reports or committed artifacts.

Scenario coverage will include:

- corrections and supersession;
- current versus historical recall;
- dispute, corroboration, and reversion;
- preference, event, relationship, instruction, and procedure behavior;
- malformed extraction and embedding/persistence failure atomicity;
- Python, REST, TypeScript, and MCP scope isolation;
- direct-write versus lifecycle-extraction behavior;
- exact legacy-mode ordering and scoring;
- Unicode and Japanese lexical fallback;
- context-size enforcement and content-free latency summaries; and
- OMP discovery and fresh-session behavior only after OMP is installed.

Acceptance requires every deterministic correctness assertion to pass, zero silent
partial-write success, zero cross-scope recall, exact cleanup of synthetic data, and
no candidate-only regression in the full supported suites. Latency and context
metrics will be recorded for local operational confidence; the small sample will not
be used to claim publication-grade performance thresholds.

## Verification Strategy

Implementation will be regression-first. Each adopted upstream behavior or issue
gets a test that fails on the pre-fix feature branch and passes after the change.

Required evidence before completion:

- focused Python lifecycle, configuration, embedding, vector-store, and server tests;
- focused TypeScript OSS tests, build, Prettier, and differential type-check review;
- bridge and deployment tests, including a transport-neutral MCP initialize and
  tools-list handshake;
- the complete Python, server, deployment, and TypeScript suites;
- Ruff and `git diff --check` on changed paths;
- the bounded personal local acceptance suite;
- fresh verifier execution of the full suite and affected behavior; and
- fresh adversarial review of scope, secret, persistence, and compatibility
  boundaries.

The known TypeScript baseline diagnostics must be compared against the same base
revision. Pre-existing diagnostics are not stabilization failures, while any new
candidate-only diagnostic is.

## Documentation Outputs

After implementation verification, update documentation in this order:

1. Add the bounded personal local acceptance contract and clearly separate it from
   the deferred official benchmark.
2. Add the OMP example, policy fragment, and pending-validation runbook.
3. Update the evaluation overlay documentation with the two distinct acceptance
   classes.
4. Refresh the handover's Git, runtime, upstream-reconciliation, and gate status
   using content-free checks.
5. Update only the existing state-aware fork notice in the root README. Preserve all
   unrelated README sections verbatim.

Documentation must identify which implementation SHA is tested, which source SHA is
running, and which OMP claims are pending. It must not include credentials, private
memory bodies, installed harness state that was not verified, or official benchmark
claims.

## Commit and Deployment Boundaries

- This design document is committed separately before implementation planning.
- Implementation work uses coherent regression-first commits; commit grouping will
  be defined in the implementation plan.
- No push occurs without a new explicit request.
- No CI/CD workflow changes are authorized.
- No shadow rebuild, production-port change, harness retarget, legacy shutdown,
  package publish, or data deletion is authorized by this design.
- A later shadow refresh must attest the exact tested implementation SHA and source
  digest before any runtime claim changes.
- Production cutover remains a separate, explicit decision after all applicable
  gates and rollback evidence.

## Completion Definition

The stabilization phase is complete only when the selected upstream fixes and
confirmed local gaps are regression-tested, all required suites and reviews pass,
the bounded personal local suite passes within budget, and documentation accurately
separates tested code, running code, deferred official benchmarks, and pending OMP
runtime validation.

Completion does not imply official benchmark acceptance, OMP runtime verification,
production cutover, harness retargeting, or legacy retirement.
