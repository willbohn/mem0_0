# mem0-local policy fragment (OMP profile)

Behavioral policy for the `mem0-local` MCP server inside a dedicated OMP
profile. This fragment is content-free: it describes *when* and *how* to
call the tools, not what to write into them. Pair it with `runbook.md` for
the profile lifecycle and `mcp.json.example` for the transport config.

## When to search, when to write

Call `search_memories` before answering a question whose answer could be
changed by stored context — a prior decision, a stated preference, a
constraint, or a lesson from earlier work in this project. Do not search on
every turn; search when the current question plausibly has a remembered
answer.

Call `add_memory` only for durable content: user preferences, project
decisions, constraints, and reusable lessons. Do not write transient facts,
one-off task status, or anything that is fully derivable from the repository
itself (code structure, file contents, commit history) — the memory store
supplements the codebase, it does not duplicate it.

## Scope fields on every call

Every call to every tool carries five explicit scope fields. None of them
are inferred from the working directory, environment, or Git metadata —
they are constants an operator chooses once per deployment and per project:

- `user_id=will`
- `agent_id=omp`
- `machine_id=arch-local`
- `project_id=mem0_0` for this repository

`project_id` in particular is always an operator-chosen constant for a
given repository, never derived from the current working directory or from
Git remote/branch metadata — two clones of the same repository, or a
renamed directory, must still resolve to the same `project_id`.

The fifth field, `data_class`, is chosen per call from the bridge's
supported classes: `user-preference`, `project-decision`, `task-learning`,
or `assistant-memory`. Pick the narrowest class that describes the memory;
it is what later scoped reads and deletions filter on.

## Secret exclusion

Never send credentials, API keys, access or refresh tokens, session
cookies, passwords, private keys, recovery codes, or credential-bearing
connection strings to any tool call — not in the memory content, not in
metadata. The bridge's own Mem0 API key is never one of these values
either: it is read only from the file path in `MEM0_API_KEY_FILE`
(`mcp.json.example`'s `env.MEM0_API_KEY_FILE`), never accepted inline in
harness configuration. This is a policy requirement on the caller, and it
is also enforced fail-closed by the bridge itself: `bridge.py` pattern-matches
outgoing content for these shapes and rejects the call rather than
forwarding a possible secret. Treat the bridge's rejection as a signal to
redact and retry, not as an error to work around.

## Deletion requires confirmation

`delete_memory` requires the caller to pass `confirm=true`, and that flag
must only be set after the user has explicitly confirmed the deletion in
the current conversation. An unconfirmed or inferred deletion is not
permitted — when in doubt, surface the candidate memory and ask before
deleting it.

## Write mode: `infer=false` by default

`infer=false` is the default write mode for `add_memory` and is the
expected mode for nearly all writes: it stores the content as given,
without an extraction pass, and returns quickly.

`infer=true` triggers local-model extraction and should be reserved for
durable state, preference, relationship, instruction, or procedure content
where the extraction step earns its cost — and only once the local
inference provider is confirmed ready. Measured local `infer=true` write
latency is roughly 70-110 seconds; treat that latency as expected, not as
a hang, but weigh it against the call being made synchronously inside an
interactive session before choosing `infer=true` over the default.

## Corrections update, they do not append

When new information corrects an existing memory, call `update_memory`
against that memory's known, previously-scoped ID. An unlinked corrective
write via `add_memory` is not automatic supersession — the old and new
memories would both remain retrievable, and a later `search_memories` call
could surface either one. Only `update_memory` against the specific ID
guarantees the correction replaces rather than coexists with the original.
