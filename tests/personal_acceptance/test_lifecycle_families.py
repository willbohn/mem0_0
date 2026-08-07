"""Deterministic lifecycle scenario families (PA Task 3).

Each test drives an in-process ``Memory`` instance (scripted LLM, in-memory
vector store) through one canonical lifecycle scenario. The dispute,
corroboration, and reversion tests PIN verified existing behavior from
``mem0/memory/main.py`` and ``mem0/memory/lifecycle.py`` -- they assert what
the code actually does, not what it "should" do; each pinned assertion below
cites the file:line it pins.
"""

from tests.personal_acceptance.conftest import extraction_item, extraction_json


def test_supersession_family(scripted_memory):
    memory = scripted_memory(
        extraction_json(extraction_item("User lives in Sapporo", "Sapporo")),
        extraction_json(
            extraction_item(
                "User moved from Sapporo to Fukuoka",
                "Fukuoka",
                excerpt="moved from Sapporo to Fukuoka",
            )
        ),
    )

    first = memory.add(
        [{"role": "user", "content": "I live in Sapporo"}],
        user_id="u1",
        metadata={"created_at": "2024-01-01T00:00:00Z"},
    )
    correction = memory.add(
        [{"role": "user", "content": "I moved from Sapporo to Fukuoka"}],
        user_id="u1",
        metadata={"created_at": "2025-01-01T00:00:00Z"},
    )

    assert first["results"][0]["event"] == "ADD"
    assert correction["results"][0]["event"] == "ADD"

    statuses = {record.payload.get("_mem0_status") for record in memory.vector_store.records.values()}
    assert statuses == {"active", "superseded"}

    audit = memory.audit({"user_id": "u1"})
    assert audit["summary"]["stale_active_predecessors"] == 0


def test_current_vs_historical_family(scripted_memory):
    memory = scripted_memory(
        extraction_json(extraction_item("User lives in Sapporo", "Sapporo")),
        extraction_json(
            extraction_item(
                "User moved from Sapporo to Fukuoka",
                "Fukuoka",
                excerpt="moved from Sapporo to Fukuoka",
            )
        ),
    )

    memory.add(
        [{"role": "user", "content": "I live in Sapporo"}],
        user_id="u1",
        metadata={"created_at": "2024-01-01T00:00:00Z"},
    )
    memory.add(
        [{"role": "user", "content": "I moved from Sapporo to Fukuoka"}],
        user_id="u1",
        metadata={"created_at": "2025-01-01T00:00:00Z"},
    )

    current = memory.search("Where do I live now?", filters={"user_id": "u1"}, threshold=0.0)
    historical = memory.search(
        "Where did I live on 2024-06-01?",
        filters={"user_id": "u1"},
        threshold=0.0,
    )

    assert [item["memory"] for item in current["results"]] == ["User moved from Sapporo to Fukuoka"]
    assert [item["memory"] for item in historical["results"]] == ["User lives in Sapporo"]
    assert all("_mem0_status" not in repr(item) for item in current["results"] + historical["results"])


def test_direct_vs_lifecycle_family(scripted_memory):
    memory = scripted_memory(
        extraction_json(
            extraction_item(
                "User prefers dark mode",
                "dark_mode",
                kind="state",
                predicate="theme",
                excerpt="I like dark mode",
            )
        )
    )

    # Raw event: infer=False never asks the LLM to extract anything.
    memory.add([{"role": "user", "content": "I like dark mode"}], user_id="u1", infer=False)
    assert memory.llm.generate_response.call_count == 0

    raw_records = list(memory.vector_store.records.values())
    assert len(raw_records) == 1
    raw_payload = raw_records[0].payload
    assert raw_payload["_mem0_kind"] == "event"
    assert "_mem0_supersedes" not in raw_payload
    assert "_mem0_superseded_by" not in raw_payload
    assert "_mem0_relation" not in raw_payload

    # Scripted lifecycle add: infer=True (default) routes through extraction.
    memory.add([{"role": "user", "content": "I like dark mode"}], user_id="u1")
    assert memory.llm.generate_response.call_count == 1

    kinds = {record.payload.get("_mem0_kind") for record in memory.vector_store.records.values()}
    assert kinds == {"event", "state"}


def test_memory_kinds_family(scripted_memory):
    message_content = (
        "I like dark mode. I attended a conference. My manager is Casey. "
        "Always respond in Markdown. Deploy via the release checklist."
    )
    items = [
        extraction_item(
            "User prefers dark mode",
            "dark_mode",
            kind="preference",
            predicate="theme",
            excerpt="I like dark mode",
            relation=None,
        ),
        extraction_item(
            "User attended a conference",
            "conference",
            kind="event",
            predicate="activity",
            excerpt="I attended a conference",
            relation=None,
        ),
        extraction_item(
            "User's manager is Casey",
            "Casey",
            kind="relationship",
            predicate="manager",
            excerpt="My manager is Casey",
            relation=None,
        ),
        extraction_item(
            "Always respond in Markdown",
            "markdown",
            kind="instruction",
            predicate="response_format",
            excerpt="Always respond in Markdown",
            relation=None,
        ),
        extraction_item(
            "Deploy via the release checklist",
            "release_checklist",
            kind="procedure",
            predicate="deployment",
            excerpt="Deploy via the release checklist",
            relation=None,
        ),
    ]
    memory = scripted_memory(extraction_json(*items))

    memory.add([{"role": "user", "content": message_content}], user_id="u1")

    records = list(memory.vector_store.records.values())
    assert len(records) == 5
    kinds = sorted(record.payload.get("_mem0_kind") for record in records)
    assert kinds == ["event", "instruction", "preference", "procedure", "relationship"]

    results = memory.search("kinds", filters={"user_id": "u1"}, threshold=0.0)["results"]
    assert len(results) == 5
    assert all(not any(key.startswith("_mem0_") for key in (item.get("metadata") or {})) for item in results)


def test_dispute_family(scripted_memory):
    # Two adds, same subject/predicate ("residence"), different values, NO explicit relation.
    memory = scripted_memory(
        extraction_json(extraction_item("User lives in Nara", "Nara", relation=None)),
        extraction_json(extraction_item("User lives in Otaru", "Otaru", relation=None)),
    )
    memory.add([{"role": "user", "content": "I live in Nara"}], user_id="u1")
    memory.add([{"role": "user", "content": "I live in Otaru"}], user_id="u1")
    statuses = sorted(record.payload.get("_mem0_status") for record in memory.vector_store.records.values())
    # PIN the actual disposition (mem0/memory/main.py:1504-1516): when a state-like
    # extraction has no "supersedes" relation and a same-state-key candidate already
    # exists, BOTH the incoming and the existing record are marked "disputed" and
    # cross-linked via "_mem0_conflicts_with" -- neither one silently overwrites.
    assert statuses == ["disputed", "disputed"]


def test_corroboration_family(scripted_memory):
    # Second add repeats the SAME value ("Nara") with relation="corroborates".
    memory = scripted_memory(
        extraction_json(extraction_item("User lives in Nara", "Nara", relation=None)),
        extraction_json(
            extraction_item(
                "User confirms living in Nara",
                "Nara",
                excerpt="I still live in Nara",
                relation="corroborates",
            )
        ),
    )
    memory.add([{"role": "user", "content": "I live in Nara"}], user_id="u1")
    second = memory.add([{"role": "user", "content": "I confirm I still live in Nara"}], user_id="u1")

    # PIN the actual disposition (mem0/memory/main.py:1439-1463): the canonical match
    # is found by (state_key, value) BEFORE the relation is even consulted, so a
    # same-value add is folded into the existing active record -- source hashes and
    # message indexes are unioned, evidence strength is bumped -- rather than
    # creating a second competing record. No new record is inserted at all.
    assert second["results"][0]["event"] == "NONE"
    records = list(memory.vector_store.records.values())
    assert len(records) == 1
    payload = records[0].payload
    assert payload.get("_mem0_status") == "active"
    assert len(payload.get("_mem0_source_hashes", [])) == 2


def test_reversion_family(scripted_memory):
    # Three adds A -> B -> A ("Nara" -> "Otaru" -> "Nara"), relation "supersedes" each time.
    memory = scripted_memory(
        extraction_json(extraction_item("User lives in Nara", "Nara", relation=None)),
        extraction_json(
            extraction_item(
                "User moved from Nara to Otaru",
                "Otaru",
                excerpt="moved from Nara to Otaru",
            )
        ),
        extraction_json(
            extraction_item(
                "User moved back to Nara",
                "Nara",
                excerpt="moved back to Nara",
            )
        ),
    )
    memory.add([{"role": "user", "content": "I live in Nara"}], user_id="u1")
    memory.add([{"role": "user", "content": "I moved from Nara to Otaru"}], user_id="u1")
    memory.add([{"role": "user", "content": "I moved back to Nara"}], user_id="u1")

    payloads = [record.payload for record in memory.vector_store.records.values()]
    assert len(payloads) == 3

    # PIN the actual disposition (mem0/memory/main.py:1489-1503): "supersedes" always
    # inserts a brand-new record and marks every same-state-key match "superseded" --
    # it never flips an old record's status back to "active". Reverting to A's value
    # therefore produces a third, distinct active record rather than reactivating the
    # first one.
    active = [payload for payload in payloads if payload.get("_mem0_status") == "active"]
    superseded = [payload for payload in payloads if payload.get("_mem0_status") == "superseded"]
    assert len(active) == 1
    assert len(superseded) == 2
    assert active[0]["_mem0_value"] == "Nara"
    assert active[0]["data"] == "User moved back to Nara"

    # The middle value's ("Otaru") record is among the superseded ones.
    otaru_records = [payload for payload in superseded if payload.get("_mem0_value") == "Otaru"]
    assert len(otaru_records) == 1

    current = memory.search("Where do I live now?", filters={"user_id": "u1"}, threshold=0.0)
    assert [item["memory"] for item in current["results"]] == ["User moved back to Nara"]
