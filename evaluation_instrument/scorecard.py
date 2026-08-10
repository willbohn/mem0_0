"""Content-free benchmark scorecard: scalar scores + run metadata only.

Never carries memory bodies, scope values, or credentials — only aggregate
scores and the run metadata needed to reproduce/compare an arm.
"""
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
