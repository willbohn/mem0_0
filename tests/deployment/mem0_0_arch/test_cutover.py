import pytest

from deployment.mem0_0_arch.cutover import CutoverRecordError, create_cutover_record, cutover_status


def test_cutover_is_hard_blocked_until_all_evidence_gates_exist(tmp_path):
    status = cutover_status()

    assert status["status"] == "blocked"
    assert status["cutover_enabled"] is False
    assert "cross_store_snapshot_provenance" in status["required_gates"]
    assert "live_candidate_reconciliation" in status["required_gates"]
    assert "legacy_rollback_image_provenance" in status["required_gates"]

    output = tmp_path / "cutover.json"
    with pytest.raises(CutoverRecordError, match="disabled"):
        create_cutover_record(output=output)
    assert not output.exists()
