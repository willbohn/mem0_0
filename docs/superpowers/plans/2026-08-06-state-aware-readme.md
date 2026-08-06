# State-Aware README Notice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a prominent, accurate fork-status notice to the root README while preserving every unrelated upstream section verbatim.

**Architecture:** Insert one self-contained Markdown section immediately before the existing `## New Memory Algorithm (April 2026)` heading. The notice summarizes branch scope, compatibility, verified status, and outstanding gates, then links to the authoritative detailed documents instead of duplicating operational instructions.

**Tech Stack:** Markdown, Git, repository-relative links.

## Global Constraints

- Preserve all unrelated `README.md` content verbatim.
- Do not change code, APIs, packages, CI/CD, runtime services, deployment state, harness configuration, or benchmark scope.
- Do not include local ports, private paths, credentials, secrets, or memory bodies.
- Do not imply that managed-platform benchmark results validate the OSS candidate.
- Do not claim official benchmark acceptance or production cutover.
- Do not push.

---

### Task 1: Add and verify the state-aware fork notice

**Files:**
- Modify: `README.md:45`
- Include in documentation checkpoint: `docs/designs/mem0-0-state-aware-handover.md`
- Include in documentation checkpoint: `docs/superpowers/plans/2026-08-06-state-aware-readme.md`

**Interfaces:**
- Consumes: the verified status and gates in `docs/designs/mem0-0-state-aware-handover.md`.
- Produces: a root README notice with relative links to the rollout design, handover, evaluation overlay, and deployment runbook.

- [ ] **Step 1: Reconfirm the documentation-only worktree scope**

Run:

```bash
git status --short --branch
git rev-parse HEAD
```

Expected: branch `feat/state-aware-memory-python`; only the approved handover edit and this plan are uncommitted.

- [ ] **Step 2: Insert the exact notice before the existing benchmark section**

Add this block immediately before `## New Memory Algorithm (April 2026)`:

```markdown
## State-aware OSS work in this fork

This branch develops a compatibility-preserving state-aware memory lifecycle and retrieval path for the open-source Python SDK, REST server, and TypeScript OSS SDK.

- **Current and historical truth:** ordinary current-state recall suppresses superseded facts, historical queries can still retrieve earlier states, and ambiguous conflicts remain disputed instead of being silently deleted.
- **Compatibility:** existing `mem0`/`mem0ai` imports, API paths, environment variables, response envelopes, MCP tool names, and legacy records remain supported. `legacy` recall mode preserves the former ordering and scoring behavior.
- **Operational safety:** lifecycle writes fail closed on incomplete persistence, private lifecycle metadata stays out of ordinary public results, and automatic maintenance archives or suppresses rather than hard-deleting memories.

> **Acceptance status:** The implementation candidate `75021d0a` has passed the full Python, REST, deployment, and TypeScript suites, plus bounded shadow-deployment, reconciliation, and synthetic lifecycle checks. The official acceptance benchmark has **not** run. The managed-platform results below are upstream context, not acceptance evidence for this OSS candidate. Production cutover and installed-harness retargeting have not occurred; the legacy deployment remains intact.

See the [rollout design](./docs/designs/mem0-0-state-aware-rollout.md), [current handover](./docs/designs/mem0-0-state-aware-handover.md), [evaluation overlay](./evaluation_overlay/README.md), and [local deployment runbook](./deployment/mem0_0_arch/README.md).

```

Do not modify the existing benchmark section or its evidence-bound extraction bullet.

- [ ] **Step 3: Verify every new relative link**

Run:

```bash
test -f docs/designs/mem0-0-state-aware-rollout.md
test -f docs/designs/mem0-0-state-aware-handover.md
test -f evaluation_overlay/README.md
test -f deployment/mem0_0_arch/README.md
```

Expected: all four commands exit 0.

- [ ] **Step 4: Review the focused README diff**

Run:

```bash
git diff --unified=3 -- README.md
```

Expected: one inserted section immediately before `## New Memory Algorithm (April 2026)` and no changes to existing README lines.

- [ ] **Step 5: Validate the complete documentation change**

Run:

```bash
git diff --check
git status --short --branch
```

Expected: no whitespace errors; modified files are `README.md` and the approved handover, with this plan untracked until staged. No code test suite is required because the change is Markdown-only and introduces no executable behavior.

- [ ] **Step 6: Create the local documentation checkpoint**

Run:

```bash
git add README.md docs/designs/mem0-0-state-aware-handover.md docs/superpowers/plans/2026-08-06-state-aware-readme.md
git diff --cached --check
git commit -m "docs: describe state-aware fork status"
```

Expected: one local documentation commit containing only the README notice, refreshed handover, and implementation plan. Do not push.
