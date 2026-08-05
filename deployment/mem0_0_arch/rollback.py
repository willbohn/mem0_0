from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Protocol, Sequence

from deployment.mem0_0_arch.backup_restore import CommandRunner


class RollbackError(RuntimeError):
    pass


class Runner(Protocol):
    def run(self, command: Sequence[str], **kwargs: Any) -> bytes: ...


def _load_record(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RollbackError(f"cannot load cutover record: {path}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("legacy_state_retained") is not True
    ):
        raise RollbackError("cutover record does not prove retained legacy state")
    for key in ("legacy_unit", "candidate_unit", "backup_receipt_sha256"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise RollbackError(f"cutover record is missing {key}")
    legacy_state = value.get("legacy_state")
    backup_receipt = value.get("backup_receipt")
    if not isinstance(legacy_state, str) or not Path(legacy_state).is_dir():
        raise RollbackError("retained legacy state is unavailable")
    if not isinstance(backup_receipt, str) or not Path(backup_receipt).is_file():
        raise RollbackError("verified backup receipt is unavailable")
    if hashlib.sha256(Path(backup_receipt).read_bytes()).hexdigest() != value["backup_receipt_sha256"]:
        raise RollbackError("backup receipt changed after cutover was recorded")
    return value


def rollback_plan(record_path: Path) -> dict[str, Any]:
    record = _load_record(record_path)
    return {
        "schema_version": 1,
        "confirmation_sha256": hashlib.sha256(record_path.read_bytes()).hexdigest(),
        "legacy_state_retained": True,
        "commands": [
            ["systemctl", "--user", "stop", record["candidate_unit"]],
            ["systemctl", "--user", "start", record["legacy_unit"]],
            ["systemctl", "--user", "is-active", record["legacy_unit"]],
        ],
    }


def execute_rollback(record_path: Path, *, confirmation: str, runner: Runner | None = None) -> dict[str, Any]:
    plan = rollback_plan(record_path)
    if confirmation != plan["confirmation_sha256"]:
        raise RollbackError("confirmation hash does not match the cutover record")
    runner = runner or CommandRunner()
    for command in plan["commands"]:
        runner.run(command)
    return {
        "schema_version": 1,
        "status": "rolled_back",
        "legacy_state_retained": True,
        "legacy_unit": _load_record(record_path)["legacy_unit"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or explicitly confirm a mem0_0 service-pointer rollback")
    parser.add_argument("record", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-record-sha256")
    args = parser.parse_args(argv)
    if args.execute:
        if not args.confirm_record_sha256:
            raise RollbackError("--execute requires --confirm-record-sha256")
        report = execute_rollback(args.record, confirmation=args.confirm_record_sha256)
    else:
        report = rollback_plan(args.record)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
