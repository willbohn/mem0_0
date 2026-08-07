# mem0_0 Stabilization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved and amended stabilization design (`docs/superpowers/specs/2026-08-07-mem0-0-stabilization-and-omp-design.md`) — regression-first fixes for every confirmed scope, extraction, edge-case, and mutation gap, plus bridge context bounding and OMP compatibility artifacts.

**Architecture:** Every task follows the same shape: write a failing regression test that reproduces the confirmed defect on the current branch, make the minimal fix, prove the test passes, commit. Tasks are grouped into phases matching the design's workstreams; each phase's commits are independent and revertible. No public package name, REST path, response envelope (beyond documented additive fields), MCP tool name, or legacy-mode semantics changes.

**Tech Stack:** Python 3.9+ (pytest via Hatch, Ruff line-length 120), TypeScript (jest, tsup, pnpm), FastAPI server, stdio MCP bridge (stdlib-only Python).

## Global Constraints

- Preserve Python import `mem0`, packages `mem0ai`, REST paths, `MEM0_*` env vars, `_mem0_*` fields, MCP tool names, and existing memory IDs/scopes/envelopes (design Principle 1).
- State-aware remains the default recall mode; `legacy` ordering and scoring stay byte-exact (pinned by `tests/memory/test_state_aware_search.py:180-239`, `tests/utils/test_scoring.py:53-156` — these must never be edited to pass).
- No new mandatory core dependency. No CI/CD workflow edits. No `pip`/`conda` — use Hatch (`uvx hatch run dev_py_3_12:test <path>` for focused Python runs). pnpm only for TypeScript.
- The full Python suite needs a Redis Stack fixture at `127.0.0.1:16379` (`REDIS_HOST=127.0.0.1 REDIS_PORT=16379 uvx hatch run dev_py_3_12:test`); focused runs do not. Never leave a temporary fixture running.
- Do not push, publish, rebuild the shadow, retarget harnesses, or touch the legacy deployment. Do not edit `~/.omp`, shell profiles, or `../system-init_sync`.
- No secrets, credentials, or personal memory bodies in code, tests, fixtures, or logs. A PostToolUse hook lints every edit (ruff, markdownlint, gitleaks); fix only what it reports in changed code.
- Before each commit: `git add` the exact files, verify with `git diff --cached --stat`, run `git diff --cached --check`, and for multi-file commits run the sweep gate `bash "${XDG_DATA_HOME:-$HOME/.local/share}/agent-harness/scripts/agent-glue-check.sh"`.
- End every commit message with: `Claude-Session: https://claude.ai/code/session_01DFFDonysTLJjm22kqmh44f`
- The pinned upstream comparison revision is `mem0ai/mem0` `main` @ `4a0a9a92a641b5da75023eca596a758a4f7ae101` (already fetched as `upstream/main`). Port behavior, never wholesale code.
- The bounded personal local acceptance suite is a **separate follow-up plan** (its own subsystem; planned after these fixes land so it tests final behavior). It is intentionally not in this document.

---

## Phase 1: Python write path

### Task 1: Strip caller-supplied identity scope on Python `add()`

**Files:**
- Modify: `mem0/memory/main.py:165-176` (`_strip_identity_keys` — add `context` param)
- Modify: `mem0/memory/main.py:593-597` (`_build_filters_and_metadata` — route caller metadata through it)
- Test: `tests/memory/test_state_aware_writes.py` (append)

**Interfaces:**
- Consumes: existing `_IDENTITY_KEYS = ENTITY_PARAMS | {"actor_id"}` (main.py:162), `make_memory()` fixture (test_state_aware_writes.py:53-69).
- Produces: `_strip_identity_keys(metadata, existing_payload, context="update()")` — third positional/keyword arg; existing two-arg call sites (main.py:2791, 5051) keep identical behavior.

- [ ] **Step 1: Write the failing tests**

Append to `tests/memory/test_state_aware_writes.py`:

```python
def test_add_metadata_cannot_inject_identity_scope(monkeypatch):
    memory = make_memory([])
    monkeypatch.setattr("mem0.memory.main.capture_event", lambda *_a, **_k: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice", lambda *_a, **_k: None)

    memory.add(
        [{"role": "user", "content": "note"}],
        run_id="r1",
        infer=False,
        metadata={"topic": "t", "user_id": "victim", "agent_id": "spoof", "actor_id": "spoof-actor"},
    )

    payload = memory.vector_store.insert.call_args.kwargs["payloads"][0]
    assert payload["run_id"] == "r1"
    assert payload["topic"] == "t"
    assert "user_id" not in payload
    assert "agent_id" not in payload
    assert "actor_id" not in payload


@pytest.mark.asyncio
async def test_async_add_metadata_cannot_inject_identity_scope(monkeypatch):
    memory = make_memory([], memory_class=AsyncMemory)
    monkeypatch.setattr("mem0.memory.main.capture_event", lambda *_a, **_k: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice_async", AsyncMock())

    await memory.add(
        [{"role": "user", "content": "note"}],
        run_id="r1",
        infer=False,
        metadata={"user_id": "victim", "actor_id": "spoof-actor"},
    )

    payload = memory.vector_store.insert.call_args.kwargs["payloads"][0]
    assert payload["run_id"] == "r1"
    assert "user_id" not in payload
    assert "actor_id" not in payload


def test_add_kwarg_identity_still_wins_over_metadata(monkeypatch):
    memory = make_memory([])
    monkeypatch.setattr("mem0.memory.main.capture_event", lambda *_a, **_k: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice", lambda *_a, **_k: None)

    memory.add(
        [{"role": "user", "content": "note"}],
        user_id="real",
        infer=False,
        metadata={"user_id": "victim"},
    )

    payload = memory.vector_store.insert.call_args.kwargs["payloads"][0]
    assert payload["user_id"] == "real"
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uvx hatch run dev_py_3_12:test tests/memory/test_state_aware_writes.py -k inject_identity -q`
Expected: FAIL — `payload["user_id"] == "victim"` (the spoofed key survives today).

- [ ] **Step 3: Implement**

In `mem0/memory/main.py:165`, change the signature and messages:

```python
def _strip_identity_keys(
    metadata: Dict[str, Any], existing_payload: Dict[str, Any], context: str = "update()"
) -> Dict[str, Any]:
    """Drop immutable identity and reserved lifecycle fields from caller metadata."""
    clean = {}
    for key, value in metadata.items():
        if str(key).startswith("_mem0_"):
            if value != existing_payload.get(key):
                logger.warning(f"{context}: ignoring metadata['{key}'] - lifecycle fields are managed internally")
        elif key not in _IDENTITY_KEYS:
            clean[key] = value
        elif value != existing_payload.get(key):
            logger.warning(f"{context}: ignoring metadata['{key}'] - identity fields are set from parameters")
    return clean
```

In `_build_filters_and_metadata` (main.py:593-597), replace the comprehension:

```python
    base_metadata_template = _strip_identity_keys(
        {key: deepcopy(value) for key, value in (input_metadata or {}).items()},
        {},
        context="add()",
    )
```

Note: `add()` previously stripped `_mem0_*` silently; it now warns like `update()` does. No test pins silence. The upstream analogue is `upstream/main` `mem0/memory/main.py:361-366` (issue 6655) — port the behavior, not the code.

- [ ] **Step 4: Run tests, verify pass + no regressions**

Run: `uvx hatch run dev_py_3_12:test tests/memory/test_state_aware_writes.py tests/memory/test_main.py tests/test_main.py -q`
Expected: PASS (update()-path tests at `tests/memory/test_main.py:439-459` must stay green).

- [ ] **Step 5: Commit**

```bash
git add mem0/memory/main.py tests/memory/test_state_aware_writes.py
git commit -m "fix(memory): strip caller-supplied identity scope on add()"
```

### Task 2: Extraction shape validation, typed parse error, skipped-item disclosure

**Files:**
- Modify: `mem0/exceptions.py` (after `LLMError`, ~line 377)
- Modify: `mem0/memory/main.py` — imports (line 13 area), new module helper `_parse_extraction_response`, sync parse+grounding block (1267-1294), sync `add()` wrap (1091-1099), async parse+grounding block (3436-3456), async `add()` wrap (3248-3256), `_add_to_vector_store` signatures (sync and async — add `stats=None`)
- Test: `tests/memory/test_state_aware_writes.py` (append)

**Interfaces:**
- Produces: `ExtractionParseError(MemoryError)` with `error_code="EXTRACT_001"` (importable from `mem0.exceptions`); `_parse_extraction_response(raw_response: str) -> list`; `add()` response gains optional additive key `skipped_invalid_extractions: int` (present only when > 0); `_add_to_vector_store(..., stats: dict | None = None)` fills `stats["skipped_invalid_extractions"]`.
- Consumes: `remove_code_blocks`, `extract_json` (already imported in main.py), `build_lifecycle_metadata` (lifecycle.py:55).

- [ ] **Step 1: Write the failing tests**

Append to `tests/memory/test_state_aware_writes.py` (add `from mem0.exceptions import ExtractionParseError` to its imports):

```python
@pytest.mark.parametrize("raw", ['"bare string"', "[1, 2]", "42", "null"])
def test_state_aware_add_raises_typed_error_for_non_object_response(raw):
    memory = make_memory([])
    memory.llm.generate_response.return_value = raw
    with pytest.raises(ExtractionParseError):
        memory._add_to_vector_store(
            [{"role": "user", "content": "hello"}], {"user_id": "u1"}, {"user_id": "u1"}, infer=True
        )
    memory.vector_store.insert.assert_not_called()


@pytest.mark.parametrize("field", ['"bare string"', "42", "null", '{"a": 1}'])
def test_state_aware_add_raises_typed_error_for_non_list_memory_field(field):
    memory = make_memory([])
    memory.llm.generate_response.return_value = '{"memory": %s}' % field
    with pytest.raises(ExtractionParseError):
        memory._add_to_vector_store(
            [{"role": "user", "content": "hello"}], {"user_id": "u1"}, {"user_id": "u1"}, infer=True
        )


@pytest.mark.asyncio
async def test_async_state_aware_add_raises_typed_error_for_malformed_response():
    memory = make_memory([], memory_class=AsyncMemory)
    memory.llm.generate_response.return_value = '{"memory": "bare string"}'
    with pytest.raises(ExtractionParseError):
        await memory._add_to_vector_store(
            [{"role": "user", "content": "hello"}], {"user_id": "u1"}, {"user_id": "u1"}, infer=True
        )


def test_add_discloses_skipped_invalid_extraction_items(monkeypatch):
    memory = make_memory([])
    valid = extraction("User lives in Tokyo", "Tokyo")
    memory.llm.generate_response.return_value = json.dumps({"memory": ["bare", 42, None, valid]})
    memory.embedding_model.embed_batch.return_value = [[0.1, 0.2]]
    monkeypatch.setattr("mem0.memory.main.capture_event", lambda *_a, **_k: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice", lambda *_a, **_k: None)

    response = memory.add([{"role": "user", "content": "I moved to Tokyo"}], user_id="u1")

    assert response["skipped_invalid_extractions"] == 3
    assert len(response["results"]) == 1


def test_add_all_invalid_items_returns_empty_with_disclosure(monkeypatch):
    memory = make_memory([])
    memory.llm.generate_response.return_value = json.dumps({"memory": ["bare", 42]})
    monkeypatch.setattr("mem0.memory.main.capture_event", lambda *_a, **_k: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice", lambda *_a, **_k: None)

    response = memory.add([{"role": "user", "content": "hello"}], user_id="u1")

    assert response["results"] == []
    assert response["skipped_invalid_extractions"] == 2
    memory.db.save_messages.assert_called_once()


def test_add_valid_empty_extraction_has_no_disclosure_field(monkeypatch):
    memory = make_memory([])
    monkeypatch.setattr("mem0.memory.main.capture_event", lambda *_a, **_k: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice", lambda *_a, **_k: None)

    response = memory.add([{"role": "user", "content": "hello"}], user_id="u1")

    assert response["results"] == []
    assert "skipped_invalid_extractions" not in response
```

- [ ] **Step 2: Run tests, verify they fail with today's raw errors**

Run: `uvx hatch run dev_py_3_12:test tests/memory/test_state_aware_writes.py -k "typed_error or disclos" -q`
Expected: FAIL — `AttributeError: 'str' object has no attribute 'get'` / `TypeError` instead of `ExtractionParseError`; no `skipped_invalid_extractions` key.

- [ ] **Step 3: Implement the exception**

In `mem0/exceptions.py`, after `LLMError` (line ~377):

```python
class ExtractionParseError(MemoryError):
    """Raised when an extraction LLM response cannot be parsed into the expected shape.

    Distinct from LLMError: the provider responded, but the response was not a
    JSON object with a list-valued "memory" field.
    """

    def __init__(self, message: str, details: dict = None):
        super().__init__(
            message=message,
            error_code="EXTRACT_001",
            details=details,
            suggestion="Check the configured extraction model's JSON output quality",
        )
```

- [ ] **Step 4: Implement parsing and grounding**

In `mem0/memory/main.py`: add `from collections.abc import Mapping` near line 13 and extend the `mem0.exceptions` import with `ExtractionParseError`. Add the module-level helper (near `_validate_search_params`):

```python
def _parse_extraction_response(raw_response: str) -> list:
    """Decode an extraction response and validate its shape.

    Returns the list under the "memory" key (empty when the response is blank
    or the key is absent). Raises ExtractionParseError when the response is
    undecodable, its top level is not a JSON object, or "memory" is not a list.
    """
    cleaned = remove_code_blocks(raw_response)
    if not cleaned or not cleaned.strip():
        return []
    try:
        decoded = json.loads(cleaned, strict=False)
    except json.JSONDecodeError:
        try:
            decoded = json.loads(extract_json(cleaned), strict=False)
        except Exception as error:
            raise ExtractionParseError(f"extraction response is not valid JSON: {error}") from error
    if not isinstance(decoded, dict):
        raise ExtractionParseError(
            f"extraction response top level is {type(decoded).__name__}, expected a JSON object"
        )
    if "memory" not in decoded:
        return []
    memory_field = decoded["memory"]
    if not isinstance(memory_field, list):
        raise ExtractionParseError(
            f"extraction 'memory' field is {type(memory_field).__name__}, expected a list"
        )
    return memory_field
```

Replace the sync parse block (main.py:1267-1280) with `extracted_memories = _parse_extraction_response(response)` (the error propagates; do not wrap it). Replace the grounding loop (1282-1294) with:

```python
        grounded_memories = []
        skipped_invalid = 0
        for extracted in extracted_memories:
            if not isinstance(extracted, Mapping):
                skipped_invalid += 1
                logger.warning("Skipping non-mapping extraction item of type %s", type(extracted).__name__)
                continue
            text_value = extracted.get("text")
            if not isinstance(text_value, str) or not text_value.strip():
                skipped_invalid += 1
                logger.warning("Skipping extraction item with missing or empty text")
                continue
            try:
                lifecycle_metadata = build_lifecycle_metadata(extracted, messages)
            except ValueError as error:
                skipped_invalid += 1
                logger.warning("Skipping ungrounded memory extraction: %s", error)
                continue
            related = lifecycle_metadata.get("_mem0_related_to", [])
            lifecycle_metadata["_mem0_related_to"] = [
                uuid_mapping[memory_id] for memory_id in related if memory_id in uuid_mapping
            ]
            grounded_memories.append({**extracted, "_mem0_lifecycle": lifecycle_metadata})
        extracted_memories = grounded_memories
        if stats is not None and skipped_invalid:
            stats["skipped_invalid_extractions"] = skipped_invalid
```

Add `stats: Optional[Dict[str, Any]] = None` as the final keyword parameter of both `_add_to_vector_store` methods. In sync `add()` (main.py:1091-1099):

```python
        stats: Dict[str, Any] = {}
        vector_store_result = self._add_to_vector_store(
            messages, processed_metadata, effective_filters, infer, prompt=prompt, stats=stats
        )
        # ... (existing notice lines unchanged) ...
        response = {"results": vector_store_result}
        if stats.get("skipped_invalid_extractions"):
            response["skipped_invalid_extractions"] = stats["skipped_invalid_extractions"]
        return response
```

Mirror all three changes at the async sites (parse block 3436-3448, grounding loop 3450-3456, wrap 3248-3256). The two blocks are textually identical copies — apply the same replacement to both and diff them against each other afterward.

- [ ] **Step 5: Run tests, verify pass + existing contracts hold**

Run: `uvx hatch run dev_py_3_12:test tests/memory/ tests/test_main.py tests/test_chatty_llm_parsing.py -q`
Expected: PASS — including the existing ungrounded-rejection test (test_state_aware_writes.py:100-111) and LLMError provider-failure behavior (main.py:1251-1265, untouched).

- [ ] **Step 6: Commit**

```bash
git add mem0/exceptions.py mem0/memory/main.py tests/memory/test_state_aware_writes.py
git commit -m "fix(memory): validate extraction shape with typed parse error and skip disclosure"
```

---

## Phase 2: Python search and listing edges

### Task 3: Reject NaN thresholds

**Files:**
- Modify: `mem0/memory/main.py:241-247` (`_validate_search_params`); add `import math` to main.py imports if absent
- Test: `tests/test_main.py` (append next to the threshold tests at 506-514)

**Interfaces:** none new — tightens existing validation; infinities already rejected by the range check.

- [ ] **Step 1: Write the failing test** (follow the harness used by `tests/test_main.py:506-514`)

```python
def test_search_rejects_nan_threshold(memory_instance):
    with pytest.raises(ValueError, match="threshold"):
        memory_instance.search("q", user_id="u1", threshold=float("nan"))
    memory_instance.embedding_model.embed.assert_not_called()
```

(Use the same fixture name the neighboring threshold tests use — read them first; if they call `_validate_search_params` directly, test that function directly instead with `pytest.raises(ValueError)`.)

- [ ] **Step 2: Run, verify FAIL** — `uvx hatch run dev_py_3_12:test tests/test_main.py -k nan -q` → NaN is accepted today.

- [ ] **Step 3: Implement** — in `_validate_search_params` after the isinstance check (main.py:243):

```python
        if isinstance(threshold, float) and math.isnan(threshold):
            raise ValueError(f"Invalid threshold: {threshold}. Must be between 0 and 1 (inclusive).")
```

- [ ] **Step 4: Run, verify PASS** — same command plus `tests/test_main.py -k threshold -q` (1.5 / -0.5 / 0 / 1.0 anchors stay green).

- [ ] **Step 5: Commit** — `git add mem0/memory/main.py tests/test_main.py && git commit -m "fix(memory): reject NaN search thresholds before embedding work"`

### Task 4: `top_k=0` returns zero rows everywhere

**Files:**
- Modify: `mem0/memory/main.py` — sync `get_all` (early return, ~1786), sync `_get_all_from_vector_store` (1852-1854 check-before-append), sync `search` (early return after `_validate_search_params`, ~1920), async twins (3980-3995, 4046-4048, ~4116)
- Modify: `mem0/memory/lifecycle.py` — `reciprocal_rank_fusion` guard (function containing lines 432-456)
- Modify: `tests/test_main.py:552-563` — replace the vacuous `test_search_accepts_top_k_zero`
- Test: `tests/memory/test_state_aware_writes.py` (append; reuse `make_memory`), `tests/memory/test_lifecycle.py` (append)

**Interfaces:** none new. Behavior contract: `get_all(top_k=0)` and `search(top_k=0)` return `{"results": []}` without calling the vector store or embedder; `reciprocal_rank_fusion(..., top_k=0)` returns `[]`.

- [ ] **Step 1: Write the failing tests**

In `tests/memory/test_state_aware_writes.py`:

```python
def test_get_all_top_k_zero_returns_zero_rows(monkeypatch):
    memory = make_memory([])
    row = SimpleNamespace(id="m1", payload={"data": "x", "user_id": "u1"})
    memory.vector_store.list = MagicMock(return_value=([row], None))
    monkeypatch.setattr("mem0.memory.main.capture_event", lambda *_a, **_k: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice", lambda *_a, **_k: None)

    assert memory.get_all(user_id="u1", top_k=0) == {"results": []}
    memory.vector_store.list.assert_not_called()


def test_search_top_k_zero_returns_zero_rows(monkeypatch):
    memory = make_memory([])
    monkeypatch.setattr("mem0.memory.main.capture_event", lambda *_a, **_k: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice", lambda *_a, **_k: None)

    assert memory.search("query", user_id="u1", top_k=0) == {"results": []}
    memory.embedding_model.embed.assert_not_called()
```

(If `get_all`/`search` call other notice helpers, monkeypatch those too — grep the method bodies; add async variants mirroring both with `AsyncMemory` and `display_first_run_notice_async`.) In `tests/memory/test_lifecycle.py`, mirror the invocation pattern of the existing `max_chars` budget test (test_lifecycle.py:205-221) with `top_k=0` and assert the fusion returns `[]`.

- [ ] **Step 2: Run, verify FAIL** — each currently returns exactly one row (append-then-check defect).

- [ ] **Step 3: Implement**

1. `reciprocal_rank_fusion` (lifecycle.py): first line of the function body: `if top_k is not None and top_k <= 0: return []`.
2. Sync `get_all` (main.py, after the filters validation at 1780-1784): `if top_k == 0: return {"results": []}` — before the telemetry/fetch. Async twin identically.
3. Sync `search` (immediately after its `_validate_search_params` call): `if top_k == 0: return {"results": []}`. Async twin identically.
4. `_get_all_from_vector_store` (main.py:1852-1854 and async 4046-4048): swap to check-before-append:

```python
            if output_limit is not None and len(formatted_memories) >= output_limit:
                break
            formatted_memories.append(memory_item_dict)
```

5. Replace `tests/test_main.py:552-563` (`test_search_accepts_top_k_zero`) with a value-asserting version: result equals `{"results": []}` and the mocked store is not called.

- [ ] **Step 4: Run, verify PASS** — `uvx hatch run dev_py_3_12:test tests/memory/ tests/test_main.py tests/utils/test_scoring.py -q` (legacy `scored[:top_k]` behavior at scoring.py:139 is untouched and stays green).

- [ ] **Step 5: Commit** — `git add mem0/memory/main.py mem0/memory/lifecycle.py tests/memory/test_state_aware_writes.py tests/memory/test_lifecycle.py tests/test_main.py && git commit -m "fix(memory): return zero rows for top_k=0 in get_all and state-aware search"`

### Task 5: Unify recall-mode fallback on state-aware

**Files:**
- Modify: `mem0/memory/main.py` — add `_recall_mode` helper; replace the six `getattr(getattr(self.config, "recall", None), "mode", ...)` sites (1216, 2125, 2779, 3384, 4326, 5039 — grep `"recall", None` to catch all)
- Test: `tests/memory/test_lifecycle.py` (append)

**Interfaces:**
- Produces: `_recall_mode(config) -> str` (module-level, main.py) returning `"state_aware"` when `config` lacks a `recall` attribute.

- [ ] **Step 1: Failing test**

```python
def test_recall_mode_fallback_defaults_to_state_aware():
    from types import SimpleNamespace
    from mem0.memory.main import _recall_mode

    assert _recall_mode(SimpleNamespace()) == "state_aware"
    assert _recall_mode(SimpleNamespace(recall=SimpleNamespace(mode="legacy"))) == "legacy"
```

- [ ] **Step 2: Run, verify FAIL** — `ImportError: cannot import name '_recall_mode'`.

- [ ] **Step 3: Implement**

```python
def _recall_mode(config) -> str:
    """Resolve the recall mode; configs without a recall attribute default to state-aware."""
    return getattr(getattr(config, "recall", None), "mode", "state_aware")
```

Replace all six inline chains with `_recall_mode(self.config)`. The write paths (1216, 3384) already defaulted to `"state_aware"` — behavior unchanged there; the read/update paths flip their unreachable-with-`MemoryConfig` fallback from `"legacy"`.

- [ ] **Step 4: Run** — `uvx hatch run dev_py_3_12:test tests/memory/ -q` → PASS (test_lifecycle.py:21-26 default-mode anchors stay green).

- [ ] **Step 5: Commit** — `git add mem0/memory/main.py tests/memory/test_lifecycle.py && git commit -m "fix(memory): unify recall-mode fallback on state-aware"`

---

## Phase 3: Python providers, imports, and utils

### Task 6: Ollama embedder raises plain ImportError

**Files:**
- Modify: `mem0/embeddings/ollama.py:1-21`
- Test: `tests/embeddings/test_ollama_embeddings.py` (append)

- [ ] **Step 1: Failing test**

```python
def test_import_without_ollama_raises_import_error(monkeypatch):
    import builtins
    import importlib
    import sys

    monkeypatch.setattr(builtins, "input", lambda *_a: pytest.fail("interactive install prompt ran"))
    monkeypatch.setitem(sys.modules, "ollama", None)
    sys.modules.pop("mem0.embeddings.ollama", None)
    try:
        with pytest.raises(ImportError, match="ollama"):
            importlib.import_module("mem0.embeddings.ollama")
    finally:
        sys.modules.pop("mem0.embeddings.ollama", None)
```

- [ ] **Step 2: Run, verify FAIL** — today the module calls `input()` (the monkeypatched `pytest.fail` fires).

- [ ] **Step 3: Implement** — replace `mem0/embeddings/ollama.py` lines 1-21 with the pattern from `mem0/llms/ollama.py:4-7`:

```python
from typing import Literal, Optional

from mem0.configs.embeddings.base import BaseEmbedderConfig
from mem0.embeddings.base import EmbeddingBase

try:
    from ollama import Client
except ImportError:
    raise ImportError("The 'ollama' library is required. Please install it using 'pip install ollama'.")
```

(`subprocess` and `sys` imports become unused — remove them.)

- [ ] **Step 4: Run** — `uvx hatch run dev_py_3_12:test tests/embeddings/test_ollama_embeddings.py -q` → PASS.

- [ ] **Step 5: Commit** — `git add mem0/embeddings/ollama.py tests/embeddings/test_ollama_embeddings.py && git commit -m "fix(embeddings): raise plain ImportError for missing ollama dependency"`

### Task 7: Ollama default dims 768

**Files:**
- Modify: `mem0/embeddings/ollama.py:29`
- Test: `tests/embeddings/test_ollama_embeddings.py` (append, reusing that file's existing `Client` mocking pattern)

- [ ] **Step 1: Failing test** — following the file's existing patch style (its tests mock `mem0.embeddings.ollama.Client`):

```python
def test_default_embedding_dims_match_nomic_embed_text(mock_ollama_client):
    config = BaseEmbedderConfig()
    embedder = OllamaEmbedding(config)
    assert embedder.config.model == "nomic-embed-text"
    assert embedder.config.embedding_dims == 768
```

(Use the file's actual fixture/patch name — read its first test and mirror it exactly.)

- [ ] **Step 2: Run, verify FAIL** — dims resolve to 512.
- [ ] **Step 3: Implement** — line 29: `self.config.embedding_dims = self.config.embedding_dims or 768`. Safe for existing collections: `embedding_dims` never sizes vector stores (`embedding_model_dims` does) and is consumed only by telemetry (`mem0/memory/telemetry.py:208`); TS already defaults to 768 (`mem0-ts/src/oss/src/embeddings/ollama.ts:18`). Existing tests passing explicit 512 stay valid.
- [ ] **Step 4: Run** — same file, PASS.
- [ ] **Step 5: Commit** — `git add mem0/embeddings/ollama.py tests/embeddings/test_ollama_embeddings.py && git commit -m "fix(embeddings): default ollama nomic-embed-text dims to 768"`

### Task 8: FastEmbed returns a plain list

**Files:**
- Modify: `mem0/embeddings/fastembed.py:31-32`
- Test: `tests/embeddings/test_fastembed_embeddings.py` (strengthen existing assertion at :25-32)

- [ ] **Step 1: Failing test** — extend the existing embed test (its mock yields `iter([np.array([...])])`):

```python
    assert isinstance(embedding, list)
    assert all(isinstance(value, float) for value in embedding)
```

- [ ] **Step 2: Run, verify FAIL** — an ndarray is returned today.
- [ ] **Step 3: Implement** — replace lines 31-32:

```python
        embeddings = list(self.dense_model.embed(text))
        first = embeddings[0]
        return first.tolist() if hasattr(first, "tolist") else list(first)
```

- [ ] **Step 4: Run** — `uvx hatch run dev_py_3_12:test tests/embeddings/test_fastembed_embeddings.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add mem0/embeddings/fastembed.py tests/embeddings/test_fastembed_embeddings.py && git commit -m "fix(embeddings): normalize fastembed output to a plain list"`

### Task 9: Hugging Face OpenAI-compatible API-key forwarding

**Files:**
- Modify: `mem0/embeddings/huggingface.py:20`
- Test: `tests/embeddings/test_huggingface_embeddings.py` (append)

- [ ] **Step 1: Failing test**

```python
def test_openai_compatible_client_receives_configured_api_key():
    with patch("mem0.embeddings.huggingface.OpenAI") as mock_openai:
        config = BaseEmbedderConfig(api_key="hf_test_key", huggingface_base_url="http://localhost:8080/v1")
        HuggingFaceEmbedding(config)
    mock_openai.assert_called_once_with(base_url="http://localhost:8080/v1", api_key="hf_test_key")
```

- [ ] **Step 2: Run, verify FAIL** — called without `api_key` today.
- [ ] **Step 3: Implement** — line 20: `self.client = OpenAI(base_url=self.config.huggingface_base_url, api_key=self.config.api_key)`. When `api_key` is `None` the OpenAI SDK falls back to `OPENAI_API_KEY` exactly as today.
- [ ] **Step 4: Run** — that file, PASS.
- [ ] **Step 5: Commit** — `git add mem0/embeddings/huggingface.py tests/embeddings/test_huggingface_embeddings.py && git commit -m "fix(embeddings): forward configured api key to huggingface openai-compatible client"`

### Task 10: Import-time config-directory guard

**Files:**
- Modify: `mem0/memory/setup.py:11-15`
- Test: Create `tests/memory/test_setup_import.py`

- [ ] **Step 1: Failing test**

```python
import os
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod is ineffective as root")
def test_import_mem0_with_uncreatable_config_dir(tmp_path):
    parent = tmp_path / "ro"
    parent.mkdir()
    parent.chmod(0o555)
    env = {**os.environ, "MEM0_DIR": str(parent / "child"), "MEM0_TELEMETRY": "false"}
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "import mem0"], env=env, capture_output=True, text=True, timeout=120
        )
    finally:
        parent.chmod(0o755)
    assert proc.returncode == 0, proc.stderr
```

- [ ] **Step 2: Run, verify FAIL** — `PermissionError` at `mem0/memory/setup.py:13` (reproduced during review).
- [ ] **Step 3: Implement** — in `setup.py`, move `_logger = logging.getLogger(__name__)` above the directory block and guard it:

```python
_logger = logging.getLogger(__name__)

VECTOR_ID = str(uuid.uuid4())
mem0_dir = str(resolve_mem0_dir())
try:
    os.makedirs(mem0_dir, exist_ok=True)
except OSError as error:
    _logger.debug("Could not create mem0 dir %s at import: %s", mem0_dir, error)
```

Lazy creation on first write is preserved — `_write_config` already retries `os.makedirs` best-effort (setup.py:41).

- [ ] **Step 4: Run** — `uvx hatch run dev_py_3_12:test tests/memory/test_setup_import.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add mem0/memory/setup.py tests/memory/test_setup_import.py && git commit -m "fix(memory): keep import working when the config directory cannot be created"`

### Task 11: Non-mutating reranker fallbacks and factory merge

**Files:**
- Modify: `mem0/reranker/cohere_reranker.py:84-90`, `mem0/reranker/huggingface_reranker.py:163-169`, `mem0/reranker/sentence_transformer_reranker.py:108-114`, `mem0/reranker/zero_entropy_reranker.py:95-101`
- Modify: `mem0/utils/factory.py:87-90`
- Test: `tests/rerankers/` (append to each provider's existing test file, following its instantiation pattern) and `tests/utils/test_factory_config.py` (create)

- [ ] **Step 1: Failing tests** — one per reranker (Cohere shown; mirror for the other three with their client attribute names):

```python
def test_failed_rerank_does_not_mutate_caller_documents(reranker_with_failing_client):
    docs = [{"memory": "a"}, {"memory": "b"}]
    result = reranker_with_failing_client.rerank("q", docs, top_k=2)
    assert docs == [{"memory": "a"}, {"memory": "b"}]
    assert all(item["rerank_score"] == 0.0 for item in result)
```

(Build `reranker_with_failing_client` the way each test file already constructs its reranker, with the client method mocked to raise.) Factory test:

```python
def test_llm_factory_does_not_mutate_caller_dict_config(monkeypatch):
    from mem0.utils import factory

    captured = {}

    class DummyLLM:
        def __init__(self, config):
            captured["config"] = config

    monkeypatch.setattr(factory, "load_class", lambda _path: DummyLLM)
    caller_config = {"model": "m"}
    factory.LlmFactory.create("openai", caller_config, temperature=0.5)
    assert caller_config == {"model": "m"}
```

- [ ] **Step 2: Run, verify FAIL** — caller docs gain `rerank_score`; caller dict gains `temperature`.
- [ ] **Step 3: Implement** — each reranker except-path copies before annotating (Cohere shown; identical shape in the other three):

```python
        except Exception as e:
            logger.warning("Cohere reranking failed, falling back to original order: %s", e)
            fallback_docs = []
            for doc in documents:
                fallback = doc.copy()
                fallback["rerank_score"] = 0.0
                fallback_docs.append(fallback)
            final_top_k = top_k or self.config.top_k
            return fallback_docs[:final_top_k] if final_top_k else fallback_docs
```

Factory (factory.py:87-90): `config = config_class(**{**config, **kwargs})` — no in-place `update`.

- [ ] **Step 4: Run** — `uvx hatch run dev_py_3_12:test tests/rerankers/ tests/utils/ -q` → PASS.
- [ ] **Step 5: Commit** — `git add mem0/reranker/*.py mem0/utils/factory.py tests/rerankers/ tests/utils/test_factory_config.py && git commit -m "fix(reranker): stop mutating caller documents and configs on fallback paths"` (run the sweep gate first — multi-file commit).

### Task 12: Lexical crash hardening (noun-chunks and search-path entity extraction)

**Files:**
- Modify: `mem0/utils/entity_extraction.py:591` (`_add_topic_phrase_candidates`)
- Modify: `mem0/memory/main.py:2158-2159, 2267-2268` and their async twins (grep `extract_entities(` in main.py; the add-path sites at 940-941 and 1562-1564 are already wrapped)
- Test: `tests/memory/test_state_aware_search.py` (append), plus a unit test beside the existing entity-extraction tests

**Interfaces:** none new; scoring for the default English model is unchanged (guards only convert raises into empty candidate lists).

- [ ] **Step 1: Failing tests**

```python
def test_search_survives_entity_extraction_failure(monkeypatch):
    # Build the search double the way this file's existing state-aware search tests do.
    monkeypatch.setattr(
        "mem0.memory.main.extract_entities",
        lambda _q: (_ for _ in ()).throw(NotImplementedError("no noun chunks")),
    )
    # ...construct memory double per this file's pattern, then:
    result = memory.search("query", user_id="u1")
    assert "results" in result
```

Unit test for the noun-chunk guard: inspect `_add_topic_phrase_candidates`'s signature (entity_extraction.py:585-600) and call it with a doc double whose `noun_chunks` property raises `NotImplementedError` (class with `@property def noun_chunks(self): raise NotImplementedError(...)` and empty `ents`); assert it returns without raising.

- [ ] **Step 2: Run, verify FAIL** — the search-level test raises `NotImplementedError` today.
- [ ] **Step 3: Implement** — entity_extraction.py:591:

```python
        try:
            noun_chunks = list(doc.noun_chunks)
        except NotImplementedError:
            noun_chunks = []
        for chunk in noun_chunks:
```

main.py search sites (each unwrapped `extract_entities(query)` call):

```python
        try:
            query_entities = extract_entities(query)
        except Exception:
            logger.debug("Entity extraction failed for search query; continuing without entity candidates")
            query_entities = []
```

(Match the variable name each site actually assigns.)

- [ ] **Step 4: Run** — `uvx hatch run dev_py_3_12:test tests/memory/test_state_aware_search.py tests/utils/ -q` → PASS, legacy anchors untouched.
- [ ] **Step 5: Commit** — `git add mem0/utils/entity_extraction.py mem0/memory/main.py tests/ && git commit -m "fix(memory): harden lexical pipeline against noun-chunk and entity-extraction failures"` (sweep gate first).

### Task 13: Body-free invalid-message warning

**Files:**
- Modify: `mem0/memory/main.py:1110` (and its async twin — grep `Skipping invalid message format`)
- Test: `tests/memory/test_state_aware_writes.py` (append)

- [ ] **Step 1: Failing test**

```python
def test_invalid_message_warning_does_not_log_bodies(monkeypatch, caplog):
    memory = make_memory([])
    monkeypatch.setattr("mem0.memory.main.capture_event", lambda *_a, **_k: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice", lambda *_a, **_k: None)

    with caplog.at_level("WARNING"):
        memory.add(
            [{"bogus": "SECRET-BODY-TOKEN"}, {"role": "user", "content": "keep me"}],
            user_id="u1",
            infer=False,
        )
    assert "SECRET-BODY-TOKEN" not in caplog.text
```

- [ ] **Step 2: Run, verify FAIL** — the raw dict is interpolated today.
- [ ] **Step 3: Implement** — at each site:

```python
                logger.warning(
                    "Skipping invalid message format: keys=%s",
                    sorted(message_dict) if isinstance(message_dict, dict) else type(message_dict).__name__,
                )
```

- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** — `git add mem0/memory/main.py tests/memory/test_state_aware_writes.py && git commit -m "fix(memory): stop logging message bodies for invalid message formats"`

---

## Phase 4: REST server

### Task 14: Align the admin serializer with SDK reads

**Files:**
- Modify: `server/main.py:495-513` (`_RESERVED_PAYLOAD_KEYS`, `_serialize_memory`)
- Test: `tests/test_server_params.py:787-804` (extend the existing end-to-end admin-listing test)

**Interfaces:**
- Produces: admin `GET /memories` rows gain top-level `actor_id`, `role`, `attributed_to` (additive) and their `metadata` excludes `text_lemmatized`, `actor_id`, `role`, `attributed_to` (matching scoped SDK reads; `_mem0_*` was already excluded).

- [ ] **Step 1: Failing test** — extend the fixture payload in `test_get_memories_admin_listing_hides_internal_lifecycle_metadata` with `"text_lemmatized": "love hike weekend", "actor_id": "actor-1", "role": "user", "attributed_to": "someone"` and assert:

```python
    assert row["metadata"] == {"source": "fixture"}
    assert row["actor_id"] == "actor-1"
    assert row["role"] == "user"
    assert row["attributed_to"] == "someone"
```

- [ ] **Step 2: Run, verify FAIL** — `uvx hatch run dev_py_3_12:test tests/test_server_params.py -k admin_listing -q` → `metadata` contains `text_lemmatized` (the confirmed leak, matching upstream issue 6825).
- [ ] **Step 3: Implement**

```python
_RESERVED_PAYLOAD_KEYS = {
    "data",
    "user_id",
    "agent_id",
    "run_id",
    "actor_id",
    "role",
    "attributed_to",
    "hash",
    "created_at",
    "updated_at",
    "expiration_date",
    "text_lemmatized",
}
```

and add to `_serialize_memory`'s returned dict (after `run_id`): `"actor_id": payload.get("actor_id"), "role": payload.get("role"), "attributed_to": payload.get("attributed_to"),`

- [ ] **Step 4: Run** — `uvx hatch run dev_py_3_12:test tests/test_server_params.py tests/test_server_auth.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add server/main.py tests/test_server_params.py && git commit -m "fix(server): stop leaking text_lemmatized and promoted keys in admin listing metadata"`

### Task 15: Metadata-boundary regression matrix (tests only)

**Files:**
- Test: Create `tests/memory/test_metadata_boundary.py`

**Interfaces:** consumes existing read paths only — no production change. This pins the already-correct behavior the design removed from code scope (no test in the repo currently asserts `text_lemmatized` absence on any read path).

- [ ] **Step 1: Write the pinning tests** (using the `make_memory` double pattern with a stored payload containing `text_lemmatized` and `_mem0_status`):

```python
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.memory.test_state_aware_writes import make_memory
from mem0.memory.main import AsyncMemory, Memory

PAYLOAD = {
    "data": "likes hiking",
    "user_id": "u1",
    "source": "fixture",
    "text_lemmatized": "like hike",
    "_mem0_status": "active",
    "created_at": None,
    "updated_at": None,
    "hash": "h",
}


def _row():
    return SimpleNamespace(id="m1", payload=dict(PAYLOAD), score=0.9)


def test_get_excludes_internal_fields():
    memory = make_memory([])
    memory.vector_store.get = MagicMock(return_value=_row())
    item = memory.get("m1")
    metadata = item.get("metadata") or {}
    assert "text_lemmatized" not in metadata
    assert not any(key.startswith("_mem0_") for key in metadata)


def test_get_all_excludes_internal_fields(monkeypatch):
    memory = make_memory([])
    memory.vector_store.list = MagicMock(return_value=([_row()], None))
    monkeypatch.setattr("mem0.memory.main.capture_event", lambda *_a, **_k: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice", lambda *_a, **_k: None)
    rows = memory.get_all(user_id="u1")["results"]
    for row in rows:
        metadata = row.get("metadata") or {}
        assert "text_lemmatized" not in metadata
        assert not any(key.startswith("_mem0_") for key in metadata)
```

Add the same pattern for state-aware `search` and legacy-mode `search` (set `memory.config.recall.mode = "legacy"` for the latter and mock `vector_store.search`/`keyword_search` per the doubles in `tests/memory/test_state_aware_search.py`), plus async `get`/`get_all` variants.

- [ ] **Step 2: Run — these should PASS immediately** (behavior already exists; if any fails, that is a real finding — stop and report before "fixing" the test).

Run: `uvx hatch run dev_py_3_12:test tests/memory/test_metadata_boundary.py -q`

- [ ] **Step 3: Commit** — `git add tests/memory/test_metadata_boundary.py && git commit -m "test(memory): pin text_lemmatized and _mem0_* suppression on all read paths"`

---

## Phase 5: TypeScript OSS

### Task 16: TS `add()` — clone filters, strip identity metadata, persist filters-only identity

**Files:**
- Modify: `mem0-ts/src/oss/src/memory/index.ts:916-942` (add), `:2798-2801` (update — factor its filter into the shared helper)
- Modify: `mem0-ts/src/oss/src/memory/lifecycle.ts` (new `stripIdentityKeys` export beside `stripInternalMetadata`, :236-244)
- Test: `mem0-ts/src/oss/tests/memory.add.test.ts` (append; the file already builds a Memory over provider `memory` with `dbPath: ":memory:"` and mocked embedder/LLM — no network)

**Interfaces:**
- Produces: `stripIdentityKeys(metadata: Record<string, any>): Record<string, any>` in lifecycle.ts removing `user_id/agent_id/run_id/actor_id` in both snake_case and camelCase; used by both `add()` and `update()`.
- Behavior contract: `add()` never mutates `config.filters`; caller metadata cannot scope a memory; identity supplied only via `config.filters` with `infer: false` is persisted into the stored payload.

- [ ] **Step 1: Failing tests** (mirror the shapes of the update() tests at `memory.crud.test.ts:214-283`):

```ts
test("add() does not mutate the caller's filters object", async () => {
  const callerFilters: Record<string, any> = {};
  await memory.add("hello world", { userId: "u1", filters: callerFilters, infer: false });
  expect(callerFilters).toEqual({});
});

test("add() metadata cannot inject an identity field (snake_case or camelCase)", async () => {
  const victim = `victim_${Date.now()}`;
  const agentId = `agent_${Date.now()}`;
  const addResult = await memory.add("agent-scoped secret", {
    agentId,
    metadata: { user_id: victim, userId: victim, actor_id: "spoof" },
    infer: false,
  });
  const id = addResult.results[0].id;

  const agentList = await memory.getAll({ filters: { agent_id: agentId } });
  expect(agentList.results.map((m) => m.id)).toContain(id);

  const victimList = await memory.getAll({ filters: { user_id: victim } });
  expect(victimList.results.map((m) => m.id)).not.toContain(id);
});

test("add() with identity supplied only via filters persists that scope", async () => {
  const filtersUser = `filters_only_${Date.now()}`;
  const addResult = await memory.add("scoped only via filters", {
    filters: { user_id: filtersUser },
    infer: false,
  });
  const id = addResult.results[0].id;

  const list = await memory.getAll({ filters: { user_id: filtersUser } });
  expect(list.results.map((m) => m.id)).toContain(id);
});
```

- [ ] **Step 2: Run, verify FAIL** — `cd mem0-ts && pnpm run test -- memory.add` → filters mutated `{user_id: "u1"}`; victim list contains the id; filters-only row is an unscoped orphan.

- [ ] **Step 3: Implement**

In `lifecycle.ts`, beside `stripInternalMetadata`:

```ts
const IDENTITY_METADATA_KEYS = new Set([
  "user_id",
  "agent_id",
  "run_id",
  "actor_id",
  "userId",
  "agentId",
  "runId",
  "actorId",
]);

export function stripIdentityKeys(
  metadata: Record<string, any>,
): Record<string, any> {
  return Object.fromEntries(
    Object.entries(metadata ?? {}).filter(
      ([key]) => !IDENTITY_METADATA_KEYS.has(key),
    ),
  );
}
```

In `index.ts` `add()` (916-931): stop destructuring `filters` by reference and strip identity from metadata:

```ts
    const { metadata: callerMetadata = {}, infer = true } = config;
    const filters: SearchFilters = { ...(config.filters ?? {}) };
    const metadata = stripInternalMetadata(stripIdentityKeys(callerMetadata));
```

After the required-scope guard (938-942), back-fill payload identity from filters so filters-only callers are persisted scoped:

```ts
    if (filters.user_id) metadata.user_id = filters.user_id;
    if (filters.agent_id) metadata.agent_id = filters.agent_id;
    if (filters.run_id) metadata.run_id = filters.run_id;
```

(The camelCase-param lines at 929-931 keep writing both `filters.*` and `metadata.*`; the back-fill makes the filters-only path equivalent.) In `update()` (2798-2801), replace the inline `IDENTITY_KEYS` filter with `stripIdentityKeys(...)` so both methods share one key set — first check `ENTITY_PARAMS`/`IDENTITY_KEYS` at index.ts:136-138: if the existing update() filter covers keys the shared helper does not, extend `IDENTITY_METADATA_KEYS` to the union; the update() tests at memory.crud.test.ts:214-283 must stay green unmodified.

- [ ] **Step 4: Run the TS gates**

```bash
cd mem0-ts && pnpm run test -- memory.add && pnpm run test && pnpm run build
```

Expected: new tests PASS; full jest suite has no new failures vs the recorded baseline (91 suites / 1,458 tests passed, 5/42 skipped); build passes. Run prettier on changed files: `pnpm exec prettier --check src/oss/src/memory/index.ts src/oss/src/memory/lifecycle.ts` (run `--write` on those two files only if it fails).

- [ ] **Step 5: Commit**

```bash
git add mem0-ts/src/oss/src/memory/index.ts mem0-ts/src/oss/src/memory/lifecycle.ts mem0-ts/src/oss/tests/memory.add.test.ts
git commit -m "fix(ts-oss): clone filters, strip identity metadata, and persist filters-only scope on add()"
```

(sweep gate first — multi-file commit.)

---

## Phase 6: MCP bridge

### Task 17: Bridge-side `max_chars` with omission disclosure, plus missing contract anchors

**Files:**
- Modify: `deployment/mem0_0_arch/bridge/bridge.py` — `TOOLS` search entry (:161-165), `search_memories` handler (:314-326)
- Test: `tests/deployment/mem0_0_arch/test_bridge.py` (append; use the existing `FakeMem0` harness and `key_file` fixture)

**Interfaces:**
- Produces: `search_memories` optional integer arg `max_chars` (default `MAX_CONTENT_CHARS` = 16384, valid range 1..16384, booleans rejected); response gains additive key `omitted_for_budget: int` present only when ≥ 1 result was dropped. The REST `/search` body remains exactly `{"query", "filters", "top_k"}` — the pinned envelope test at test_bridge.py:293 must stay green unmodified.

- [ ] **Step 1: Failing tests**

```python
@pytest.mark.parametrize("bad", [True, 0, -1, 16385, "12"])
def test_search_rejects_invalid_max_chars(key_file, bad):
    bridge = Mem0Bridge(api_key_file=key_file)
    with pytest.raises(BridgeError) as excinfo:
        bridge.call_tool("search_memories", {**FULL_SCOPE, "query": "q", "max_chars": bad})
    assert excinfo.value.code == "invalid_argument"


def test_search_enforces_cumulative_budget_with_disclosure(fake_server_bridge):
    bridge, state = fake_server_bridge
    long_text = "x" * 300
    short_text = "y" * 50
    for content in (long_text, short_text):
        bridge.call_tool("add_memory", {**FULL_SCOPE, "content": content})

    result = bridge.call_tool("search_memories", {**FULL_SCOPE, "query": "budget", "max_chars": 100})

    texts = [row["memory"] for row in result["results"]]
    assert long_text not in texts          # over-budget row omitted whole, never truncated
    assert short_text in texts             # lower-ranked shorter row still admitted
    assert result["omitted_for_budget"] == 1
    search_bodies = [r["body"] for r in state.requests if r["path"] == "/search"]
    assert all("max_chars" not in body for body in search_bodies)  # never forwarded to REST


def test_search_default_budget_returns_no_disclosure_when_nothing_omitted(fake_server_bridge):
    bridge, _state = fake_server_bridge
    bridge.call_tool("add_memory", {**FULL_SCOPE, "content": "small"})
    result = bridge.call_tool("search_memories", {**FULL_SCOPE, "query": "small"})
    assert "omitted_for_budget" not in result


def test_query_length_limit_enforced(key_file):
    bridge = Mem0Bridge(api_key_file=key_file)
    with pytest.raises(BridgeError) as excinfo:
        bridge.call_tool("search_memories", {**FULL_SCOPE, "query": "q" * 4097})
    assert excinfo.value.code == "invalid_argument"


@pytest.mark.parametrize("bad_top_k", [0, 101, True])
def test_search_top_k_bounds_enforced(key_file, bad_top_k):
    bridge = Mem0Bridge(api_key_file=key_file)
    with pytest.raises(BridgeError) as excinfo:
        bridge.call_tool("search_memories", {**FULL_SCOPE, "query": "q", "top_k": bad_top_k})
    assert excinfo.value.code == "invalid_argument"
```

(`FULL_SCOPE` = the five-field scope dict the existing tests use — reuse their constant/fixture, e.g. the scope from test_bridge.py:178-186; `fake_server_bridge` = the existing FakeMem0-backed fixture pattern at test_bridge.py:251-298.) Also assert the tools/list schema now carries `max_chars`: extend `test_runtime_identity_and_tool_names_are_canonical` (:189-202) or add a sibling asserting `"max_chars" in search_schema["properties"]`.

- [ ] **Step 2: Run, verify FAIL** — `uvx hatch run dev_py_3_12:test tests/deployment/mem0_0_arch/test_bridge.py -q` → `max_chars` is silently ignored today; budget/disclosure tests fail; the two anchor tests (query length, top_k bounds) should PASS already — keep them (they pin enforced-but-untested behavior).

- [ ] **Step 3: Implement** — TOOLS search entry:

```python
        (
            "search_memories",
            "Search only within one explicit scope.",
            {"query": {"type": "string"}, "top_k": {"type": "integer"}, "max_chars": {"type": "integer"}},
        ),
```

Handler (:314-326):

```python
        if name == "search_memories":
            query = _require_text(args, "query", MAX_QUERY_CHARS)
            top_k = args.get("top_k", self.max_results)
            if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= self.max_results:
                raise BridgeError("invalid_argument", f"top_k must be between 1 and {self.max_results}")
            max_chars = args.get("max_chars", MAX_CONTENT_CHARS)
            if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= MAX_CONTENT_CHARS:
                raise BridgeError("invalid_argument", f"max_chars must be between 1 and {MAX_CONTENT_CHARS}")
            response = self._request(
                "POST",
                "/search",
                body={"query": query, "filters": scope.as_dict(), "top_k": top_k},
            )
            rows = [row for row in _rows(response) if self._row_in_scope(row, scope)]
            _reject_secrets(rows)
            selected: list[dict[str, Any]] = []
            omitted = 0
            char_count = 0
            for row in rows:
                text = str(row.get("memory") or "")
                if char_count + len(text) > max_chars:
                    omitted += 1
                    continue
                selected.append(row)
                char_count += len(text)
            result: dict[str, Any] = {"results": selected}
            if omitted:
                result["omitted_for_budget"] = omitted
            return result
```

- [ ] **Step 4: Run** — `uvx hatch run dev_py_3_12:test tests/deployment/ -q` → PASS, including the pinned `/search` body test (:293) and the handshake test (:189-202) unmodified.
- [ ] **Step 5: Commit** — `git add deployment/mem0_0_arch/bridge/bridge.py tests/deployment/mem0_0_arch/test_bridge.py && git commit -m "feat(bridge): bound search context with max_chars and omission disclosure"`

---

## Phase 7: OMP compatibility artifacts

### Task 18: OMP harness artifacts (content-free, provisional until installation)

**Files:**
- Create: `deployment/mem0_0_arch/harnesses/omp/mcp.json.example`
- Create: `deployment/mem0_0_arch/harnesses/omp/policy-fragment.md`
- Create: `deployment/mem0_0_arch/harnesses/omp/runbook.md`
- Test: Create `tests/deployment/mem0_0_arch/test_omp_artifacts.py`

**Interfaces:** documentation artifacts only — nothing under `~/.omp`, no shell profiles, no credential values. Everything OMP-runtime-specific is labeled "pending installation."

- [ ] **Step 1: Failing tests**

```python
import json
from pathlib import Path

ARTIFACTS = Path(__file__).parents[3] / "deployment" / "mem0_0_arch" / "harnesses" / "omp"
EXPECTED_TOOLS = ("add_memory", "search_memories", "get_memories", "get_memory", "update_memory", "delete_memory")


def test_mcp_example_uses_key_file_and_no_inline_credentials():
    data = json.loads((ARTIFACTS / "mcp.json.example").read_text())
    (server,) = [srv for block in data.values() if isinstance(block, dict) for srv in block.values()]
    env = server["env"]
    assert "MEM0_API_KEY" not in env
    assert env["MEM0_API_KEY_FILE"].endswith("bridge-api-key")
    assert env["MEM0_API_URL"] == "http://127.0.0.1:28889"
    assert Path(server["args"][-1]).name == "bridge.py"


def test_policy_fragment_pins_scope_constants():
    text = (ARTIFACTS / "policy-fragment.md").read_text()
    for required in ("agent_id=omp", "machine_id=arch-local", "project_id=mem0_0", "infer=false", "MEM0_API_KEY_FILE"):
        assert required in text


def test_runbook_lists_exact_tools_and_pending_label():
    text = (ARTIFACTS / "runbook.md").read_text()
    for tool in EXPECTED_TOOLS:
        assert tool in text
    assert "pending installation" in text
```

- [ ] **Step 2: Run, verify FAIL** — files do not exist (`harnesses/` has no directory today).
- [ ] **Step 3: Create the artifacts**

`mcp.json.example` (before committing, re-verify the top-level wrapper key against the primary source `docs/mcp-config.md` in `can1357/oh-my-pi` via `gh api repos/can1357/oh-my-pi/contents/docs/mcp-config.md --jq .content | base64 -d | head -140`; per the 2026-08-07 review it is `mcpServers` with per-server `command`/`args`/`env`, stdio by default):

```json
{
  "mcpServers": {
    "mem0-local": {
      "command": "/home/will/Documents/CodingWorkspaces/mem0_0/.venv/bin/python",
      "args": [
        "/home/will/Documents/CodingWorkspaces/mem0_0/deployment/mem0_0_arch/bridge/bridge.py"
      ],
      "env": {
        "MEM0_API_URL": "http://127.0.0.1:28889",
        "MEM0_API_KEY_FILE": "/home/will/.config/mem0_0-arch/bridge-api-key"
      }
    }
  }
}
```

`policy-fragment.md` — behavioral policy for the OMP profile, covering exactly (write real prose for each; one short section per bullet):

- When to search (`search_memories` before answering questions that stored context could change) and when to write (durable preferences, decisions, constraints, lessons only).
- The five explicit scope fields on every call, with the constants: `user_id=will`, `agent_id=omp`, `machine_id=arch-local`, `project_id=mem0_0` for this repository (project identity is always an operator-chosen constant, never derived from CWD or Git metadata).
- Secret exclusion (never send credentials, tokens, keys, cookies, connection strings; the bridge also rejects them fail-closed).
- Deletion requires `confirm=true` and prior user confirmation.
- `infer=false` is the default write mode; `infer=true` only for durable state/preference/relationship/instruction/procedure content when the local provider is ready and the measured local write latency (roughly 70-110 s) is acceptable.
- Corrections should update a known scoped memory ID; an unlinked corrective raw write is not automatic supersession.

`runbook.md` — dedicated-profile lifecycle, all commands content-free, every OMP-runtime claim labeled "pending installation":

- Create/select the dedicated profile (`omp --profile mem0-validation`); place this example at the profile's user MCP config path.
- Verify `memory.backend` is `off` (its documented default) so no second retain/injection path exists.
- Enumerate discovered servers (`/mcp list`) and disable any hosted Mem0 configs discovered from Claude/Codex by their discovered names via the profile's `disabledServers` denylist; confirm no project-level `.omp/mcp.json` or `.mcp.json` exists in the working repository.
- Validation gate: the ten installation-dependent steps from the design (record versions/config hashes; exactly one enabled `mem0-local` server; MCP connection test confirming the six expected tools `add_memory`, `search_memories`, `get_memories`, `get_memory`, `update_memory`, `delete_memory`; synthetic five-field CRUD; fail-closed proofs per operation class; no cross-scope recall; `max_chars` respected; fresh-session policy adherence; durability across compaction/shutdown/restart; exact synthetic cleanup plus receipt reconciliation).
- Rollback and removal: remove the profile directory and denylist entries; nothing outside the profile was changed.
- Status line, verbatim: "OMP integration prepared; transport and harness behavior pending installation."

- [ ] **Step 4: Run** — `uvx hatch run dev_py_3_12:test tests/deployment/mem0_0_arch/test_omp_artifacts.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add deployment/mem0_0_arch/harnesses/ tests/deployment/mem0_0_arch/test_omp_artifacts.py && git commit -m "docs(deployment): add provisional OMP harness artifacts and policy"` (sweep gate first).

---

## Phase 8: Documentation and full verification

### Task 19: Documentation outputs

**Files:**
- Modify: `evaluation_overlay/README.md` (add an "Acceptance classes" paragraph)
- Modify: `README.md` (only the existing state-aware fork notice block — locate it first; change nothing else)

- [ ] **Step 1: evaluation_overlay/README.md** — append a short section stating there are two distinct acceptance classes: (1) the deferred official benchmark gate (STALE, Memora/FAMA, LoCoMo, LongMemEval, BEAM — unrun, unclaimed, thresholds unchanged) and (2) the bounded personal local acceptance suite (defined in the 2026-08-07 design; budgets: ≤24 families, ≤120 mutations, ≤48 `infer=true` writes, 100-minute provider wall-clock cap; provides bounded local confidence only and must never be presented as official benchmark acceptance). Note the suite implementation is a separate follow-up plan.
- [ ] **Step 2: README.md** — inside the existing fork-notice block only, add one sentence: "A bounded stabilization pass (2026-08-07 design, amended after independent adversarial review) hardened scope isolation, extraction validation, and bridge context bounding; the official research benchmark remains deferred and unclaimed." Preserve every other README section verbatim.
- [ ] **Step 3: Commit** — `git add evaluation_overlay/README.md README.md && git commit -m "docs: separate acceptance classes and note the stabilization pass"`

### Task 20: Full-suite verification

No file changes — evidence gathering. Run in order; every step must pass before Task 21.

- [ ] **Step 1: Ruff + focused suites** — the lint hook already ran per edit; confirm cleanliness on changed paths: `uvx hatch run dev_py_3_12:test tests/memory tests/embeddings tests/rerankers tests/utils tests/deployment tests/test_main.py tests/test_server_params.py tests/test_server_auth.py -q`
- [ ] **Step 2: Full Python suite** — start the Redis Stack fixture, run, stop it:

```bash
podman run -d --name mem0-test-redis -p 16379:6379 redis/redis-stack-server:latest
REDIS_HOST=127.0.0.1 REDIS_PORT=16379 uvx hatch run dev_py_3_12:test
podman rm -f mem0-test-redis
```

Expected: no failures (baseline: 2,055 passed, 73 skipped, plus this plan's new tests).
- [ ] **Step 3: Server + deployment suites** — `uvx hatch run dev_py_3_12:test tests/test_server_params.py tests/test_server_auth.py tests/test_api_keys_router.py tests/test_provider_readiness.py tests/deployment/ -q` (baseline: 94 + 61 passed).
- [ ] **Step 4: TypeScript** — `cd mem0-ts && pnpm run test && pnpm run build` (baseline: 91 suites / 1,458 tests passed, 5/42 skipped). Differential type-check against the pinned base: `./node_modules/.bin/tsc --noEmit` must exit with exactly the same 26 diagnostics recorded for local `main` @ `50bdaaea` — any new candidate-only diagnostic is a failure; the 26 pre-existing ones are not.
- [ ] **Step 5: `git diff --check`** on the whole branch range and `git status --porcelain` clean.

### Task 21: Fresh verification, adversarial review, and handover checkpoint

- [ ] **Step 1:** Dispatch a fresh verifier agent to re-run the full Python, server, deployment, and TypeScript suites from a cold context and exercise at least: one identity-injection attempt (Python and TS), one malformed-extraction add, one `max_chars` budgeted bridge search, and `top_k=0` on both `get_all` and `search`. Evidence, not opinions.
- [ ] **Step 2:** Dispatch a fresh critic agent for an adversarial review of the complete branch delta, focused on scope, secret, persistence, and compatibility boundaries (legacy-mode exactness, envelope additivity, five-field scope, fail-closed behavior). Fix or explicitly defer every finding before proceeding.
- [ ] **Step 3:** Update `docs/designs/mem0-0-state-aware-handover.md` with the implementation checkpoint: date/purpose, branch and exact HEAD, unchanged running candidate (`75021d0a` / `33fb027e...`) and the note that the shadow does NOT yet run this code, upstream ahead/behind, files changed, commits created, validation commands with summarized results, no runtime/deployment/harness/CI-CD/production change, remaining gates (official benchmark deferred; OMP validation pending installation; personal acceptance suite = next plan), and the single next recommended bounded task.
- [ ] **Step 4: Commit** — `git add docs/designs/mem0-0-state-aware-handover.md && git commit -m "docs: record stabilization implementation checkpoint"`
- [ ] **Step 5:** Report the exact final HEAD, clean/dirty status, all commits created, and verification evidence. Do NOT push, rebuild the shadow, or claim any runtime behavior change — the shadow still runs `75021d0a` until a separately approved refresh.

---

## Self-review notes (spec coverage)

- Workstream 1 items → Tasks 1 (Python identity), 16 (TS identity + filters clone + filters-only orphan), 6 (Ollama import), 8 (FastEmbed), 9 (HF key), 11 (non-mutating rerankers/factory). Verified-moot items (design list) have no tasks by design; Task 15 pins the already-correct read boundary.
- Workstream 2 → Tasks 2 (extraction + parse error + disclosure), 3 (NaN), 4 (top_k=0 incl. state-aware search), 5 (recall fallback), 14 (admin serializer), 10 (import-time dir), 7 (Ollama dims), 12 (lexical crash hardening only — spaCy configurability and empty-lemma fallback are deferred by the amended design), 13 (body-free logging).
- Workstream 3 → Task 17 (bridge `max_chars`, 16,384 default, bridge-side enforcement, disclosure, retained contract anchors).
- Workstream 4 → Task 18 (artifacts; installation-gated validation stays pending).
- Documentation outputs → Tasks 18, 19, 21; personal acceptance suite contract doc lands with the suite's own follow-up plan.
- Verification strategy → Tasks 20, 21 (regression-first per task; full suites; differential TS baseline `50bdaaea`/26; fresh verifier + critic; handover refresh).
