# OMP dedicated-profile runbook for `mem0-local`

**Status: OMP integration prepared; transport and harness behavior pending installation.**

This runbook describes the lifecycle of a dedicated OMP profile that
connects to the `mem0-local` MCP bridge (`deployment/mem0_0_arch/bridge/bridge.py`).
Every command below is content-free — none of it embeds credential values —
and every claim about how OMP actually behaves at runtime is explicitly
labeled "pending installation" below, because this artifact was authored
without OMP installed and has not been exercised against a live OMP
process.

## 1. Create and select the dedicated profile

Create or select a profile scoped to this validation so it never shares
user-level MCP config with any default or other-project OMP profile:

```text
omp --profile mem0-validation
```

Per OMP's documented profile model, a named profile isolates *user-level*
MCP config only: the active profile's user scope resolves to
`~/.omp/profiles/mem0-validation/agent/mcp.json` instead of the default
`~/.omp/agent/mcp.json`. Place the contents of `mcp.json.example` (adjusted
for the local `.venv` interpreter path and bridge path if they differ) at
that profile's user MCP config path — pending installation for confirming
the exact path and that the profile picks it up as expected.

## 2. Verify `memory.backend` is `off`

Before enabling `mem0-local`, confirm the profile's `memory.backend` is set
to its documented default of `off`. This matters because OMP has its own
built-in memory/retain-and-inject path; leaving it enabled alongside
`mem0-local` would create two independent memory paths writing and
injecting context in parallel. Verifying the effective value inside a real
OMP session is pending installation.

## 3. Enumerate discovered servers and disable duplicates by name

The discovery, denylist, and profile-independence behavior described in
this section is per OMP's current documentation; it is not yet exercised
against a live install, and remains pending installation like the rest of
this runbook.

OMP discovers MCP servers from multiple sources, including Claude Code's
and Codex's own configs, not only from OMP-native `mcp.json` files. Before
enabling `mem0-local`, enumerate everything OMP currently sees:

```text
/mcp list
```

OMP's `disabledServers` denylist matches by **server name**, not by source
or transport. If `/mcp list` surfaces a hosted Mem0 MCP server discovered
from a Claude or Codex config (for example an entry translated from
`~/.claude/mcp.json` or `~/.codex/config.toml`), record its exact
discovered name and add that name to the profile's `disabledServers` array
in its user `mcp.json`. Do this for every duplicate discovered name before
treating `mem0-local` as the sole active Mem0 server — a denylist entry
for the wrong name silently disables nothing.

Also confirm no project-level `.omp/mcp.json` or `.mcp.json` exists in this
working repository (`mem0_0`). Project-level MCP config and external-tool
configs (`.claude/`, `.cursor/`, etc.) are profile-independent — they apply
under every profile regardless of which one is active — so a stray
project-level file would reintroduce a second Mem0 path that the
profile-scoped `disabledServers` denylist cannot suppress. This is the
reason the check is repo-level rather than profile-level.

## 4. Validation gate

The following ten steps are installation-dependent; none has been run yet.
Each is pending installation until performed against a live OMP process
connected to the running bridge:

1. Record the installed OMP version and the config hash (or content digest)
   of the profile's active `mcp.json` before any live call is made.
2. Confirm exactly one enabled `mem0-local` server is visible to the
   profile after step 3's denylist pass — no duplicate, no second Mem0
   server left enabled.
3. Run an MCP connection test and confirm the server exposes exactly the
   six expected tools: `add_memory`, `search_memories`, `get_memories`,
   `get_memory`, `update_memory`, `delete_memory`.
4. Run a synthetic create/read/update/delete cycle that supplies all five
   explicit scope fields (`user_id`, `agent_id`, `machine_id`,
   `project_id`, `data_class`) on every call, per `policy-fragment.md`.
5. Prove fail-closed behavior per operation class: a write containing
   secret-shaped content is rejected, a call missing a required scope
   field is rejected, and an unconfirmed `delete_memory` (no
   `confirm=true`) is rejected.
6. Prove no cross-scope recall: a `search_memories` call scoped to one
   `project_id`/`agent_id` combination does not return memories written
   under a different one.
7. Prove `search_memories`' `max_chars` argument (default 16384) is
   respected — a query with a lower `max_chars` returns a bounded result
   set, and any content dropped to stay under budget is disclosed via the
   response's `omitted_for_budget` field rather than silently truncated.
8. Confirm fresh-session policy adherence: a newly started OMP session
   under this profile searches before answering questions that stored
   context could change, per `policy-fragment.md`, without manual
   prompting.
9. Confirm durability across compaction, shutdown, and restart: a memory
   written in one session is still retrievable by ID and by scoped search
   after context compaction and after a full OMP process restart.
10. Delete every synthetic memory created during steps 4-9 by ID with
    `confirm=true`, and reconcile the deletions against a receipt (IDs
    created vs. IDs deleted) so no synthetic validation data is left in
    the store.

## 5. Rollback and removal

To remove this integration entirely:

1. Delete the profile directory: `~/.omp/profiles/mem0-validation/`.
2. Remove any `disabledServers` entries added in step 3 from whichever
   config file they were added to, if that file is reused outside this
   profile.
3. Confirm nothing outside the profile directory and those denylist
   entries was changed — no edits to `~/.claude/`, `~/.codex/`, shell
   profiles, or any file outside this profile's own directory tree.

Rollback has not been exercised against a real OMP installation; treat it
as pending installation like the rest of this runbook.

## Status

OMP integration prepared; transport and harness behavior pending installation.
