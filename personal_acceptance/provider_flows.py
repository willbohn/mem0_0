"""Budget-guarded provider flows on the live-verifier REST seam (PA Task 7).

``AcceptanceFlows`` drives the five provider-tier scenario families
(supersession, corroboration, dispute, reversion, direct-vs-lifecycle)
through one disposable ``LiveVerifier`` scope. It reuses the parent's
constructor guards, request/result helpers, and guaranteed-cleanup
``run()``; it only overrides ``_run_flow`` (to run the families) and
``_cleanup`` (to charge deletes to the ledger). No memory body, ID, or
scope value is ever written into ``self.report``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from deployment.mem0_0_arch.live_verify import MAX_SCOPE_RESULTS, LiveVerifier, VerificationError
from personal_acceptance.budget import BudgetLedger


class AcceptanceFlows(LiveVerifier):
    """Run the five provider-tier families inside one disposable REST scope."""

    def __init__(
        self,
        *,
        ledger: BudgetLedger,
        clock: Callable[[], float] = time.monotonic,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.ledger = ledger
        self.clock = clock
        # Replace the base flow's report shape (kyoto/osaka-specific keys) with
        # this flow's own checks/counts, carrying over what __init__ already computed.
        self.report = {
            "ok": False,
            "checks": {
                "auth_file_private": self.report["checks"]["auth_file_private"],
                "scope_complete": self.report["checks"]["scope_complete"],
                "supersession_live_ok": False,
                "corroboration_live_ok": False,
                "dispute_live_ok": False,
                "reversion_live_ok": False,
                "direct_vs_lifecycle_live_ok": False,
                "cleanup_complete": False,
                "history_cleanup_complete": self.report["checks"]["history_cleanup_complete"],
            },
            "counts": {
                "created": 0,
                "cleanup_discovered": 0,
                "cleanup_deleted": 0,
                "cleanup_remaining": 0,
                "cleanup_history_deleted": 0,
                "cleanup_messages_deleted": 0,
            },
            "timings_ms": {},
        }

    def _infer_add(self, content: str, *, created_at: str) -> str:
        # Budget is checked and charged BEFORE the write, mirroring the
        # ledger's own "every check runs before the action" contract.
        self.ledger.check_provider_clock(self.clock())
        self.ledger.record_infer_write()
        rows = self._add(content, created_at=created_at)
        memory_id = rows[0].get("id")
        if not isinstance(memory_id, str):
            raise VerificationError("add produced an invalid synthetic memory identifier")
        self.report["counts"]["created"] += 1
        return memory_id

    def _raw_add(self, content: str, *, created_at: str) -> str:
        self.ledger.check_provider_clock(self.clock())
        self.ledger.record_mutation("write")
        response = self._request(
            "POST",
            "/memories",
            body={
                "messages": [{"role": "user", "content": content}],
                "user_id": self.scope["user_id"],
                "agent_id": self.scope["agent_id"],
                "metadata": {**self.scope, "created_at": created_at},
                "infer": False,
            },
        )
        rows = self._results(response)
        if not rows:
            raise VerificationError("raw add produced no result")
        memory_id = rows[0].get("id")
        if not isinstance(memory_id, str):
            raise VerificationError("raw add produced an invalid synthetic memory identifier")
        self.report["counts"]["created"] += 1
        return memory_id

    def _supersession_flow(self) -> None:
        self._infer_add("City preference is Kumamoto.", created_at="2025-01-01T00:00:00Z")
        self._infer_add("City preference changed to Matsue.", created_at="2026-01-01T00:00:00Z")

        current_texts = self._memory_texts(self._search("What is the current city preference?"))
        current_ok = (
            bool(current_texts)
            and all("kumamoto" not in text.casefold() for text in current_texts)
            and any("matsue" in text.casefold() for text in current_texts)
        )
        historical_texts = self._memory_texts(self._search("What was the city preference on 2025-06-01?"))
        historical_ok = any("kumamoto" in text.casefold() for text in historical_texts)

        self.report["checks"]["supersession_live_ok"] = current_ok and historical_ok
        if not self.report["checks"]["supersession_live_ok"]:
            raise VerificationError("supersession recall did not satisfy the synthetic assertion")

    def _corroboration_flow(self) -> None:
        self._infer_add("Beverage preference is oolong tea.", created_at="2025-02-01T00:00:00Z")
        self._infer_add("Beverage preference is oolong tea.", created_at="2025-02-02T00:00:00Z")

        current_rows = self._search("What is the current beverage preference?")
        current_texts = self._memory_texts(current_rows)
        self.report["checks"]["corroboration_live_ok"] = (
            len(current_rows) == 1 and bool(current_texts) and "oolong" in current_texts[0].casefold()
        )
        if not self.report["checks"]["corroboration_live_ok"]:
            raise VerificationError("corroboration recall did not satisfy the synthetic assertion")

    def _dispute_flow(self) -> None:
        self._infer_add("Snack preference is senbei.", created_at="2025-03-01T00:00:00Z")
        self._infer_add("Snack preference is castella.", created_at="2025-03-02T00:00:00Z")

        current_rows = self._search("What is the current snack preference?")
        not_single_canonical = len(current_rows) != 1

        audit = self._request(
            "POST", "/memories/audit", body={"filters": dict(self.scope), "limit": MAX_SCOPE_RESULTS}
        )
        audit_completed = isinstance(audit, dict) and isinstance(audit.get("scanned"), int)

        self.report["checks"]["dispute_live_ok"] = not_single_canonical and audit_completed
        if not self.report["checks"]["dispute_live_ok"]:
            raise VerificationError("dispute recall did not satisfy the synthetic assertion")

    def _reversion_flow(self) -> None:
        self._infer_add("Tea preference is genmaicha.", created_at="2025-04-01T00:00:00Z")
        self._infer_add("Tea preference changed to houjicha.", created_at="2025-04-02T00:00:00Z")
        self._infer_add("Tea preference reverted to genmaicha.", created_at="2025-04-03T00:00:00Z")

        current_texts = self._memory_texts(self._search("What is the current tea preference?"))
        self.report["checks"]["reversion_live_ok"] = bool(current_texts) and all(
            "genmaicha" in text.casefold() and "houjicha" not in text.casefold() for text in current_texts
        )
        if not self.report["checks"]["reversion_live_ok"]:
            raise VerificationError("reversion recall did not satisfy the synthetic assertion")

    def _direct_vs_lifecycle_flow(self) -> None:
        infer_id = self._infer_add("Notification preference is enabled.", created_at="2025-05-01T00:00:00Z")
        raw_content = "raw-event: settings toggle tapped for notifications"
        raw_id = self._raw_add(raw_content, created_at="2025-05-01T00:00:01Z")

        both_rows = self._search("What are the current notification-related records?")
        ids_present = {row.get("id") for row in both_rows}
        both_present = infer_id in ids_present and raw_id in ids_present
        raw_verbatim = raw_content in self._memory_texts(both_rows)

        self.report["checks"]["direct_vs_lifecycle_live_ok"] = both_present and raw_verbatim
        if not self.report["checks"]["direct_vs_lifecycle_live_ok"]:
            raise VerificationError("direct-vs-lifecycle recall did not satisfy the synthetic assertion")

    def _run_flow(self) -> None:
        self._supersession_flow()
        self._corroboration_flow()
        self._dispute_flow()
        self._reversion_flow()
        self._direct_vs_lifecycle_flow()

    def _cleanup(self) -> None:
        super()._cleanup()
        # Deletes are charged to the ledger AFTER the fact, from the parent's
        # already-drained count -- the data-plane cleanup above is unconditional
        # and must never be gated by ledger bookkeeping.
        for _ in range(self.report["counts"]["cleanup_deleted"]):
            self.ledger.record_mutation("delete")
