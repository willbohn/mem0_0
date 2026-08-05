# mem0_0-arch 0.1.0

This directory contains the canonical local stdio MCP bridge for the authenticated
Mem0 REST API in the `mem0_0-arch` deployment. It uses only Python's standard
library.

Every memory operation requires five explicit scope fields:

- `user_id`
- `agent_id`
- `machine_id`
- `project_id`
- `data_class`

The bridge rejects wildcard scopes, credential-like payloads, oversized requests,
and unconfirmed deletion. Deletion is available only for one explicit memory ID.
`add_memory` defaults `infer` to `false`; callers must opt into locally configured
LLM extraction explicitly. Results are filtered and checked against the requested
scope before being returned.

The API key is read only from `MEM0_API_KEY_FILE`, which defaults to
`/home/will/.config/mem0_0-arch/bridge-api-key` and must have mode `0600`.
`MEM0_API_KEY` is rejected. Harness configuration should contain only the bridge
command and key-file path, never a key value.

Self-test:

```text
python3 deployment/mem0_0_arch/bridge/bridge.py --self-test
```

The self-test distinguishes MCP/API availability from provider readiness. If a
configured provider is unavailable, it reports `status=degraded` with a bounded
provider code rather than fabricating memories.

MCP stdio command:

```text
python3 deployment/mem0_0_arch/bridge/bridge.py
```

Diagnostics go to stderr; stdout is reserved for newline-delimited JSON-RPC.
