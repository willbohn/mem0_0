import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from deployment.mem0_0_arch import backup_restore
from deployment.mem0_0_arch.backup_restore import (
    BackupVerificationError,
    compare_inventories,
    create_encrypted_backup,
    postgres_inventory,
    reconcile_shadow,
    restore_shadow,
    verify_backup_receipt,
    verify_legacy_sidecar,
)
from deployment.mem0_0_arch.migration import MigrationConflictError, RuntimePaths, sqlite_inventory


class FakeRunner:
    def __init__(self, *outputs: bytes):
        self.outputs = list(outputs)
        self.commands = []

    def run(self, command, **kwargs):
        self.commands.append(tuple(command))
        return self.outputs.pop(0)


class BackupRunner:
    def __init__(self, *, fail_pipeline=False, fail_unpause=False):
        self.events = []
        self.fail_pipeline = fail_pipeline
        self.fail_unpause = fail_unpause

    def run(self, command, **kwargs):
        command = tuple(command)
        self.events.append(command)
        if command[:2] == ("age-keygen", "-y"):
            return b"age1synthetic-recipient\n"
        if command[:2] == ("podman", "unpause") and self.fail_unpause:
            raise BackupVerificationError("synthetic unpause failure")
        if command[:2] in {("podman", "pause"), ("podman", "unpause")}:
            return b""
        if command[0] == "age" and "-o" in command:
            Path(command[command.index("-o") + 1]).write_bytes(b"encrypted-sqlite")
            return b""
        raise AssertionError(f"unexpected command: {command}")

    def pipeline(self, commands):
        frozen = tuple(tuple(command) for command in commands)
        self.events.append(("pipeline", frozen))
        if self.fail_pipeline:
            raise BackupVerificationError("synthetic pipeline failure")
        output_command = frozen[1]
        Path(output_command[output_command.index("-o") + 1]).write_bytes(b"encrypted-postgres")


def _create_history_database(path):
    path.parent.mkdir(parents=True)
    database = sqlite3.connect(path)
    database.execute(
        "CREATE TABLE history (id TEXT, memory_id TEXT, old_memory TEXT, new_memory TEXT, event TEXT, "
        "created_at TEXT, updated_at TEXT, is_deleted INTEGER, actor_id TEXT, role TEXT)"
    )
    database.execute(
        "CREATE TABLE messages (id INTEGER, session_scope TEXT, role TEXT, content TEXT, name TEXT, created_at TEXT)"
    )
    database.execute(
        "INSERT INTO history VALUES ('event-1', 'memory-1', NULL, 'private-value', 'ADD', NULL, NULL, 0, NULL, NULL)"
    )
    database.commit()
    database.close()


def _backup_paths(tmp_path):
    paths = RuntimePaths(
        legacy_state=tmp_path / "legacy-state",
        legacy_config=tmp_path / "legacy-config",
        target_state=tmp_path / "target-state",
        target_config=tmp_path / "target-config",
    )
    _create_history_database(paths.legacy_state / "history" / "history.db")
    (paths.target_state / "backups").mkdir(parents=True)
    paths.target_config.mkdir(parents=True)
    key = paths.target_config / "backup.agekey"
    key.write_text("synthetic-age-key", encoding="utf-8")
    os.chmod(key, 0o600)
    return paths


def _patch_backup_sources(monkeypatch, postgres_inventories=None):
    postgres_inventories = iter(postgres_inventories or [{"rows": 2, "content_sha256": "a" * 64}])
    last_postgres = None

    def postgres(*_args, **_kwargs):
        nonlocal last_postgres
        try:
            last_postgres = next(postgres_inventories)
        except StopIteration:
            assert last_postgres is not None
        return dict(last_postgres)

    monkeypatch.setattr(
        "deployment.mem0_0_arch.backup_restore.source_attestation",
        lambda _repository: {
            "revision": "b" * 40,
            "source_tree_sha256": "c" * 64,
        },
    )
    monkeypatch.setattr("deployment.mem0_0_arch.backup_restore.postgres_inventory", postgres)


def test_quiesced_backup_emits_verified_content_free_receipt_after_unpause(tmp_path, monkeypatch):
    paths = _backup_paths(tmp_path)
    runner = BackupRunner()
    _patch_backup_sources(monkeypatch)

    receipt_path = create_encrypted_backup(
        paths,
        legacy_postgres_container="mem0-arch-postgres-1",
        legacy_writer_container="mem0-arch-mem0-1",
        source_repository=tmp_path / "source",
        runner=runner,
        timestamp="20260806T000000Z",
    )

    receipt = verify_backup_receipt(receipt_path)
    assert receipt["cross_store_consistency"] == "verified"
    assert receipt["source"] == receipt["quiescence"]["pre_backup"]
    assert receipt["quiescence"]["pre_backup"] == receipt["quiescence"]["post_backup"]
    assert receipt["quiescence"] == {
        "method": "podman_pause",
        "writer_container": "mem0-arch-mem0-1",
        "pause_succeeded": True,
        "unpause_succeeded": True,
        "pre_backup": receipt["source"],
        "post_backup": receipt["source"],
    }
    assert runner.events[1] == ("podman", "pause", "mem0-arch-mem0-1")
    assert runner.events[-1] == ("podman", "unpause", "mem0-arch-mem0-1")
    assert "private-value" not in json.dumps(receipt)


def test_verified_receipt_rejects_unbounded_quiescence_fields(tmp_path, monkeypatch):
    paths = _backup_paths(tmp_path)
    runner = BackupRunner()
    _patch_backup_sources(monkeypatch)
    receipt_path = create_encrypted_backup(
        paths,
        legacy_postgres_container="mem0-arch-postgres-1",
        legacy_writer_container="mem0-arch-mem0-1",
        source_repository=tmp_path / "source",
        runner=runner,
        timestamp="20260806T000001Z",
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["quiescence"]["writer_output"] = "unbounded-runtime-output"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(BackupVerificationError, match="quiescence evidence"):
        verify_backup_receipt(receipt_path)


def test_backup_cli_forwards_explicit_legacy_writer_target(tmp_path, monkeypatch, capsys):
    captured = {}

    def fake_backup(paths, **kwargs):
        captured["paths"] = paths
        captured.update(kwargs)
        return tmp_path / "receipt.json"

    monkeypatch.setattr(backup_restore, "create_encrypted_backup", fake_backup)

    assert (
        backup_restore.main(
            [
                "backup",
                "--home",
                str(tmp_path),
                "--legacy-container",
                "mem0-arch-postgres-1",
                "--quiesce-legacy-writer",
                "mem0-arch-mem0-1",
                "--source-repository",
                str(tmp_path / "source"),
            ]
        )
        == 0
    )
    assert captured["legacy_writer_container"] == "mem0-arch-mem0-1"
    assert json.loads(capsys.readouterr().out)["status"] == "verified"


def test_default_backup_remains_not_proven_and_never_pauses_writer(tmp_path, monkeypatch):
    paths = _backup_paths(tmp_path)
    runner = BackupRunner()
    _patch_backup_sources(monkeypatch)

    receipt_path = create_encrypted_backup(
        paths,
        legacy_postgres_container="mem0-arch-postgres-1",
        source_repository=tmp_path / "source",
        runner=runner,
        timestamp="20260806T000002Z",
    )

    receipt = verify_backup_receipt(receipt_path)
    assert receipt["cross_store_consistency"] == "not_proven"
    assert "quiescence" not in receipt
    assert not any(event[:2] in {("podman", "pause"), ("podman", "unpause")} for event in runner.events)


def test_reconcile_shadow_proves_existing_stores_match_a_quiesced_receipt(tmp_path, monkeypatch):
    paths = _backup_paths(tmp_path)
    runner = BackupRunner()
    _patch_backup_sources(monkeypatch)
    receipt_path = create_encrypted_backup(
        paths,
        legacy_postgres_container="mem0-arch-postgres-1",
        legacy_writer_container="mem0-arch-mem0-1",
        source_repository=tmp_path / "source",
        runner=runner,
        timestamp="20260806T000003Z",
    )
    target_history = paths.target_state / "runtime" / ".mem0_0" / "history.db"
    target_history.parent.mkdir(parents=True)
    shutil.copyfile(paths.legacy_state / "history" / "history.db", target_history)
    monkeypatch.setattr(
        "deployment.mem0_0_arch.backup_restore.postgres_inventory",
        lambda *_args, **_kwargs: {"rows": 2, "content_sha256": "a" * 64},
    )

    report = reconcile_shadow(
        paths,
        receipt_path,
        shadow_postgres_container="mem0_0-arch-shadow-postgres-1",
        runner=runner,
    )

    assert report == {
        "schema_version": 1,
        "status": "reconciled",
        "cross_store_consistency": "verified",
        "postgres": {"rows": 2, "content_sha256": "a" * 64},
        "sqlite": sqlite_inventory(target_history),
        "legacy_state_retained": True,
    }


def test_quiesced_backup_unpauses_and_removes_artifacts_on_inventory_drift(tmp_path, monkeypatch):
    paths = _backup_paths(tmp_path)
    runner = BackupRunner()
    _patch_backup_sources(
        monkeypatch,
        [
            {"rows": 2, "content_sha256": "a" * 64},
            {"rows": 3, "content_sha256": "d" * 64},
        ],
    )

    with pytest.raises(BackupVerificationError, match="PostgreSQL inventory mismatch"):
        create_encrypted_backup(
            paths,
            legacy_postgres_container="mem0-arch-postgres-1",
            legacy_writer_container="mem0-arch-mem0-1",
            source_repository=tmp_path / "source",
            runner=runner,
            timestamp="20260806T000003Z",
        )

    assert runner.events[-1] == ("podman", "unpause", "mem0-arch-mem0-1")
    assert list((paths.target_state / "backups").iterdir()) == []


def test_quiesced_backup_rejects_sqlite_drift_while_writer_is_paused(tmp_path, monkeypatch):
    paths = _backup_paths(tmp_path)

    class SqliteDriftRunner(BackupRunner):
        def run(self, command, **kwargs):
            result = super().run(command, **kwargs)
            if command[0] == "age" and "-o" in command:
                database = sqlite3.connect(paths.legacy_state / "history" / "history.db")
                database.execute("UPDATE history SET new_memory = 'changed-while-paused'")
                database.commit()
                database.close()
            return result

    runner = SqliteDriftRunner()
    _patch_backup_sources(monkeypatch)

    with pytest.raises(BackupVerificationError, match="SQLite inventory mismatch"):
        create_encrypted_backup(
            paths,
            legacy_postgres_container="mem0-arch-postgres-1",
            legacy_writer_container="mem0-arch-mem0-1",
            source_repository=tmp_path / "source",
            runner=runner,
            timestamp="20260806T000003Z",
        )

    assert runner.events[-1] == ("podman", "unpause", "mem0-arch-mem0-1")
    assert list((paths.target_state / "backups").iterdir()) == []


def test_quiesced_backup_unpauses_when_backup_pipeline_fails(tmp_path, monkeypatch):
    paths = _backup_paths(tmp_path)
    runner = BackupRunner(fail_pipeline=True)
    _patch_backup_sources(monkeypatch)

    with pytest.raises(BackupVerificationError, match="synthetic pipeline failure"):
        create_encrypted_backup(
            paths,
            legacy_postgres_container="mem0-arch-postgres-1",
            legacy_writer_container="mem0-arch-mem0-1",
            source_repository=tmp_path / "source",
            runner=runner,
            timestamp="20260806T000004Z",
        )

    assert runner.events[-1] == ("podman", "unpause", "mem0-arch-mem0-1")
    assert list((paths.target_state / "backups").iterdir()) == []


def test_unpause_failure_removes_new_artifacts_and_never_writes_receipt(tmp_path, monkeypatch):
    paths = _backup_paths(tmp_path)
    runner = BackupRunner(fail_unpause=True)
    _patch_backup_sources(monkeypatch)

    with pytest.raises(BackupVerificationError, match="synthetic unpause failure"):
        create_encrypted_backup(
            paths,
            legacy_postgres_container="mem0-arch-postgres-1",
            legacy_writer_container="mem0-arch-mem0-1",
            source_repository=tmp_path / "source",
            runner=runner,
            timestamp="20260806T000005Z",
        )

    assert runner.events[-1] == ("podman", "unpause", "mem0-arch-mem0-1")
    assert list((paths.target_state / "backups").iterdir()) == []


def test_receipt_write_failure_after_unpause_removes_all_new_artifacts(tmp_path, monkeypatch):
    paths = _backup_paths(tmp_path)
    runner = BackupRunner()
    _patch_backup_sources(monkeypatch)

    def fail_receipt_write(_path, _receipt):
        raise BackupVerificationError("synthetic receipt write failure")

    monkeypatch.setattr("deployment.mem0_0_arch.backup_restore._atomic_json", fail_receipt_write)

    with pytest.raises(BackupVerificationError, match="synthetic receipt write failure"):
        create_encrypted_backup(
            paths,
            legacy_postgres_container="mem0-arch-postgres-1",
            legacy_writer_container="mem0-arch-mem0-1",
            source_repository=tmp_path / "source",
            runner=runner,
            timestamp="20260806T000006Z",
        )

    assert runner.events[-1] == ("podman", "unpause", "mem0-arch-mem0-1")
    assert list((paths.target_state / "backups").iterdir()) == []


def test_quiescence_rejects_any_writer_name_except_exact_legacy_app(tmp_path, monkeypatch):
    paths = _backup_paths(tmp_path)
    runner = BackupRunner()
    _patch_backup_sources(monkeypatch)

    with pytest.raises(MigrationConflictError, match="exact legacy app writer"):
        create_encrypted_backup(
            paths,
            legacy_postgres_container="mem0-arch-postgres-1",
            legacy_writer_container="another-container",
            source_repository=tmp_path / "source",
            runner=runner,
            timestamp="20260806T000007Z",
        )

    assert runner.events == []
    assert list((paths.target_state / "backups").iterdir()) == []


def test_postgres_inventory_hashes_full_rows_without_returning_them():
    runner = FakeRunner(b"1\n", b'{"id":"id-a","memory":"private-a"}\n{"id":"id-b","memory":"private-b"}\n')

    inventory = postgres_inventory(runner, "legacy-postgres", 15432)

    assert inventory == {
        "rows": 2,
        "content_sha256": hashlib.sha256(
            b'{"id":"id-a","memory":"private-a"}\n{"id":"id-b","memory":"private-b"}\n'
        ).hexdigest(),
    }
    assert "id-a" not in json.dumps(inventory)
    assert runner.commands[0][0:3] == ("podman", "exec", "legacy-postgres")


def test_postgres_inventory_treats_a_fresh_database_without_memory_table_as_empty():
    runner = FakeRunner(b"0\n")

    inventory = postgres_inventory(runner, "shadow-postgres", 25432)

    assert inventory == {"rows": 0, "content_sha256": hashlib.sha256(b"").hexdigest()}
    assert len(runner.commands) == 1


def test_compare_inventories_fails_on_any_count_or_digest_drift():
    expected = {"rows": 2, "content_sha256": "a" * 64}

    assert compare_inventories(expected, dict(expected), label="postgres") is True
    with pytest.raises(BackupVerificationError, match="postgres inventory mismatch"):
        compare_inventories(expected, {"rows": 1, "content_sha256": "a" * 64}, label="postgres")


def test_backup_receipt_verification_detects_ciphertext_changes(tmp_path):
    postgres = tmp_path / "postgres.dump.age"
    sqlite = tmp_path / "history.sqlite.age"
    postgres.write_bytes(b"encrypted-postgres")
    sqlite.write_bytes(b"encrypted-sqlite")
    receipt = {
        "schema_version": 1,
        "source_revision": "a" * 40,
        "source_tree_sha256": "b" * 64,
        "cross_store_consistency": "not_proven",
        "artifacts": {
            "postgres": {"path": postgres.name, "sha256": hashlib.sha256(postgres.read_bytes()).hexdigest()},
            "sqlite": {"path": sqlite.name, "sha256": hashlib.sha256(sqlite.read_bytes()).hexdigest()},
        },
        "source": {
            "postgres": {"rows": 0, "content_sha256": hashlib.sha256(b"").hexdigest()},
            "sqlite": {
                "schema_version": 1,
                "tables": {
                    "history": {"rows": 0, "content_sha256": hashlib.sha256(b"").hexdigest()},
                    "messages": {"rows": 0, "content_sha256": hashlib.sha256(b"").hexdigest()},
                },
            },
        },
    }
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    assert verify_backup_receipt(receipt_path)["schema_version"] == 1
    postgres.write_bytes(b"changed")
    with pytest.raises(BackupVerificationError, match="SHA-256"):
        verify_backup_receipt(receipt_path)


def test_backup_receipt_requires_exact_source_attestation(tmp_path):
    receipt = tmp_path / "receipt.json"
    receipt.write_text('{"schema_version": 1}', encoding="utf-8")

    with pytest.raises(BackupVerificationError, match="source attestation"):
        verify_backup_receipt(receipt)


def test_legacy_sidecar_is_accepted_only_for_the_exact_unchanged_ciphertext(tmp_path):
    artifact = tmp_path / "legacy.dump.age"
    artifact.write_bytes(b"legacy-encrypted-dump")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    sidecar = tmp_path / "legacy.dump.age.sha256"
    sidecar.write_text(f"{digest}  {artifact}\n", encoding="utf-8")

    assert verify_legacy_sidecar(artifact, sidecar) == digest
    sidecar.write_text(f"{digest}  {tmp_path / 'different.dump.age'}\n", encoding="utf-8")
    with pytest.raises(BackupVerificationError, match="does not name"):
        verify_legacy_sidecar(artifact, sidecar)


def test_failed_postgres_reconciliation_never_commits_shadow_history(tmp_path, monkeypatch):
    paths = RuntimePaths(
        legacy_state=tmp_path / "legacy-state",
        legacy_config=tmp_path / "legacy-config",
        target_state=tmp_path / "target-state",
        target_config=tmp_path / "target-config",
    )
    target_runtime = paths.target_state / "runtime" / ".mem0_0"
    target_runtime.mkdir(parents=True)
    paths.target_config.mkdir()
    key = paths.target_config / "backup.agekey"
    key.write_text("synthetic-key", encoding="utf-8")
    os.chmod(key, 0o600)
    source_history = tmp_path / "source.sqlite"
    database = sqlite3.connect(source_history)
    database.execute(
        "CREATE TABLE history (id TEXT, memory_id TEXT, old_memory TEXT, new_memory TEXT, event TEXT, "
        "created_at TEXT, updated_at TEXT, is_deleted INTEGER, actor_id TEXT, role TEXT)"
    )
    database.execute(
        "CREATE TABLE messages (id INTEGER, session_scope TEXT, role TEXT, content TEXT, name TEXT, created_at TEXT)"
    )
    database.commit()
    database.close()
    postgres_artifact = tmp_path / "postgres.age"
    sqlite_artifact = tmp_path / "sqlite.age"
    postgres_artifact.write_bytes(b"encrypted-postgres")
    sqlite_artifact.write_bytes(b"encrypted-sqlite")
    expected_postgres = {"rows": 1, "content_sha256": "a" * 64}
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_revision": "a" * 40,
                "source_tree_sha256": "b" * 64,
                "cross_store_consistency": "not_proven",
                "source": {"postgres": expected_postgres, "sqlite": sqlite_inventory(source_history)},
                "artifacts": {
                    "postgres": {
                        "path": postgres_artifact.name,
                        "sha256": hashlib.sha256(postgres_artifact.read_bytes()).hexdigest(),
                    },
                    "sqlite": {
                        "path": sqlite_artifact.name,
                        "sha256": hashlib.sha256(sqlite_artifact.read_bytes()).hexdigest(),
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    inventories = iter(
        [
            {"rows": 0, "content_sha256": hashlib.sha256(b"").hexdigest()},
            {"rows": 1, "content_sha256": "c" * 64},
        ]
    )
    monkeypatch.setattr("deployment.mem0_0_arch.backup_restore.postgres_inventory", lambda *args: next(inventories))
    monkeypatch.setattr(
        "deployment.mem0_0_arch.backup_restore._restore_postgres_pipeline", lambda *args, **kwargs: None
    )

    class RestoreRunner:
        def run(self, command, **kwargs):
            shutil.copyfile(source_history, command[command.index("-o") + 1])
            return b""

    with pytest.raises(BackupVerificationError, match="PostgreSQL inventory mismatch"):
        restore_shadow(
            paths,
            receipt,
            shadow_postgres_container="mem0_0-arch-shadow-postgres-1",
            runner=RestoreRunner(),
        )

    assert not (target_runtime / "history.db").exists()
