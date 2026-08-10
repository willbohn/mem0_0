# mem0_0 Promote-and-Integrate Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.
> **Phase-gated:** Phase A is fully task-expanded. Phases B–E are skeletons; each is
> expanded into bite-sized tasks when the prior phase completes (their content depends
> on what the Phase A/B measurement surfaces).

**Goal:** Promote local mem0 to the permanent mem0_0 implementation with vanilla-baseline
measurement captured *during* promotion (fix-forward, no rollback), then ship mem0_0 as a
memory provider for Claude Code, Codex, oh-my-pi, and Hermes Agent, fix what measurement
reveals, and land everything OSS-ready.

**Architecture:** The existing benchmark runners (`evaluation/benchmarks/{locomo,longmemeval,beam}/run.py`)
are backend-switchable via `--backend oss --mem0-host <url>`. The vanilla arm points at the
legacy server (`http://localhost:8888`); the candidate arm at the canonical shadow
(`http://127.0.0.1:28889`). Cutover machinery (`deployment/mem0_0_arch/cutover.py`) is
currently an unimplemented stub and must be built; harness retargeting is README-spec only
(bridge config files + systemd units) with no tooling yet.

**Tech Stack:** Python 3.11 (hatch), FastAPI server, podman compose shadow, MCP server
integrations (TypeScript/tsup), pytest, jest/vitest.

---

## Owner decisions baked into this plan (2026-08-10)

1. **Promote-first.** Cutover (SA-12) is NOT gated on benchmark results. Measurement is
   captured *during* promotion, not as a pre-cutover go/no-go.
2. **Fix-forward, no rollback drill.** OWNER OVERRIDE of SA-12: the
   `legacy_rollback_image_provenance` gate and the rollback drill are WAIVED. Response
   policy during promotion is fix-forward only. (Recorded in the ledger 2026-08-10.)
3. **Measurement arms.** Vanilla arm = current mem0 `main` at `http://localhost:8888`;
   candidate arm = mem0_0 at `http://127.0.0.1:28889`; SAME benchmarks + SAME model.
   The arm switch is `--mem0-host` (NOT `--backend`, which only selects oss-vs-cloud).

---

## Recon findings (driving the tasks)

- `deployment/mem0_0_arch/cutover.py` is a 46-line stub: `REQUIRED_GATES` (9 names),
  `cutover_status()` returns `status:"blocked"`, `create_cutover_record()` always raises,
  `main()` exits 2. **No gate is implemented.**
- Rollback-coupled gates to re-scope under the override: `legacy_rollback_image_provenance`
  (cutover.py:19) — waived; `live_candidate_reconciliation` (cutover.py:18) and
  `cross_store_snapshot_provenance` (cutover.py:17) — keep, but drop their rollback-consumer
  coupling (rollback.py:28-41 reads `legacy_state_retained` + `backup_receipt_sha256`).
- Benchmark runners take `--backend {oss,cloud}`, `--mem0-host <url>`, `--project-name`
  (required), `--top-k`, `--predict-only`, `--evaluate-only`; BEAM adds `--chat-sizes`.
- Harness retargeting mutates: per-harness bridge config files (endpoint URL 8888→28889)
  and systemd unit state (disable/retain `mem0-arch.service`, enable `mem0_0-arch.service`).
  No tooling exists; README.md:158,181 is the spec.
- Integrations follow one pattern: MCP server + lifecycle hooks, registered in 5
  `marketplace.json` files + `docs/docs.json` + `docs/llms.txt` + a CI workflow pair.
  `integrations/mem0-plugin` already targets Claude Code + Codex; **there is NO
  `integrations/hermes/` directory** — only a docs page (`docs/integrations/hermes.mdx`).

---

# PHASE A — Instrumentation (measurement harness)  [DETAILED]

**Phase goal:** A reproducible, content-free way to run a benchmark arm (vanilla or
candidate) and emit a comparable scalar scorecard, so Phase B can measure during cutover.

**Exit criteria:** `make`-able / scripted command runs one arm of one benchmark end-to-end
against a chosen `--mem0-host` and writes a JSON scorecard (scores + run metadata, no memory
bodies) to a gitignored path; a diff tool compares two scorecards and reports deltas.

### Task A1: Scorecard schema + writer

**Objective:** Define the content-free scorecard contract every arm emits.

**Files:**
- Create: `evaluation/benchmarks/common/scorecard.py`
- Test: `tests/evaluation/test_scorecard.py`

**Step 1: Write failing test**

```python
def test_scorecard_round_trip(tmp_path):
    from benchmarks.common.scorecard import Scorecard
    card = Scorecard(
        benchmark="locomo", arm="vanilla", project_name="p",
        mem0_host="http://localhost:8888", model="m",
        scores={"f1": 0.5}, question_count=40,
    )
    path = card.write(tmp_path)
    import json
    data = json.loads(path.read_text())
    assert data["schema_version"] == 1
    assert data["arm"] == "vanilla"
    assert "memory_bodies" not in data  # content-free guarantee
```

**Step 2: Run test to verify failure**

Run: `pytest tests/evaluation/test_scorecard.py -v`
Expected: FAIL — `ModuleNotFoundError: benchmarks.common.scorecard`

**Step 3: Write minimal implementation**

```python
# evaluation/benchmarks/common/scorecard.py
"""Content-free benchmark scorecard: scalar scores + run metadata only."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Scorecard:
    benchmark: str
    arm: str  # "vanilla" | "candidate"
    project_name: str
    mem0_host: str
    model: str
    scores: dict[str, float]
    question_count: int
    schema_version: int = field(default=1, init=False)

    def write(self, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{self.benchmark}-{self.arm}-scorecard.json"
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True))
        return path
```

**Step 4: Run test to verify pass**

Run: `pytest tests/evaluation/test_scorecard.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add evaluation/benchmarks/common/scorecard.py tests/evaluation/test_scorecard.py
git commit -m "feat(eval): content-free benchmark scorecard schema"
```

### Task A2: Scorecard diff tool

**Objective:** Compare two scorecards (vanilla vs candidate) and report per-metric deltas.

**Files:**
- Create: `evaluation/benchmarks/common/scorecard_diff.py`
- Test: `tests/evaluation/test_scorecard_diff.py`

**Step 1: Write failing test**

```python
def test_diff_reports_delta(tmp_path):
    from benchmarks.common.scorecard import Scorecard
    from benchmarks.common.scorecard_diff import diff
    v = Scorecard("locomo", "vanilla", "p", "h", "m", {"f1": 0.5}, 40)
    c = Scorecard("locomo", "candidate", "p", "h", "m", {"f1": 0.6}, 40)
    report = diff(v, c)
    assert report["scores"]["f1"]["delta"] == 0.1
    assert report["regressed"] is False
```

**Step 2: Run test to verify failure**

Run: `pytest tests/evaluation/test_scorecard_diff.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# evaluation/benchmarks/common/scorecard_diff.py
"""Compare a vanilla and candidate scorecard; flag regressions."""
from __future__ import annotations

from typing import Any

from benchmarks.common.scorecard import Scorecard


def diff(vanilla: Scorecard, candidate: Scorecard) -> dict[str, Any]:
    assert vanilla.benchmark == candidate.benchmark
    scores: dict[str, Any] = {}
    regressed = False
    for key, v_val in vanilla.scores.items():
        c_val = candidate.scores.get(key)
        delta = None if c_val is None else round(c_val - v_val, 6)
        if delta is not None and delta < 0:
            regressed = True
        scores[key] = {"vanilla": v_val, "candidate": c_val, "delta": delta}
    return {
        "schema_version": 1,
        "benchmark": vanilla.benchmark,
        "scores": scores,
        "regressed": regressed,
    }
```

**Step 4: Run test to verify pass**

Run: `pytest tests/evaluation/test_scorecard_diff.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add evaluation/benchmarks/common/scorecard_diff.py tests/evaluation/test_scorecard_diff.py
git commit -m "feat(eval): scorecard diff tool with regression flag"
```

### Task A3: Arm-runner wrapper (one benchmark, one host)

**Objective:** A single entry point that runs one benchmark arm against a chosen
`--mem0-host` and emits a Scorecard.

**Files:**
- Create: `evaluation/benchmarks/run_arm.py`
- Test: `tests/evaluation/test_run_arm.py`

**Step 1: Write failing test**

```python
def test_arm_command_construction():
    from benchmarks.run_arm import build_command
    cmd = build_command(
        benchmark="locomo", arm="vanilla", project_name="p",
        mem0_host="http://localhost:8888", model="m",
    )
    assert "--backend" in cmd and "oss" in cmd
    assert "--mem0-host" in cmd and "http://localhost:8888" in cmd
    assert "benchmarks.locomo.run" in cmd
```

**Step 2: Run test to verify failure**

Run: `pytest tests/evaluation/test_run_arm.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# evaluation/benchmarks/run_arm.py
"""Run one benchmark arm (vanilla or candidate) and emit a content-free scorecard."""
from __future__ import annotations

_RUNNERS = {
    "locomo": "benchmarks.locomo.run",
    "longmemeval": "benchmarks.longmemeval.run",
    "beam": "benchmarks.beam.run",
}


def build_command(*, benchmark: str, arm: str, project_name: str,
                  mem0_host: str, model: str) -> list[str]:
    if benchmark not in _RUNNERS:
        raise ValueError(f"unknown benchmark: {benchmark}")
    if arm not in ("vanilla", "candidate"):
        raise ValueError(f"unknown arm: {arm}")
    return [
        "python", "-m", _RUNNERS[benchmark],
        "--backend", "oss",
        "--mem0-host", mem0_host,
        "--project-name", f"{project_name}-{arm}",
    ]
```

(Scorecard emission parses the runner's own scalar summary; wired in A4.)

**Step 4: Run test to verify pass**

Run: `pytest tests/evaluation/test_run_arm.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add evaluation/benchmarks/run_arm.py tests/evaluation/test_run_arm.py
git commit -m "feat(eval): arm-runner command builder (mem0-host arm switch)"
```

### Task A4: Smoke-run the vanilla arm on LOCOMO

**Objective:** Prove the instrument end-to-end against the legacy server before cutover.

**Files:**
- Modify: `evaluation/benchmarks/run_arm.py` (parse runner output → Scorecard)

**Step 1:** Confirm legacy server healthy at `http://localhost:8888`.
Run: `curl -sf http://localhost:8888/health || echo "legacy not on 8888"`

**Step 2:** Run the vanilla arm (background — LLM-bound).
Run: `python -m benchmarks.run_arm --benchmark locomo --arm vanilla --project-name sa12 --mem0-host http://localhost:8888`
Expected: writes `locomo-vanilla-scorecard.json` with `arm=vanilla`, scalar scores.

**Step 3:** Verify scorecard is content-free.
Run: `python -c "import json; d=json.load(open('locomo-vanilla-scorecard.json')); assert 'memory_bodies' not in d; print(d['arm'], d['question_count'])"`

**Step 4: Commit**

```bash
git add evaluation/benchmarks/run_arm.py
git commit -m "feat(eval): parse runner summary into scorecard; vanilla LOCOMO smoke"
```

> **Phase A gate:** vanilla LOCOMO scorecard exists, is content-free, and is reproducible.
> LongMemEval/BEAM arms reuse the same wrapper; add them in Phase B if the promotion
> measurement needs broader coverage.

---

# PHASE B — Promote + measure (cutover, fix-forward)  [SKELETON]

**Phase goal:** Build the cutover machinery (re-scoped gates), retarget the harness from
legacy (8888) to candidate (28889), and capture the candidate scorecard during promotion.

**Skeleton tasks (expand on entry):**
- B1: Re-scope `REQUIRED_GATES` — remove `legacy_rollback_image_provenance`; keep the rest.
- B2: Implement each retained gate check in `cutover.py` (rest_parity, typescript_parity,
  shadow_end_to_end, fresh_session_behavior, cross_store_snapshot_provenance,
  live_candidate_reconciliation, canonical_state_path_attestation, benchmark_acceptance*).
  *`benchmark_acceptance` becomes REPORT-ONLY (records the diff, never blocks) per the
  promote-first override.
- B3: Implement `create_cutover_record` + a real CLI (`argparse`).
- B4: Build the harness-retargeting tool (back up bridge configs; rewrite endpoint
  8888→28889; disable/retain `mem0-arch.service`, enable `mem0_0-arch.service`).
- B5: Execute cutover; run candidate LOCOMO arm; `scorecard_diff` vanilla vs candidate.
- B6: Fix-forward on any regression the diff flags (feeds Phase D).

# PHASE C — Harness integrations  [SKELETON]

**Phase goal:** mem0_0 as a memory provider for Claude Code, Codex, oh-my-pi, Hermes.

**Skeleton tasks (expand on entry):**
- C1: Claude Code — already targeted by `integrations/mem0-plugin`; verify it points at the
  candidate endpoint post-cutover; add a dedicated provider config if the contract differs.
- C2: Codex — same `mem0-plugin` surface; verify/extend.
- C3: oh-my-pi (SA-09) — implement per the existing SA-09 contract (six-tool discovery,
  scoped CRUD, search-before-answer, durable-write, persistence, exact cleanup, reconcile).
- C4: Hermes Agent — NEW `integrations/hermes/` directory (none exists; only a docs page).
  Follow the MCP-server + lifecycle-hooks pattern; register in 5× `marketplace.json`,
  `docs/docs.json`, `docs/llms.txt`, and a new CI workflow pair.

# PHASE D — Fix-forward  [SKELETON]

**Phase goal:** Address every regression/bug surfaced by Phase B measurement and Phase C
integration testing. No revert; fix in place and re-measure.

# PHASE E — OSS-ready / merge / push  [SKELETON]

**Phase goal:** Re-run full suites (SA-10), obtain merge decision (SA-11), clean workspace,
land on the appropriate branch. Note: PR #1 was already ff-merged into fork `main`
2026-08-10, so "merge" scope must be re-confirmed on entry (new PR vs upstream).

---

## Ledger rows to add/change when this plan is adopted

- Re-scope **SA-07** → instrumentation (Phase A), status READY.
- Re-scope **SA-12** → fix-forward cutover, rollback gate waived (override recorded).
- Add **SA-13** (Claude Code), **SA-14** (Codex), **SA-15** (Hermes) harness integrations;
  SA-09 already covers oh-my-pi.
- Re-confirm **SA-10/SA-11** merge scope in Phase E.
