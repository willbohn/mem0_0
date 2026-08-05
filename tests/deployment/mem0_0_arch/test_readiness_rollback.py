import hashlib
import json
from pathlib import Path

import pytest

from deployment.mem0_0_arch.readiness import readiness_report
from deployment.mem0_0_arch.rollback import RollbackError, execute_rollback, rollback_plan


class FakeRunner:
    def __init__(self, outputs=None):
        self.outputs = list(outputs or [])
        self.commands = []

    def run(self, command, **kwargs):
        self.commands.append(tuple(command))
        return self.outputs.pop(0) if self.outputs else b""


def test_readiness_report_contains_status_only_and_checks_private_paths(tmp_path):
    config = tmp_path / "shadow.env"
    history = tmp_path / "history.db"
    config.write_text("POSTGRES_PASSWORD=private-value\n", encoding="utf-8")
    history.write_bytes(b"sqlite")
    config.chmod(0o600)
    history.chmod(0o600)
    runner = FakeRunner([b"accepting connections\n"])

    report = readiness_report(
        config_path=config,
        history_path=history,
        api_url="http://127.0.0.1:28889",
        runner=runner,
        opener=lambda request, timeout: b'{"openapi":"3.1.0"}',
    )

    assert report == {
        "schema_version": 1,
        "status": "ready",
        "checks": {"api": "ready", "config": "private", "history": "private", "postgres": "ready"},
    }
    assert "private-value" not in json.dumps(report)


def _record(tmp_path: Path) -> Path:
    legacy_state = tmp_path / "legacy-state"
    legacy_state.mkdir()
    receipt = tmp_path / "backup-receipt.json"
    receipt.write_text('{"schema_version":1}', encoding="utf-8")
    record = tmp_path / "cutover.json"
    record.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "legacy_unit": "mem0-arch.service",
                "candidate_unit": "mem0_0-arch.service",
                "legacy_state_retained": True,
                "legacy_state": str(legacy_state),
                "backup_receipt": str(receipt),
                "backup_receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    return record


def test_rollback_requires_exact_record_hash_before_changing_units(tmp_path):
    record = _record(tmp_path)
    runner = FakeRunner()

    with pytest.raises(RollbackError, match="confirmation hash"):
        execute_rollback(record, confirmation="0" * 64, runner=runner)
    assert runner.commands == []


def test_rollback_plan_retains_old_state_and_execution_only_switches_units(tmp_path):
    record = _record(tmp_path)
    plan = rollback_plan(record)
    confirmation = hashlib.sha256(record.read_bytes()).hexdigest()
    runner = FakeRunner([b"", b"", b"active\n"])

    report = execute_rollback(record, confirmation=confirmation, runner=runner)

    assert plan["legacy_state_retained"] is True
    assert all("rm" not in command for command in plan["commands"])
    assert runner.commands == [
        ("systemctl", "--user", "stop", "mem0_0-arch.service"),
        ("systemctl", "--user", "start", "mem0-arch.service"),
        ("systemctl", "--user", "is-active", "mem0-arch.service"),
    ]
    assert report["status"] == "rolled_back"
