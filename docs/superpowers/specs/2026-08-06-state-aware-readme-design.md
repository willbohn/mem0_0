# State-Aware README Notice Design

## Goal

Make the repository root README explicit about the compatibility-preserving state-aware OSS work in this fork without rewriting or weakening unrelated upstream Mem0 documentation.

## Audience

The notice is for contributors and operators who land on this branch and need to distinguish the fork's implementation status from upstream managed-platform claims and benchmark results.

## Placement and Structure

Add one prominent `## State-aware OSS work in this fork` section near the top of `README.md`, immediately before the existing `## New Memory Algorithm (April 2026)` section.

The section will:

- Identify the work as branch-specific, compatibility-preserving OSS development across Python, REST, and TypeScript.
- Explain current-state suppression, preserved historical recall, disputed ambiguous conflicts, and the `legacy` ranking escape hatch.
- Summarize the verified implementation checkpoint: full suites, bounded shadow deployment, reconciliation, and synthetic lifecycle exercises.
- State that the official acceptance benchmark has not run and that the managed-platform benchmark table below does not prove acceptance for this candidate.
- State that production cutover and installed-harness retargeting have not occurred and that the legacy deployment remains intact.
- Link to the rollout design, current handover, evaluation overlay, and local deployment runbook for authoritative details.

## Existing README Content

Preserve all unrelated README sections verbatim. The existing evidence-bound extraction bullet may be adjusted only if needed to avoid duplication or an implication that the managed-platform numbers validate the OSS candidate.

Do not add local ports, private paths, credentials, secret-bearing examples, benchmark projections presented as results, or cutover instructions.

## Validation

- Confirm all new relative links resolve to tracked repository paths.
- Run `git diff --check`.
- Review the focused README diff for benchmark and rollout accuracy.
- Confirm no unrelated README section changed.
- Confirm the worktree contains only the already-approved handover update, this design record, and the intended README change.

No runtime, test-suite, CI/CD, deployment, harness, package, or API behavior changes are part of this task.
