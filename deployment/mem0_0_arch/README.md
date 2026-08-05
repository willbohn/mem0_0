# Canonical Arch shadow deployment

This directory moves ownership of the existing local deployment from `mem0-implement` into the `mem0_0` repository without overwriting or deleting its data. The first deployment is intentionally a shadow:

| Resource | Legacy | Canonical shadow |
|---|---|---|
| API | `127.0.0.1:8888` | `127.0.0.1:28889` |
| PostgreSQL | `127.0.0.1:15432` | `127.0.0.1:25432` |
| State | `~/.local/share/mem0-arch` | `~/.local/share/mem0_0-arch` |
| Private config | `~/.config/mem0-arch` | `~/.config/mem0_0-arch` |
| PostgreSQL volume | `mem0_arch_postgres_data` | `mem0_0_arch_shadow_postgres_data` |
| Python local state inside the API | legacy history bind | `/app/state/.mem0_0` |

Existing Python callers keep importing `mem0`, packages keep the `mem0ai` distribution name, REST paths and `MEM0_*` variables remain compatible, and MCP tools keep their current names. For direct Python use, an explicit `MEM0_DIR` still wins. A fresh install now chooses `~/.mem0_0`; when only `~/.mem0` exists, the SDK keeps using it until this explicit migration is performed.

The REST process keeps liveness separate from local-model readiness. `GET /health` remains HTTP 200 and includes a credential-safe provider summary; `GET /ready` is HTTP 200 only when the configured OpenAI-compatible endpoint advertises both configured models. Provider-dependent add, search, update, and instruction-generation requests fail fast with a bounded HTTP 503 code while read, audit, consolidation, feedback, and deletion remain available. Set `MEM0_LMSTUDIO_COMPAT=true` for LM Studio's text response-format compatibility and native loaded-model check. In this mode, `MEM0_LMSTUDIO_REASONING_EFFORT` is validated as `none`, `low`, `medium`, or `high` and defaults to `none` so deterministic extraction does not spend its output budget on hidden reasoning. `MEM0_REQUIRE_LOADED_MODELS=false` preserves just-in-time model loading by default.

## Safety contract

- All commands default to planning or content-free inspection.
- Preparation refuses any existing target directory; it never merges state.
- PostgreSQL and SQLite are encrypted separately with `age` before restore.
- Restore requires an empty, distinctly named shadow database and a missing target history DB.
- Reconciliation compares row counts and SHA-256 digests of complete canonical rows. Stored bodies are hashed locally and never printed or written to receipts.
- The old unit, directories, PostgreSQL volume, backups, configs, and keys remain in place.
- The application service is behind the Compose `application` profile, preventing an accidental empty API start before restore.
- Image variables must contain digest-pinned references. Tags alone are not sufficient evidence.
- Automatic hard deletion is absent. The rollback operation only switches systemd units.

Do not copy `.env` values into chat, logs, commits, or migration reports. The tools read private files locally and report key names and aggregate evidence only.

## 1. Inspect and prepare empty shadow roots

The first command is a dry run. It reads permissions, environment key names, and SQLite row counts/content digests, but creates nothing:

```bash
python -m deployment.mem0_0_arch.migration --home /home/will
```

Review the report, then create the new private directories and copy the private runtime/key files without changing their values. Only non-secret shadow endpoints and `MEM0_DIR`/`HISTORY_DB_PATH` are rewritten:

```bash
python -m deployment.mem0_0_arch.migration --home /home/will --apply \
  --report /home/will/.local/share/mem0_0-arch/receipts/prepare-shadow.json
```

Preparation deliberately does not copy or open the target history DB. Database state enters the shadow only through the encrypted backup/restore gate.

## 2. Pin images and candidate source

Copy `deployment.env.example` to `~/.config/mem0_0-arch/deployment.env`, set mode `0600`, and replace every placeholder with the candidate attestation's exact Git revision/source-tree digest or a registry digest. This file contains pins, not credentials; credentials remain in `shadow.env`. The systemd start path re-attests the tree before and after build, verifies the image labels, and starts with `--no-build` only after they match.

```bash
install -m 0600 deployment/mem0_0_arch/deployment.env.example \
  /home/will/.config/mem0_0-arch/deployment.env
```

Do not start the application profile yet. Start only the isolated PostgreSQL target:

```bash
set -a
. /home/will/.config/mem0_0-arch/deployment.env
set +a
python -m deployment.mem0_0_arch.shadow_runtime \
  --postgres-only --compose-file deployment/mem0_0_arch/compose.yaml
```

## 3. Encrypt both databases and restore the shadow

The legacy PostgreSQL writer must be quiesced or otherwise proven stable before the final backup. The backup command streams `pg_dump` directly through `age`, makes a consistent SQLite backup through SQLite's backup API, encrypts it, removes the plaintext temporary file, and writes a mode-`0600` receipt.

First attest the exact legacy source checkout. The report contains only the Git revision, tracked and untracked file counts, and a digest of all non-ignored source paths and contents; it does not print source or secret values. A dirty tree is allowed because that reflects the deployed reality, but its source-tree digest is mandatory in the backup receipt:

```bash
python -m deployment.mem0_0_arch.source_attestation \
  /home/will/Documents/CodingWorkspaces/mem0-implement/vendor/mem0
```

```bash
python -m deployment.mem0_0_arch.backup_restore backup \
  --home /home/will \
  --legacy-container mem0-arch-postgres-1 \
  --source-repository /home/will/Documents/CodingWorkspaces/mem0-implement/vendor/mem0
```

Without an explicit writer-quiescence option, this preserves the existing behavior and records `cross_store_consistency=not_proven`. Use that mode for exploratory snapshots, not final cutover evidence.

For the final matched snapshot, explicitly name the only accepted legacy app writer container:

```bash
python -m deployment.mem0_0_arch.backup_restore backup \
  --home /home/will \
  --legacy-container mem0-arch-postgres-1 \
  --quiesce-legacy-writer mem0-arch-mem0-1 \
  --source-repository /home/will/Documents/CodingWorkspaces/mem0-implement/vendor/mem0
```

This pauses only `mem0-arch-mem0-1`, inventories PostgreSQL and SQLite, backs up both stores, inventories them again while the writer remains paused, and requires the pre/post counts and complete-row digests to match. The tool always attempts to unpause after a successful pause. It writes a `cross_store_consistency=verified` receipt only after unpause succeeds; any backup, reconciliation, or unpause failure removes the new artifacts and emits no receipt. The receipt contains the exact writer name, pause method, success booleans, and content-free pre/post inventories—never stored bodies or command output. Any other writer-container name is rejected before Podman is called.

If the isolated shadow was restored from an earlier snapshot and the verified final snapshot has identical content-free inventories, prove the existing stores match without overwriting them:

```bash
python -m deployment.mem0_0_arch.backup_restore reconcile-shadow \
  /home/will/.local/share/mem0_0-arch/backups/<verified-receipt>.receipt.json \
  --home /home/will
```

Reconciliation is read-only, accepts only a quiesced `cross_store_consistency=verified` receipt, and fails on any PostgreSQL or SQLite count or full-row digest difference.

Restore only into the isolated shadow container. The command rejects a non-empty memory table or an existing target history DB, verifies ciphertext hashes before decryption, and reconciles both stores afterward:

```bash
python -m deployment.mem0_0_arch.backup_restore restore-shadow \
  /home/will/.local/share/mem0_0-arch/backups/<receipt.json> \
  --home /home/will \
  --shadow-container mem0_0-arch-shadow-postgres-1
```

If the old container/volume is unavailable but a legacy `*.dump.age` and its original `*.sha256` sidecar remain, adopt that immutable backup instead. Adoption verifies the legacy ciphertext and exact sidecar path, restores it only to the empty shadow container, inventories the restored IDs, creates a new canonical encrypted SQLite artifact, and emits a canonical receipt covering both stores:

```bash
python -m deployment.mem0_0_arch.backup_restore adopt-legacy-backup \
  /home/will/.local/share/mem0-arch/backups/<legacy.dump.age> \
  /home/will/.local/share/mem0-arch/backups/<legacy.dump.age.sha256> \
  --home /home/will \
  --shadow-container mem0_0-arch-shadow-postgres-1 \
  --source-repository /home/will/Documents/CodingWorkspaces/mem0-implement/vendor/mem0
```

This path exists specifically for the currently retained encrypted legacy dumps. It still requires the canonical `backup.agekey`, never rewrites either legacy artifact, and refuses to run against a nonempty or differently named database target. Because the retained PostgreSQL dump has no matched SQLite snapshot/receipt, the resulting receipt is explicitly marked `legacy_postgres_with_current_sqlite_unverified`. It is suitable only for quarantined shadow investigation and is rejected by the cutover recorder. If adoption fails after PostgreSQL restore begins, discard and recreate only the isolated shadow volume before retrying; never reuse a partially restored target and never remove legacy state.

## 4. Start and verify the application shadow

Install the user unit only after restore. Do not enable it automatically during migration.

```bash
install -m 0644 deployment/mem0_0_arch/systemd/mem0_0-arch.service \
  /home/will/.config/systemd/user/mem0_0-arch.service
systemctl --user daemon-reload
systemctl --user start mem0_0-arch.service
python -m deployment.mem0_0_arch.readiness --home /home/will --wait-seconds 120
```

Readiness checks the private-file modes, isolated PostgreSQL, and the API OpenAPI document. It does not read or print stored memory content. The deeper scoped add/correction/current/historical/audit/consolidation/feedback/delete exercise belongs to the later shadow-verification gate; deployed legacy rows remain read-only during it.

After readiness passes and the local model provider is ready, run that scoped REST gate with the private admin key file:

```bash
python -m deployment.mem0_0_arch.live_verify \
  --api-url http://127.0.0.1:28889 \
  --api-key-file /home/will/.config/mem0_0-arch/bridge-api-key
```

The verifier prints only aggregate counts, booleans, and timings. It creates a unique five-field synthetic scope, never calls `delete_all`, and attempts exact-ID cleanup of only records rediscovered inside that scope even when an earlier check fails.

## 5. Bridge compatibility

`bridge/` is repository-owned and uses the canonical `~/.config/mem0_0-arch/bridge-api-key` default. It preserves the existing `mem0-local` MCP contract and tool names, all five explicit scope fields, file-only authentication, secret rejection, and REST envelopes. Point a test harness at `http://127.0.0.1:28889` only after shadow readiness. Retargeting installed harnesses is a later cutover action and must first back up every config.

## 6. Cutover remains hard-blocked

This phase deliberately cannot create a cutover record. The command exits nonzero and returns the machine-readable list of still-required gates, including benchmark acceptance, REST and TypeScript parity, end-to-end shadow/fresh-session behavior, immutable cross-store snapshot provenance, reconciliation of the live candidate against that snapshot, rollback-image provenance, and canonical state-path attestation:

```bash
python -m deployment.mem0_0_arch.cutover
```

The rollback implementation can be exercised only with synthetic records until the final rollout phase implements a trustworthy record creator. Once that later gate exists, preview rollback with its immutable record:

```bash
python -m deployment.mem0_0_arch.rollback <cutover.json>
```

Execution requires the exact SHA-256 printed by the preview. It stops `mem0_0-arch.service`, starts the retained `mem0-arch.service`, and verifies the old unit is active. It never removes a container volume or state directory:

```bash
python -m deployment.mem0_0_arch.rollback <cutover.json> \
  --execute --confirm-record-sha256 <exact-preview-hash>
```

Production-port cutover, harness retargeting, disabling (but retaining) the old unit, and the rollback drill remain blocked until every earlier rollout gate passes.
