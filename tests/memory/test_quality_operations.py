from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mem0.exceptions import MemoryPersistenceError
from mem0.memory.main import AsyncMemory, Memory


def row(memory_id, text, **payload):
    return SimpleNamespace(id=memory_id, payload={"data": text, "hash": f"hash-{memory_id}", **payload})


def make_memory(memory_class=Memory):
    memory = memory_class.__new__(memory_class)
    memory.vector_store = MagicMock()
    return memory


def test_audit_reports_quality_issues_without_memory_content():
    memory = make_memory()
    memory.vector_store.list.return_value = (
        [
            row("old", "Lives in Osaka", _mem0_status="active", _mem0_schema_version=1),
            row(
                "current",
                "Lives in Tokyo",
                _mem0_status="active",
                _mem0_schema_version=1,
                _mem0_supersedes=["old"],
                _mem0_source_hashes=["source"],
            ),
            row("legacy", "Legacy ungrounded"),
            row("orphan", "Orphan", _mem0_supersedes=["missing"], _mem0_schema_version=1),
        ],
        None,
    )

    report = memory.audit({"user_id": "u1"})

    assert report["scanned"] == 4
    assert report["issues"]["stale_active_predecessors"] == ["old"]
    assert report["issues"]["ungrounded_legacy"] == ["legacy"]
    assert report["issues"]["orphaned_links"] == ["orphan"]
    assert "Lives in" not in repr(report)


def test_consolidate_is_dry_run_by_default_and_apply_is_reversible():
    memory = make_memory()
    duplicate_a = row(
        "a",
        "Same fact",
        created_at="2026-01-01T00:00:00Z",
        _mem0_schema_version=1,
        _mem0_kind="state",
        _mem0_state_key="user::fact",
        _mem0_value="same",
    )
    duplicate_b = row(
        "b",
        "Same fact",
        created_at="2026-02-01T00:00:00Z",
        _mem0_schema_version=1,
        _mem0_kind="state",
        _mem0_state_key="user::fact",
        _mem0_value="same",
    )
    memory.vector_store.list.return_value = ([duplicate_a, duplicate_b], None)

    preview = memory.consolidate({"user_id": "u1"})

    assert preview == {
        "dry_run": True,
        "strategy": "safe",
        "actions": [{"memory_id": "b", "action": "archive", "reason": "exact_duplicate:a"}],
    }
    memory.vector_store.update.assert_not_called()

    applied = memory.consolidate({"user_id": "u1"}, dry_run=False)

    assert applied["applied"] == 1
    update = memory.vector_store.update.call_args.kwargs
    assert update["payload"]["_mem0_status"] == "archived"
    assert update["payload"]["_mem0_archived_reason"] == "exact_duplicate:a"


def test_consolidate_preserves_active_reversion_as_canonical():
    memory = make_memory()
    old = row(
        "old",
        "User lives in Tokyo",
        created_at="2024-01-01T00:00:00Z",
        _mem0_kind="state",
        _mem0_state_key="user::residence",
        _mem0_value="Tokyo",
        _mem0_status="superseded",
        _mem0_schema_version=1,
    )
    current = row(
        "current",
        "User lives in Tokyo",
        created_at="2026-01-01T00:00:00Z",
        _mem0_kind="state",
        _mem0_state_key="user::residence",
        _mem0_value="Tokyo",
        _mem0_status="active",
        _mem0_supersedes=["old"],
        _mem0_schema_version=1,
    )
    memory.vector_store.list.return_value = ([old, current], None)

    preview = memory.consolidate({"user_id": "u1"})

    assert preview["actions"] == [{"memory_id": "old", "action": "archive", "reason": "exact_duplicate:current"}]


def test_consolidate_never_archives_independent_events_with_identical_text():
    memory = make_memory()
    memory.vector_store.list.return_value = (
        [
            row("event-1", "Daily standup", _mem0_kind="event", created_at="2026-01-01"),
            row("event-2", "Daily standup", _mem0_kind="event", created_at="2026-01-02"),
        ],
        None,
    )

    assert memory.consolidate({"user_id": "u1"})["actions"] == []


def test_consolidate_never_changes_duplicate_legacy_records():
    memory = make_memory()
    memory.vector_store.list.return_value = (
        [
            row("legacy-1", "Same legacy fact", created_at="2025-01-01"),
            row("legacy-2", "Same legacy fact", created_at="2026-01-01"),
        ],
        None,
    )

    preview = memory.consolidate({"user_id": "u1"})

    assert preview["actions"] == []


def test_consolidate_never_supersedes_a_linked_legacy_record():
    memory = make_memory()
    memory.vector_store.list.return_value = (
        [
            row("legacy", "Old fact", _mem0_status="active"),
            row(
                "current",
                "Current fact",
                _mem0_schema_version=1,
                _mem0_kind="state",
                _mem0_state_key="user::fact",
                _mem0_value="current",
                _mem0_supersedes=["legacy"],
            ),
        ],
        None,
    )

    preview = memory.consolidate({"user_id": "u1"})

    assert preview["actions"] == []


def test_consolidate_does_not_merge_same_text_across_distinct_state_keys():
    memory = make_memory()
    memory.vector_store.list.return_value = (
        [
            row(
                "one",
                "Available",
                _mem0_kind="state",
                _mem0_state_key="user::work_status",
                _mem0_value="available",
            ),
            row(
                "two",
                "Available",
                _mem0_kind="state",
                _mem0_state_key="user::calendar_status",
                _mem0_value="available",
            ),
        ],
        None,
    )

    assert memory.consolidate({"user_id": "u1"})["actions"] == []


def test_consolidate_restores_prior_updates_when_later_update_fails():
    memory = make_memory()
    lifecycle = {
        "_mem0_status": "active",
        "_mem0_schema_version": 1,
        "_mem0_kind": "state",
        "_mem0_state_key": "user::fact",
        "_mem0_value": "same",
    }
    first = row("a", "Same fact", **lifecycle)
    second = row("b", "Same fact", **lifecycle)
    third = row("c", "Same fact", **lifecycle)
    memory.vector_store.list.return_value = ([first, second, third], None)
    memory.vector_store.update.side_effect = [None, RuntimeError("provider failure"), None]

    with pytest.raises(MemoryPersistenceError, match="updates were restored"):
        memory.consolidate({"user_id": "u1"}, dry_run=False)

    restore = memory.vector_store.update.call_args_list[-1].kwargs
    assert restore["vector_id"] == "b"
    assert restore["payload"]["_mem0_status"] == "active"


@pytest.mark.parametrize(
    ("signal", "expected_status"),
    [("stale", "archived"), ("incorrect", "invalid")],
)
def test_feedback_suppresses_only_explicit_stale_or_incorrect(signal, expected_status):
    memory = make_memory()
    memory.vector_store.get.side_effect = [
        row("m1", "Fact", user_id="u1", _mem0_status="active", _mem0_evidence_strength=0.9),
        row("m2", "Replacement", user_id="u1", _mem0_status="active"),
    ]

    memory.feedback("m1", signal=signal, replacement_id="m2")

    payload = memory.vector_store.update.call_args.kwargs["payload"]
    assert payload["_mem0_status"] == expected_status
    assert payload["_mem0_replacement_id"] == "m2"
    assert payload["_mem0_evidence_strength"] == 0.9


def test_feedback_rejects_missing_replacement_before_mutating_memory():
    memory = make_memory()
    memory.vector_store.get.side_effect = [row("m1", "Fact", user_id="u1"), None]

    with pytest.raises(ValueError, match="Replacement memory with id m2 not found"):
        memory.feedback("m1", signal="stale", replacement_id="m2")

    memory.vector_store.update.assert_not_called()


def test_feedback_rejects_replacement_outside_identity_scope_before_mutating_memory():
    memory = make_memory()
    memory.vector_store.get.side_effect = [
        row("m1", "Fact", user_id="u1", agent_id="agent", run_id="run"),
        row("m2", "Replacement", user_id="u2", agent_id="agent", run_id="run"),
    ]

    with pytest.raises(ValueError, match="same identity scope"):
        memory.feedback("m1", signal="incorrect", replacement_id="m2")

    memory.vector_store.update.assert_not_called()


@pytest.mark.asyncio
async def test_async_feedback_rejects_replacement_outside_identity_scope():
    memory = make_memory(AsyncMemory)
    memory.vector_store.get.side_effect = [
        row("m1", "Fact", user_id="u1"),
        row("m2", "Replacement", user_id="u2"),
    ]

    with pytest.raises(ValueError, match="same identity scope"):
        await memory.feedback("m1", signal="stale", replacement_id="m2")

    memory.vector_store.update.assert_not_called()


def test_helpful_feedback_changes_salience_not_evidence_strength_or_eligibility():
    memory = make_memory()
    memory.vector_store.get.return_value = row("m1", "Fact", _mem0_status="active", _mem0_evidence_strength=0.4)

    memory.feedback("m1", signal="helpful")

    payload = memory.vector_store.update.call_args.kwargs["payload"]
    assert payload["_mem0_status"] == "active"
    assert payload["_mem0_evidence_strength"] == 0.4
    assert payload["_mem0_salience"] == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_async_audit_and_feedback_match_sync_contract():
    memory = make_memory(AsyncMemory)
    stored = row("m1", "Fact", _mem0_status="active", _mem0_schema_version=1, _mem0_source_hashes=["source"])
    memory.vector_store.list.return_value = ([stored], None)
    memory.vector_store.get.return_value = stored

    report = await memory.audit({"user_id": "u1"})
    feedback = await memory.feedback("m1", signal="irrelevant")

    assert report["scanned"] == 1
    assert feedback["signal"] == "irrelevant"
    assert memory.vector_store.update.call_args.kwargs["payload"]["_mem0_status"] == "active"
