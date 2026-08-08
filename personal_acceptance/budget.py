"""Hard budget enforcement for the bounded personal local acceptance suite."""

from __future__ import annotations

_MUTATION_KINDS = frozenset({"write", "update", "delete"})


class BudgetExceeded(RuntimeError):
    """Raised before any action that would exceed a suite budget."""

    def __init__(self, limit: str, message: str):
        super().__init__(message)
        self.limit = limit


class BudgetLedger:
    """Tracks the design's hard budgets; every check runs before the action."""

    def __init__(
        self,
        *,
        max_families: int = 24,
        max_mutations: int = 120,
        max_infer_writes: int = 48,
        provider_wall_clock_seconds: float = 6000.0,
    ) -> None:
        self.max_families = int(max_families)
        self.max_mutations = int(max_mutations)
        self.max_infer_writes = int(max_infer_writes)
        self.provider_wall_clock_seconds = float(provider_wall_clock_seconds)
        self._families: set[str] = set()
        self._mutations = 0
        self._reserved_mutations = 0
        self._infer_writes = 0
        self._provider_started: float | None = None
        self._provider_elapsed = 0.0

    def register_family(self, name: str) -> None:
        if name in self._families:
            raise ValueError(f"family already registered: {name}")
        if len(self._families) + 1 > self.max_families:
            raise BudgetExceeded("families", f"family budget of {self.max_families} would be exceeded")
        self._families.add(name)

    def record_mutation(self, kind: str) -> None:
        if kind not in _MUTATION_KINDS:
            raise ValueError(f"unknown mutation kind: {kind}")
        if self._reserved_mutations:
            self._reserved_mutations -= 1
        elif self._mutations + 1 > self.max_mutations:
            raise BudgetExceeded("mutations", f"mutation budget of {self.max_mutations} would be exceeded")
        self._mutations += 1

    def reserve_mutations(self, count: int) -> None:
        """Reserve capacity for a bounded write-and-cleanup plan before it starts."""
        count = int(count)
        if count < 0:
            raise ValueError("mutation reservation must be non-negative")
        if self._mutations + self._reserved_mutations + count > self.max_mutations:
            raise BudgetExceeded("mutations", f"mutation budget of {self.max_mutations} would be exceeded")
        self._reserved_mutations += count

    def record_infer_write(self) -> None:
        if self._infer_writes + 1 > self.max_infer_writes:
            raise BudgetExceeded("infer_writes", f"infer=true budget of {self.max_infer_writes} would be exceeded")
        self.record_mutation("write")
        self._infer_writes += 1

    def start_provider_clock(self, now: float) -> None:
        self._provider_started = float(now)

    def check_provider_clock(self, now: float) -> None:
        if self._provider_started is None:
            return
        self._provider_elapsed = float(now) - self._provider_started
        if self._provider_elapsed > self.provider_wall_clock_seconds:
            raise BudgetExceeded(
                "provider_wall_clock",
                f"provider wall-clock cap of {self.provider_wall_clock_seconds}s exceeded; the run fails",
            )

    def snapshot(self) -> dict:
        return {
            "families": len(self._families),
            "mutations": self._mutations,
            "infer_writes": self._infer_writes,
            "provider_elapsed_seconds": float(self._provider_elapsed),
        }
