from __future__ import annotations

import json
from typing import Any


class CutoverRecordError(RuntimeError):
    pass


REQUIRED_GATES = (
    "benchmark_acceptance",
    "rest_parity",
    "typescript_parity",
    "shadow_end_to_end",
    "fresh_session_behavior",
    "cross_store_snapshot_provenance",
    "live_candidate_reconciliation",
    "legacy_rollback_image_provenance",
    "canonical_state_path_attestation",
)


def cutover_status() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "blocked",
        "cutover_enabled": False,
        "required_gates": list(REQUIRED_GATES),
        "legacy_state_retained": True,
    }


def create_cutover_record(*_args: Any, **_kwargs: Any) -> None:
    raise CutoverRecordError("cutover record creation is disabled until every required gate is implemented and passes")


def main(argv: list[str] | None = None) -> int:
    if argv:
        raise CutoverRecordError("cutover arguments are disabled until every required gate passes")
    print(json.dumps(cutover_status(), indent=2, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
