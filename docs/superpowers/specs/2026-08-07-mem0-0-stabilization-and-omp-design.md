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
- Legacy and shadow deployments were verified healthy and reconciled by the
  content-free readiness and receipt-reconciliation checks, most recently on
  2026-08-07. Re-run those checks before relying on runtime state. The legacy
  deployment remains authoritative and retained.
- This document was amended on 2026-08-07 following an independent adversarial
  design review; review-verified findings are annotated inline as "confirmed."
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

The pinned upstream comparison revision is `mem0ai/mem0` `main` at
`4a0a9a92a641b5da75023eca596a758a4f7ae101`, fetched read-only on 2026-08-07. As
of that date none of the cited issues has a merged upstream fix (the candidate
fix pull requests 6725, 5880, 6735, 6797, 6813, and 6827 are all unmerged), so
every adopted behavior is implemented and regression-tested locally rather than
cherry-picked.
Deferral rationales are dated external-evidence snapshots held to the same
lead-not-proof standard as adoptions; they are revisited whenever the pinned
upstream revision is advanced.

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

The implementation plan will compare each candidate fix against the pinned
upstream revision and port only missing behavior. The 2026-08-07 review verified
every item against the current branch; the reconciliation covers exactly:

- Python identity-scope stripping from caller-supplied metadata on `add()`
  (confirmed missing; `update()` is already protected).
- TypeScript OSS identity-scope stripping from caller-supplied metadata on
  `add()`, covering snake_case, camelCase, and `actor_id` (confirmed missing;
  `update()` is already protected).
- TypeScript `add()` cloning caller `filters` before normalization (confirmed
  mutating today), together with the newly confirmed defect that identity
  supplied only via `filters` with `infer=false` is never written into the
  stored payload and produces an unscoped orphan row. The cloning, stripping,
  and filters-identity fixes land together under one combined regression
  contract because they interact.
- Ollama embedder import failure as a normal `ImportError`, without interactive
  installation or process exit during module import (confirmed: the module
  currently calls `input()`, in-process pip, and `sys.exit(1)`).
- FastEmbed return normalization to an ordinary list (confirmed: a numpy array
  is returned today).
- Hugging Face OpenAI-compatible endpoint API-key forwarding, embedder only —
  this fork has no Hugging Face LLM module (confirmed: the configured key is
  never passed to the client).
- Non-mutating caller inputs, scoped to the confirmed mutation sites: the four
  non-LLM rerankers' exception paths, which write `rerank_score` into
  caller-supplied result dicts in place, and `LlmFactory.create`, which updates
  caller dict configs in place when kwargs are passed. Reranker failure already
  returns the original candidate list at every level; that behavior is pinned,
  not changed.

Layer policy for identity keys: the SDKs strip caller-supplied identity keys
from metadata with a logged warning, matching the existing `update()` contract
in both languages, because benign callers echo response metadata back into
`add()`. The MCP bridge continues to reject rather than strip. This resolves
the strip-versus-reject ambiguity in favor of explicit per-layer behavior.

The following proposed items were verified already implemented on this branch
and are removed from scope with no work budgeted: write-side stripping of
caller-supplied `_mem0_*` fields (Python and TypeScript, all write paths),
TypeScript `maxChars` and recall-mode parity, the transport-neutral MCP
initialize and tools-list handshake test, infinity rejection in threshold
validation, and `_mem0_*` exclusion in the REST admin serializer.

The following post-base upstream work is intentionally deferred:

- `agent_custom_instructions` / `agentCustomInstructions`. Upstream merged and
  released it (v2.0.17 / ts-v3.1.5) despite the introducing PR's note that the
  backing Platform field was not yet live; the field is a hosted-Platform
  server-side field with no effect on self-hosted OSS behavior, which is the
  operative reason it does not apply to this fork's local lifecycle contract.
- CLI option parity, Oracle support, hosted Platform features, release-version
  bumps, CI workflow changes, and unrelated documentation.

Upstream reconciliation must not rewrite state-aware logic to match an older
upstream code shape. When both branches changed the same path, Design Principle
1 is the tiebreak: ported behavior must not alter state-aware or exact-legacy
semantics, and the regression test for each port must assert both the ported
fix and the preserved fork behavior it could have disturbed.

## Workstream 2: Confirmed Correctness and Scope Hardening

### Extraction shape validation

Sync and async state-aware writes will validate that the decoded response is a
mapping, that its `memory` field is a list, and that each item is a mapping with
a non-empty string text value and valid exact evidence. Bare strings, arrays,
numbers, and malformed mappings will never reach `.get()` calls or persistence.
(Confirmed current behavior: every non-mapping item crashes `add()` with a raw
`AttributeError` or `TypeError` outside the documented exception contract, and
a non-mapping top-level response is silently coerced to an empty extraction.)

Failure classes are separated explicitly:

- A provider or transport failure keeps raising the existing typed `LLMError`.
- A parse-level failure — an undecodable response, a top level that is not a
  mapping, or a `memory` field that is not a list — raises a new typed
  extraction-parse error, distinct from `LLMError` and from a valid empty
  extraction. Today such responses are silently coerced to the valid-empty
  result; that coercion is removed by this change.
- Invalid individual items inside a valid list are logged without their bodies
  and skipped, while valid items are persisted. Whenever at least one item was
  skipped, the response discloses the count of skipped invalid items in an
  additive top-level field, so a partial extraction is never silently
  indistinguishable from a complete one. If no valid grounded items remain, the
  call returns the existing no-extraction result carrying the same disclosure
  field and persists only the eligible message history according to the current
  contract.

The disclosure rule is what reconciles item skipping with the acceptance
suite's zero-silent-partial-write requirement, and it keeps a systematically
malformed local model visible to callers instead of degrading into an
indefinite stream of silent no-op writes.

### Identity and caller-owned input

- Caller metadata cannot set or override `user_id`, `agent_id`, `run_id`, or
  `actor_id` in Python or TypeScript: the SDKs strip these keys with a logged
  warning, matching the existing `update()` behavior, and the bridge rejects
  them, per the layer policy in Workstream 1.
- TypeScript `add()` clones `filters` before normalization and never mutates
  caller objects, and identity supplied only via `filters` with `infer=false`
  is written into the stored payload instead of producing an unscoped orphan
  row.
- Reused config, filter, document, and reranker inputs remain byte-for-byte
  equivalent after calls unless the public API explicitly documents mutation.
  The confirmed mutation sites in scope are the four non-LLM rerankers'
  exception paths and `LlmFactory.create` with dict configs and kwargs.

### Search and listing edges

- NaN thresholds are rejected before embedding or vector-store work. Infinities
  are already rejected by the existing range check, so the remaining gap is NaN
  only, and its confirmed failure mode is silent over-inclusion — a NaN
  threshold disables threshold filtering entirely in both recall modes — not a
  crash.
- `get_all(top_k=0)`, state-aware `search(top_k=0)`, and the corresponding REST
  routes return zero rows. (Confirmed today: both return exactly one row via
  the same append-then-check defect while legacy search already returns zero,
  so the two modes disagree. The existing vacuous accepts-top-k-zero test is
  replaced by value-asserting regressions.)
- The recall-mode fallbacks for configs lacking a `recall` attribute are
  unified on state-aware across write, search, and update paths (today writes
  fall back to state-aware while search and update fall back to legacy).
- Existing valid boundaries and `legacy` ordering remain unchanged.

### Private metadata boundary

The REST admin serializer already excludes every `_mem0_*` field, and that
behavior is pinned by an existing test. The confirmed remaining leak is
`text_lemmatized` on the admin all-memory listing only, plus promoted keys such
as `actor_id`, `role`, and `attributed_to` that land inside admin metadata
while SDK reads exclude them. The code change is scoped to that one serializer,
aligning it with ordinary SDK reads. Scoped list, get, search, update, and
lifecycle-operation responses were verified already clean, and no code change
is budgeted for them. The test matrix still lands in full — scoped and admin
list, get, search, update, and lifecycle-operation response paths — because
almost none of it exists today: no test in the repository currently asserts
`text_lemmatized` absence on any response path.

### Import and local-model behavior

- Importing `mem0` must not fail merely because the resolved local config
  directory is absent or read-only. Directory creation and config writes remain
  best-effort and happen only when needed. (Confirmed by reproduction: the
  unguarded import-time `os.makedirs` raises `PermissionError` when the
  directory is absent under an unwritable parent; the config read and write
  helpers are already best-effort.)
- Ollama's default `nomic-embed-text` dimension becomes 768. This changes no
  vector-store schema: the embedder's `embedding_dims` never sizes collections
  (`embedding_model_dims` does) and is consumed only by telemetry, and the
  TypeScript SDK already defaults to 768.
- Crash hardening only for the lexical pipeline: languages without a noun-chunk
  iterator fall back without raising, and the currently unguarded
  `extract_entities` call sites on the search paths gain the same protection
  the add paths already have. These guards change no scoring for the default
  English model.
- Deferred to a separate bounded design: making the spaCy model configurable
  and the empty-lemma original-token fallback. Both change `lemmatize_for_bm25`,
  which produces the persisted `text_lemmatized` index and the legacy-mode
  query lemmatization; changing either would break the exact-legacy ordering
  gate and skew already-stored index rows against new queries with no reindex
  path. They require their own design with index versioning and migration.
- The `infer=false` invalid-message warning stops logging message bodies
  verbatim, consistent with the no-bodies logging rule applied to extraction
  items.

No new mandatory core dependency is introduced.

## Workstream 3: Bounded Harness Recall

Mnemopi's bounded injection is useful, but its storage and lifecycle model will
not be imported. The context bound is enforced in the local MCP bridge itself,
so it holds identically regardless of the server's recall mode. (Confirmed: the
core `max_chars` is honored only in state-aware recall and is a silent no-op in
legacy mode in both SDKs, so server-side forwarding alone cannot guarantee the
bound.)

For `search_memories`:

- Add an optional integer `max_chars` tool argument.
- Use 16,384 characters when the caller omits it — deliberately equal to the
  bridge's existing `MAX_CONTENT_CHARS` write bound, so any single legally
  stored memory can always be returned as the top result and no memory becomes
  permanently unreachable through the bridge.
- Reject booleans, zero, negative values, and values above the hard limit of
  16,384.
- Enforce the budget bridge-side on the returned ranked results with the same
  semantics the core uses in state-aware mode: the budget is cumulative over
  the returned memories' text (not the response envelope), applied in rank
  order; an over-budget memory is omitted whole and never truncated, and a
  lower-ranked shorter memory may still be admitted. The bridge does not
  forward `max_chars` to REST, so the REST request shape is unchanged and the
  bound is recall-mode-independent. The tool may therefore return fewer than
  `top_k` results.
- Disclose enforcement: whenever at least one result was omitted for budget,
  the tool response includes the count of omitted results in an additive
  field. A bounded result set is never silently indistinguishable from a
  complete one.
- Retain the existing 4,096-character query limit, result-count limit, five
  required scope fields, and secret rejection.

This is additive to the MCP tool schema and does not rename tools or alter the
REST envelope. Because a default budget now applies, recall composition through
the bridge can change for existing callers whose cumulative result text exceeds
16,384 characters; that change is deliberate, disclosed per response, and
covered by the acceptance suite.

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
bridge. The five-field scope requirement and secret rejection are enforced by
the bridge, not by REST or the SDKs, so OMP must always be wired through the
bridge and never directly to the REST endpoint. Repository-owned artifacts will
live under `deployment/mem0_0_arch/harnesses/omp/` and contain no credential
values.

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
- OMP's built-in Mnemopi/Hindsight memory backend at `off` — its documented
  default — so there is no second automatic retain or prompt-injection path;
- hosted Mem0 configurations discovered from Claude, Codex, or other tools
  disabled for that profile via the profile's name-matched `disabledServers`
  denylist (discovered server names are enumerated first, since disablement
  matches by name; the runbook also confirms no project-level `.omp/mcp.json`
  or compat `.mcp.json` exists in the working repository, because
  project-level config applies under every profile); and
- `agent_id=omp`, `machine_id=arch-local`, and the explicit constant
  `project_id=mem0_0` for this repository. Project identity is always an
  operator-chosen constant recorded in the policy fragment, never computed
  from the working directory or Git metadata; the bridge cannot enforce that
  negative rule structurally, so the policy fragment and validation runbook
  carry it. Adopting `agent_id=omp` extends the operator's documented harness
  enumeration, and the operator's harness policy documents are updated to
  include `omp` when OMP is configured; no code allowlist exists or is added.

The tracked example is not an installed harness configuration. No file under
`~/.omp`, no shell profile, and no neighboring `system-init_sync` file will be
changed during this workstream.

### Installation-dependent validation gate

After `system-init_sync` has installed OMP and the user separately approves
local harness configuration, a separately approved configuration step first
creates the intended state: it creates the dedicated profile, writes the single
`mem0-local` server into that profile's OMP-native config, verifies the memory
backend remains `off`, and disables discovered hosted Mem0 servers by their
discovered names. Only that approved step may write under `~/.omp`, and only
within the dedicated profile's directory and its profile-scoped denylist.
Validation must then:

1. record the OMP version, active profile, effective config paths, and config hashes;
2. prove there is exactly one enabled local Mem0 MCP definition;
3. run OMP's MCP connection test and confirm the six expected tools;
4. exercise synthetic add, search, list, get, update, and confirmed delete with all
   five scope fields;
5. prove missing, wildcard, secret-bearing, and unconfirmed-delete calls fail
   closed with typed errors; prove mismatched-scope get, update, and delete
   calls fail closed with `scope_mismatch`; and prove mismatched-scope search
   and list calls return empty results with zero cross-scope leakage, the
   strongest available semantics for reads absent an ownership registry;
6. prove there is no cross-scope recall;
7. verify search result context respects `max_chars`;
8. verify a fresh OMP session follows search-before-answer and durable-write policy;
9. verify compaction, shutdown, and restart do not drop acknowledged writes; and
10. clean up only the exact synthetic IDs and reconcile the baseline.

Until that gate runs, documentation must say "OMP integration prepared;
transport and harness behavior pending installation" rather than "OMP
verified." Transport compatibility is itself installation-gated: the bridge
returns a fixed MCP protocol version of `2025-06-18` and does not negotiate, so
the handshake against OMP's MCP revision is unproven until OMP runs it, and the
tracked `mcp.json.example` is provisional until the same gate.

## Mnemopi Adoption Boundary

### Apply now

- Bounded recall context.
- Stable explicit project identity and fragmentation diagnostics.
- Flush/durability verification before shutdown claims.
- Clear differentiation between inexpensive direct retention and model-backed
  extraction. This is a lesson drawn from Mnemopi's synchronous-row-write
  versus background-extraction split; OMP's retain tool does not itself expose
  a per-call `infer=false` analogue.

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
  (The cited fragmentation issue was fixed upstream by hashing the resolved
  working directory only; the motivation stands, but current OMP derives bank
  identity from the working directory, not the Git root.)
- Destructive automatic embedding reconciliation.
- Running Mnemopi and mem0_0 as simultaneous default memory backends in OMP.

## Personal Local Acceptance Suite

The official benchmark gate remains unchanged and unrun. A separate suite will be
named **bounded personal local acceptance** and must never be presented as STALE,
Memora, LoCoMo, LongMemEval, or BEAM acceptance.

The suite will use deterministic synthetic or public fixtures and enforce these
budgets across the entire run:

- no more than 24 scenario families;
- no more than 120 total memory mutations, where every write, update, and
  delete — including cleanup — counts against the budget;
- no more than 48 provider-backed `infer=true` writes, reserved exclusively
  for the supersession, corroboration, dispute, reversion, and
  direct-write-versus-lifecycle families; every other family runs
  deterministically with `infer=false` or a stubbed LLM and spends none of
  this budget;
- a wall-clock cap of 100 minutes on the provider-backed portion of a full
  run, where exceeding the cap fails the run rather than extending it;
- one local model configuration, with no paid provider requirement;
- no retry beyond the existing fail-closed request contract; and
- no personal memory bodies in reports or committed artifacts.

The suite runs against an isolated store scope and never against the migrated
legacy data. Any run that touches the shadow deployment ends with receipt
reconciliation against the current migration receipt, and that reconciliation
must pass for the run to count. A full-suite run is required once per completed
implementation workstream and once before the completion claim; per-change
verification uses only the deterministic `infer=false` subset.

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

The known TypeScript baseline diagnostics must be compared against the pinned
base revision: local `main` at `50bdaaea`, whose recorded baseline is 26
diagnostics. Pre-existing diagnostics are not stabilization failures, while any
new candidate-only diagnostic is.

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
