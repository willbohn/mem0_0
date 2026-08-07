from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Any

INTERNAL_PREFIX = "_mem0_"
SCHEMA_VERSION = 1
MEMORY_KINDS = frozenset({"state", "preference", "event", "plan", "relationship", "instruction", "procedure"})
STATE_LIKE_KINDS = MEMORY_KINDS - {"event"}
SUPPRESSED_STATUSES = frozenset({"invalid", "archived"})
RRF_WEIGHTS = {"semantic": 1.0, "keyword": 1.0, "entity": 0.5}
RRF_CONSTANT = 60

_HISTORICAL_PATTERNS = (
    r"\buse[ds]?\s+to\b",
    r"\bbefore\b",
    r"\bhistory\b",
    r"\bprevious(?:ly)?\b",
    r"\bformerly\b",
    r"\bantes\b",
    r"\bhistoria\b",
    r"\bavant\b",
    r"\bhistorique\b",
    r"以前",
    r"昔",
    r"履歴",
    r"かつて",
    r"前は",
    r"\b(?:19|20)\d{2}\b",
)


def _normalize_identifier(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return "_".join(part for part in re.split(r"[^\w]+", normalized, flags=re.UNICODE) if part)


def _bounded_strength(value: Any) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return 1.0


def strip_internal_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return caller-visible metadata without lifecycle implementation fields."""
    return {key: value for key, value in (metadata or {}).items() if not str(key).startswith(INTERNAL_PREFIX)}


def build_lifecycle_metadata(extraction: Mapping[str, Any], messages: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Validate an extraction's exact evidence and build private lifecycle metadata."""
    evidence = extraction.get("evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("memory evidence must include a message_index and exact excerpt")

    message_index = evidence.get("message_index")
    excerpt = evidence.get("excerpt")
    if not isinstance(message_index, int) or not 0 <= message_index < len(messages):
        raise ValueError("memory evidence message_index is invalid")

    message = messages[message_index]
    content = message.get("content")
    if message.get("role") == "system" or not isinstance(content, str):
        raise ValueError("memory evidence must reference a non-system source message")
    if not isinstance(excerpt, str) or not excerpt or excerpt not in content:
        raise ValueError("memory evidence excerpt is not an exact source substring")

    kind = str(extraction.get("kind") or "event").casefold()
    if kind not in MEMORY_KINDS:
        raise ValueError(f"unsupported memory kind: {kind}")

    subject = _normalize_identifier(extraction.get("subject"))
    predicate = _normalize_identifier(extraction.get("predicate"))
    state_key = f"{subject}::{predicate}" if subject and predicate else None

    metadata = {
        "_mem0_status": "active",
        "_mem0_kind": kind,
        "_mem0_source_hashes": [hashlib.sha256(excerpt.encode("utf-8")).hexdigest()],
        "_mem0_source_message_indexes": [message_index],
        "_mem0_evidence_strength": _bounded_strength(extraction.get("evidence_strength", 1.0)),
        "_mem0_schema_version": SCHEMA_VERSION,
    }
    if state_key and kind in STATE_LIKE_KINDS:
        metadata["_mem0_state_key"] = state_key
    if extraction.get("value") is not None:
        metadata["_mem0_value"] = extraction["value"]
    if extraction.get("valid_from") is not None:
        metadata["_mem0_valid_from"] = extraction["valid_from"]
    if extraction.get("valid_to") is not None:
        metadata["_mem0_valid_to"] = extraction["valid_to"]

    related_ids = extraction.get("related_memory_ids") or extraction.get("linked_memory_ids") or []
    if isinstance(related_ids, Iterable) and not isinstance(related_ids, (str, bytes)):
        metadata["_mem0_related_to"] = sorted({str(memory_id) for memory_id in related_ids if memory_id})

    relation = extraction.get("relation")
    if relation in {"supersedes", "conflicts_with", "corroborates"}:
        metadata["_mem0_relation"] = relation
    return metadata


def unicode_lexical_terms(text: str) -> list[str]:
    """Create deterministic Unicode terms, including n-grams for unsegmented scripts."""
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    words = re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)
    terms = list(words)

    has_unsegmented_script = any(
        "CJK" in unicodedata.name(char, "")
        or "HIRAGANA" in unicodedata.name(char, "")
        or "KATAKANA" in unicodedata.name(char, "")
        or "HANGUL" in unicodedata.name(char, "")
        for char in normalized
    )
    if has_unsegmented_script:
        compact = "".join(char for char in normalized if char.isalnum())
        for width in (2, 3):
            terms.extend(compact[index : index + width] for index in range(max(len(compact) - width + 1, 0)))

    return list(dict.fromkeys(term for term in terms if term))


def lexical_fallback_results(query: str, rows: Iterable[Any], *, top_k: int) -> list[dict[str, Any]]:
    """Rank listed rows by deterministic Unicode term overlap when native BM25 is unavailable."""
    query_terms = set(unicode_lexical_terms(query))
    if not query_terms:
        return []
    matches = []
    for row in rows:
        memory_id, _source_score, payload = _result_parts(row)
        if memory_id is None:
            continue
        indexed_text = str(payload.get("text_lemmatized") or payload.get("data") or "")
        memory_terms = set(unicode_lexical_terms(indexed_text))
        overlap = query_terms & memory_terms
        if not overlap:
            continue
        matches.append({"id": memory_id, "score": len(overlap) / len(query_terms), "payload": payload})
    matches.sort(key=lambda item: (-item["score"], item["id"]))
    return matches[:top_k]


def is_historical_query(query: str) -> bool:
    normalized = unicodedata.normalize("NFKC", query or "").casefold()
    # "Based on the conversation history ..." is common benchmark and agent
    # boilerplate for a current-state question.  Remove only that source-context
    # phrase; genuine requests such as "relocation history" remain historical,
    # and stronger markers (dates, "used to", "before") still match below.
    normalized = re.sub(r"\b(?:conversation|chat)\s+history\b", "", normalized)
    return any(re.search(pattern, normalized, flags=re.UNICODE) for pattern in _HISTORICAL_PATTERNS)


def historical_query_window(query: str) -> tuple[date, date] | None:
    """Return an explicit date/year window present in a historical query."""
    normalized = unicodedata.normalize("NFKC", query or "")
    date_match = re.search(r"\b((?:19|20)\d{2}-\d{2}-\d{2})\b", normalized)
    if date_match:
        try:
            target = date.fromisoformat(date_match.group(1))
        except ValueError:
            return None
        return target, target
    year_match = re.search(r"\b((?:19|20)\d{2})\b", normalized)
    if year_match:
        year = int(year_match.group(1))
        return date(year, 1, 1), date(year, 12, 31)
    return None


def _payload_overlaps_window(payload: Mapping[str, Any], window: tuple[date, date]) -> bool:
    def parse(value: Any) -> date | None:
        if not value:
            return None
        raw = str(value).replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(raw).date()
        except ValueError:
            try:
                return date.fromisoformat(raw)
            except ValueError:
                return None

    window_start, window_end = window
    valid_from = parse(payload.get("_mem0_valid_from"))
    valid_to = parse(payload.get("_mem0_valid_to"))
    return (valid_from is None or valid_from <= window_end) and (valid_to is None or valid_to >= window_start)


def _result_parts(result: Any) -> tuple[str | None, float, dict[str, Any]]:
    if isinstance(result, Mapping):
        memory_id = result.get("id")
        score = result.get("score") or 0.0
        payload = result.get("payload") or {}
    else:
        memory_id = getattr(result, "id", None)
        score = getattr(result, "score", 0.0) or 0.0
        payload = getattr(result, "payload", {}) or {}
    return (str(memory_id) if memory_id is not None else None, float(score), dict(payload))


def _row_parts(row: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(row, Mapping):
        return str(row.get("id")), dict(row.get("payload") or {})
    return str(row.id), dict(getattr(row, "payload", {}) or {})


def audit_rows(rows: Iterable[Any]) -> dict[str, Any]:
    """Build a content-free deterministic quality report for stored memories."""
    records = [_row_parts(row) for row in rows]
    ids = {memory_id for memory_id, _payload in records}
    superseded_targets = {
        str(target) for _memory_id, payload in records for target in payload.get("_mem0_supersedes", [])
    }

    content_groups: dict[str, list[str]] = {}
    orphaned_links = []
    provider_inconsistencies = []
    for memory_id, payload in records:
        content_key = _content_key(payload)
        if content_key:
            content_groups.setdefault(content_key, []).append(memory_id)
        if not payload.get("data") or not payload.get("hash"):
            provider_inconsistencies.append(memory_id)
        linked_ids = set()
        for field in (
            "_mem0_supersedes",
            "_mem0_conflicts_with",
            "_mem0_superseded_by",
            "_mem0_replacement_id",
        ):
            value = payload.get(field, [])
            if isinstance(value, (str, int)):
                linked_ids.add(str(value))
            elif isinstance(value, Iterable):
                linked_ids.update(str(item) for item in value if item)
        if linked_ids - ids:
            orphaned_links.append(memory_id)

    duplicate_groups = [sorted(group) for group in content_groups.values() if len(group) > 1]
    issues = {
        "duplicates": sorted(duplicate_groups),
        "stale_active_predecessors": sorted(
            memory_id
            for memory_id, payload in records
            if memory_id in superseded_targets and payload.get("_mem0_status", "active") == "active"
        ),
        "disputed_chains": sorted(
            memory_id for memory_id, payload in records if payload.get("_mem0_status") == "disputed"
        ),
        "ungrounded_legacy": sorted(
            memory_id for memory_id, payload in records if payload.get("_mem0_schema_version") is None
        ),
        "expired": sorted(
            memory_id
            for memory_id, payload in records
            if payload.get("expiration_date") and str(payload.get("expiration_date")) < date.today().isoformat()
        ),
        "orphaned_links": sorted(set(orphaned_links)),
        "provider_inconsistencies": sorted(set(provider_inconsistencies)),
    }
    return {
        "scanned": len(records),
        "summary": {name: len(items) for name, items in issues.items()},
        "issues": issues,
    }


def safe_consolidation_actions(rows: Iterable[Any]) -> list[dict[str, str]]:
    """Plan reversible, fact-preserving maintenance actions."""
    records = [_row_parts(row) for row in rows]
    by_content: dict[tuple[Any, ...], list[tuple[str, dict[str, Any]]]] = {}
    for record in records:
        payload = record[1]
        if (
            payload.get("_mem0_schema_version") != SCHEMA_VERSION
            or payload.get("_mem0_kind") == "event"
            or payload.get("_mem0_status") == "disputed"
        ):
            continue
        content_key = _content_key(payload)
        if content_key:
            kind = payload.get("_mem0_kind")
            state_key = payload.get("_mem0_state_key")
            if not kind or not state_key:
                continue
            duplicate_key = ("lifecycle", kind, state_key, repr(payload.get("_mem0_value")), content_key)
            by_content.setdefault(duplicate_key, []).append(record)

    actions = []
    acted_on = set()
    for group in by_content.values():
        if len(group) < 2:
            continue
        status_priority = {"active": 0, "disputed": 1, "superseded": 2, "archived": 3, "invalid": 4}
        ordered = sorted(
            group,
            key=lambda item: (
                status_priority.get(item[1].get("_mem0_status", "active"), 5),
                str(item[1].get("created_at") or ""),
                item[0],
            ),
        )
        canonical_id = ordered[0][0]
        for duplicate_id, _payload in ordered[1:]:
            actions.append(
                {"memory_id": duplicate_id, "action": "archive", "reason": f"exact_duplicate:{canonical_id}"}
            )
            acted_on.add(duplicate_id)

    superseded_targets = {
        str(target) for _memory_id, payload in records for target in payload.get("_mem0_supersedes", [])
    }
    for memory_id, payload in sorted(records):
        if (
            memory_id in superseded_targets
            and memory_id not in acted_on
            and payload.get("_mem0_schema_version") == SCHEMA_VERSION
            and payload.get("_mem0_status", "active") == "active"
        ):
            actions.append({"memory_id": memory_id, "action": "supersede", "reason": "linked_successor"})
    return actions


def _content_key(payload: Mapping[str, Any]) -> str:
    return " ".join(unicode_lexical_terms(str(payload.get("data") or "")))


def _is_recall_eligible(
    payload: Mapping[str, Any],
    *,
    historical: bool,
    historical_window: tuple[date, date] | None,
) -> bool:
    status = payload.get("_mem0_status", "active")
    if status in SUPPRESSED_STATUSES or (status == "superseded" and not historical):
        return False
    if historical and historical_window and payload.get("_mem0_kind") in STATE_LIKE_KINDS:
        return _payload_overlaps_window(payload, historical_window)
    return True


def reciprocal_rank_fusion(
    channels: Mapping[str, Iterable[Any] | None],
    *,
    threshold: float,
    top_k: int,
    historical: bool,
    historical_window: tuple[date, date] | None = None,
    max_chars: int | None = None,
    explain: bool = False,
) -> list[dict[str, Any]]:
    """Union and rank dense, keyword, and entity candidates with weighted RRF."""
    if top_k is not None and top_k <= 0:
        return []

    candidates: dict[str, dict[str, Any]] = {}
    active_weights = 0.0
    for channel, weight in RRF_WEIGHTS.items():
        results = []
        for raw_result in channels.get(channel) or []:
            result_parts = _result_parts(raw_result)
            if _is_recall_eligible(result_parts[2], historical=historical, historical_window=historical_window):
                results.append(result_parts)
        if not results:
            continue
        active_weights += weight
        seen_in_channel: set[str] = set()
        for rank, (memory_id, source_score, payload) in enumerate(results, start=1):
            if memory_id is None or memory_id in seen_in_channel:
                continue
            seen_in_channel.add(memory_id)
            candidate = candidates.setdefault(
                memory_id,
                {"id": memory_id, "payload": payload, "fusion": 0.0, "ranks": {}, "source_scores": {}},
            )
            if not candidate["payload"] and payload:
                candidate["payload"] = payload
            candidate["fusion"] += weight / (RRF_CONSTANT + rank)
            candidate["ranks"][channel] = rank
            candidate["source_scores"][channel] = source_score

    if not candidates or active_weights == 0:
        return []

    maximum = active_weights / (RRF_CONSTANT + 1)
    eligible = []
    for candidate in candidates.values():
        payload = candidate["payload"]
        candidate["score"] = min(candidate["fusion"] / maximum, 1.0)
        if candidate["score"] < threshold:
            continue
        strength = _bounded_strength(payload.get("_mem0_evidence_strength", 0.5))
        candidate["evidence_tiebreak"] = strength * 0.001
        eligible.append(candidate)

    eligible.sort(key=lambda item: (-item["score"], -item["evidence_tiebreak"], item["id"]))

    selected: list[dict[str, Any]] = []
    seen_state_keys: dict[str, str] = {}
    seen_content: dict[str, str] = {}
    char_count = 0
    for candidate in eligible:
        payload = candidate["payload"]
        status = payload.get("_mem0_status", "active")
        state_key = payload.get("_mem0_state_key")
        content_key = _content_key(payload)
        deduplication_reason = None
        canonical_id = None

        if not historical and status != "disputed" and state_key and state_key in seen_state_keys:
            canonical_id = seen_state_keys[state_key]
            deduplication_reason = f"state_key:{state_key}"
        elif content_key and payload.get("_mem0_kind") != "event":
            for prior_key in seen_content:
                if content_key == prior_key or SequenceMatcher(None, content_key, prior_key).ratio() >= 0.98:
                    canonical_id = seen_content[prior_key]
                    deduplication_reason = "near_identical"
                    break
        if deduplication_reason:
            if explain and canonical_id:
                canonical = next((item for item in selected if item["id"] == canonical_id), None)
                if canonical is not None:
                    canonical["score_details"].setdefault("deduplicated_candidates", []).append(
                        {"id": candidate["id"], "reason": deduplication_reason}
                    )
                    canonical["score_details"]["deduplication_reason"] = "canonical_result"
            continue

        text = str(payload.get("data") or "")
        if max_chars is not None and char_count + len(text) > max_chars:
            continue

        item = {"id": candidate["id"], "score": candidate["score"], "payload": payload}
        if explain:
            item["score_details"] = {
                "fusion_ranks": candidate["ranks"],
                "fusion_source_scores": candidate["source_scores"],
                "state_disposition": (
                    "historical" if status == "superseded" else "disputed" if status == "disputed" else "current"
                ),
                "deduplication_reason": None,
                "evidence_strength_factor": candidate["evidence_tiebreak"],
                "final_score": candidate["score"],
                "threshold": threshold,
            }
        selected.append(item)
        char_count += len(text)
        if state_key and status != "disputed":
            seen_state_keys[state_key] = candidate["id"]
        if content_key:
            seen_content[content_key] = candidate["id"]
        if len(selected) >= top_k:
            break

    return selected
