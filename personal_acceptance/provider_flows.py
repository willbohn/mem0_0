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

from deployment.mem0_0_arch.live_verify import (
    DEFAULT_API_URL,
    MAX_SCOPE_RESULTS,
    ConfigurationError,
    LiveVerifier,
    VerificationError,
)
from personal_acceptance.budget import BudgetExceeded, BudgetLedger

PLANNED_PROVIDER_MUTATIONS = 32


class AcceptanceFlows(LiveVerifier):
    """Run the five provider-tier families inside one disposable REST scope.

    Callers own the ledger's wall-clock gate: construct with a ``ledger`` that
    has already had ``start_provider_clock(...)`` called on it (matching how
    ``register_family`` is caller-owned elsewhere in the suite). Without that
    call, ``check_provider_clock`` is a documented no-op and the wall-clock
    cap silently never fires during this flow's writes.
    """

    def __init__(
        self,
        *,
        ledger: BudgetLedger,
        clock: Callable[[], float] = time.monotonic,
        planned_mutations: int = PLANNED_PROVIDER_MUTATIONS,
        **kwargs: Any,
    ) -> None:
        api_url = str(kwargs.get("api_url", DEFAULT_API_URL)).rstrip("/")
        if api_url != DEFAULT_API_URL:
            raise ConfigurationError("personal acceptance requires the canonical shadow endpoint")
        super().__init__(**kwargs)
        self.ledger = ledger
        self.clock = clock
        # Every family gets an isolated scope (see _family_scope) so its searches
        # are not polluted by other families' memories. Cleanup must sweep all of
        # them, so we track each one here.
        self._family_scopes: list[dict[str, str]] = []
        self.planned_mutations = planned_mutations
        # Replace the base flow's report shape (kyoto/osaka-specific keys) with
        # this flow's own checks/counts, carrying over what __init__ already computed.
        self.report = {
            "ok": False,
            "checks": {
                "auth_file_private": self.report["checks"]["auth_file_private"],
                "scope_complete": self.report["checks"]["scope_complete"],
                "budget_ok": True,
                "bounded_write_contract": False,
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

    def _family_scope(self, family: str) -> dict[str, str]:
        """Derive an isolated scope for one family so its searches only see its own memories.

        All five families otherwise share the single disposable scope, and the
        search path returns every scoped memory (top_k is far larger than the
        handful in scope) -- so a later family's recall would be polluted by
        earlier families' memories. Giving each family its own scope keeps every
        behavioral assertion narrow and independent.
        """
        base = self.scope["user_id"].replace("live-user-", "", 1)
        scope = {
            "user_id": f"live-user-{base}-{family}",
            "agent_id": f"live-agent-{base}-{family}",
            "machine_id": f"live-machine-{base}-{family}",
            "project_id": f"live-project-{base}-{family}",
            "data_class": f"live-synthetic-{base}-{family}",
        }
        self._family_scopes.append(scope)
        return scope

    def _discover_scope_ids(self) -> set[str]:
        # Sweep every family's isolated scope, not just the last one assigned to
        # self.scope, so cleanup drains all synthetic memories this run created.
        discovered: set[str] = set()
        for fam_scope in self._family_scopes:
            saved = self.scope
            self.scope = fam_scope
            try:
                found = super()._discover_scope_ids()
                discovered |= found
            finally:
                self.scope = saved
        return discovered

    def _purge_history_rows(self, memory_ids: set[str]) -> tuple[int, int]:
        # The parent purges one session_scope derived from self.scope; with
        # per-family scopes we must purge each family's session_scope.
        total_history = 0
        total_messages = 0
        for fam_scope in self._family_scopes:
            saved = self.scope
            self.scope = fam_scope
            try:
                h, m = super()._purge_history_rows(memory_ids)
                total_history += h
                total_messages += m
            finally:
                self.scope = saved
        return total_history, total_messages

    def _check_provider_clock(self) -> None:
        try:
            self.ledger.check_provider_clock(self.clock())
        except BudgetExceeded:
            self.report["checks"]["budget_ok"] = False
            raise

    def _record_mutation(self, kind: str) -> None:
        try:
            self.ledger.record_mutation(kind)
        except BudgetExceeded:
            self.report["checks"]["budget_ok"] = False
            raise

    def _record_infer_write(self) -> None:
        try:
            self.ledger.record_infer_write()
        except BudgetExceeded:
            self.report["checks"]["budget_ok"] = False
            raise

    def _reserve_mutations(self) -> None:
        try:
            self.ledger.reserve_mutations(self.planned_mutations)
        except BudgetExceeded:
            self.report["checks"]["budget_ok"] = False
            raise

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        is_cleanup_request = method == "DELETE" or (method == "GET" and path == "/memories")
        if not is_cleanup_request:
            self._check_provider_clock()
        if method == "DELETE":
            self._record_mutation("delete")
        response = super()._request(method, path, body=body, query=query)
        if not is_cleanup_request:
            self._check_provider_clock()
        return response

    def _infer_add(self, content: str, *, created_at: str) -> str:
        # Budget is checked and charged BEFORE the write, mirroring the
        # ledger's own "every check runs before the action" contract.
        self._check_provider_clock()
        self._record_infer_write()
        # One extraction may insert a new memory and update one predecessor.
        # Charge that fail-closed ceiling before asking the server to work.
        self._record_mutation("update")
        response = self._request(
            "POST",
            "/memories",
            body={
                "messages": [{"role": "user", "content": content}],
                "user_id": self.scope["user_id"],
                "agent_id": self.scope["agent_id"],
                "metadata": {**self.scope, "created_at": created_at},
                "infer": True,
                "max_extractions": 1,
                "max_mutations": 2,
            },
        )
        rows = self._results(response)
        if not rows:
            raise VerificationError("add produced no result")
        self.report["counts"]["created"] += len(rows)
        # The local model may legitimately return one row (the common case) or,
        # under stochastic extraction, merge/over-extract into a different count.
        # SA-04 validates behavior via search recall, not exact row counts, so
        # accept any non-empty result and use the first id for scope cleanup.
        if len(rows) < 1:
            raise VerificationError("synthetic infer add must produce at least one memory")
        memory_id = rows[0].get("id")
        if not isinstance(memory_id, str):
            raise VerificationError("add produced an invalid synthetic memory identifier")
        return memory_id

    def _raw_add(self, content: str, *, created_at: str) -> str:
        self._check_provider_clock()
        self._record_mutation("write")
        response = self._request(
            "POST",
            "/memories",
            body={
                "messages": [{"role": "user", "content": content}],
                "user_id": self.scope["user_id"],
                "agent_id": self.scope["agent_id"],
                "metadata": {**self.scope, "created_at": created_at},
                "infer": False,
                "max_mutations": 1,
            },
        )
        rows = self._results(response)
        if not rows:
            raise VerificationError("raw add produced no result")
        self.report["counts"]["created"] += len(rows)
        if len(rows) < 1:
            raise VerificationError("synthetic raw add must produce at least one memory")
        memory_id = rows[0].get("id")
        if not isinstance(memory_id, str):
            raise VerificationError("raw add produced an invalid synthetic memory identifier")
        return memory_id

    def _supersession_flow(self) -> None:
        self.scope = self._family_scope("supersession")
        self._infer_add("City preference is Kumamoto.", created_at="2025-01-01T00:00:00Z")
        self._infer_add("City preference changed to Matsue.", created_at="2026-01-01T00:00:00Z")

        current_texts = self._memory_texts(self._search("What is the current city preference?"))
        # Current recall must return the Matsue memory and NOT a bare, still-active
        # "City preference is Kumamoto." The model may legitimately phrase the
        # current memory as a transition ("...changed from Kumamoto to Matsue."),
        # which includes the old value by design -- so we assert the *current set*
        # is exactly the Matsue memory, not that "kumamoto" is absent from text.
        current_ok = (
            len(current_texts) == 1
            and any("matsue" in text.casefold() for text in current_texts)
            and not any(text.casefold().strip() == "city preference is kumamoto." for text in current_texts)
        )
        historical_texts = self._memory_texts(self._search("What was the city preference on 2025-06-01?"))
        historical_ok = any("kumamoto" in text.casefold() for text in historical_texts)

        self.report["checks"]["supersession_live_ok"] = current_ok and historical_ok
        if not self.report["checks"]["supersession_live_ok"]:
            raise VerificationError("supersession recall did not satisfy the synthetic assertion")

    def _corroboration_flow(self) -> None:
        self.scope = self._family_scope("corroboration")
        self._infer_add("Beverage preference is oolong tea.", created_at="2025-02-01T00:00:00Z")
        self._infer_add("Beverage preference is oolong tea.", created_at="2025-02-02T00:00:00Z")

        current_rows = self._search("What is the current beverage preference?")
        current_texts = self._memory_texts(current_rows)
        # Corroboration: the same fact repeated must not produce a competing
        # active state. The model may merge to one memory OR keep two rows that
        # both carry the same value -- both satisfy the contract. Assert the
        # current set is non-empty and every current memory is about oolong.
        self.report["checks"]["corroboration_live_ok"] = (
            len(current_texts) >= 1 and all("oolong" in text.casefold() for text in current_texts)
        )
        if not self.report["checks"]["corroboration_live_ok"]:
            raise VerificationError("corroboration recall did not satisfy the synthetic assertion")

    def _dispute_flow(self) -> None:
        self.scope = self._family_scope("dispute")
        self._infer_add("Snack preference is senbei.", created_at="2025-03-01T00:00:00Z")
        self._infer_add("Snack preference is castella.", created_at="2025-03-02T00:00:00Z")

        current_rows = self._search("What is the current snack preference?")
        current_texts = self._memory_texts(current_rows)
        # Both conflicting facts must be retrievable as current -- not zero (a
        # dropped-memory regression) and not collapsed into one canonical answer
        # that loses a side. Assert both "senbei" and "castella" appear across the
        # current set (the model may keep two rows, or one row naming both).
        both_conflicting_present = (
            len(current_texts) >= 1
            and any("senbei" in text.casefold() for text in current_texts)
            and any("castella" in text.casefold() for text in current_texts)
        )

        audit = self._request("POST", "/memories/audit", body={"filters": dict(self.scope), "limit": MAX_SCOPE_RESULTS})
        audit_completed = isinstance(audit, dict) and isinstance(audit.get("scanned"), int)

        self.report["checks"]["dispute_live_ok"] = both_conflicting_present and audit_completed
        if not self.report["checks"]["dispute_live_ok"]:
            raise VerificationError("dispute recall did not satisfy the synthetic assertion")

    def _reversion_flow(self) -> None:
        self.scope = self._family_scope("reversion")
        self._infer_add("Tea preference is genmaicha.", created_at="2025-04-01T00:00:00Z")
        self._infer_add("Tea preference changed to houjicha.", created_at="2025-04-02T00:00:00Z")
        self._infer_add("Tea preference reverted to genmaicha.", created_at="2025-04-03T00:00:00Z")

        current_texts = self._memory_texts(self._search("What is the current tea preference?"))
        # Current recall must be exactly the genmaicha memory. The model may phrase
        # it as a transition ("...reverted from houjicha to genmaicha.") which
        # includes the old value by design, so assert the *current set* is the
        # genmaicha memory, not that "houjicha" is absent from text.
        self.report["checks"]["reversion_live_ok"] = (
            len(current_texts) == 1
            and any("genmaicha" in text.casefold() for text in current_texts)
            and not any(text.casefold().strip() == "tea preference is houjicha." for text in current_texts)
        )
        if not self.report["checks"]["reversion_live_ok"]:
            raise VerificationError("reversion recall did not satisfy the synthetic assertion")

    def _direct_vs_lifecycle_flow(self) -> None:
        self.scope = self._family_scope("direct_vs_lifecycle")
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
        self._reserve_mutations()
        schema = self._request("GET", "/openapi.json")
        try:
            properties = schema["components"]["schemas"]["MemoryCreate"]["properties"]
            max_extractions = properties["max_extractions"]
            max_mutations = properties["max_mutations"]
        except (KeyError, TypeError):
            raise VerificationError("shadow does not advertise the bounded-write contracts")
        for contract in (max_extractions, max_mutations):
            variants = [contract, *contract.get("anyOf", [])]
            if not any(item.get("minimum") == 1 and item.get("type") == "integer" for item in variants):
                raise VerificationError("shadow advertises an invalid bounded-write contract")
        self.report["checks"]["bounded_write_contract"] = True
        self._supersession_flow()
        self._corroboration_flow()
        self._dispute_flow()
        self._reversion_flow()
        self._direct_vs_lifecycle_flow()

    def run(self) -> dict[str, Any]:
        report = super().run()
        try:
            self._check_provider_clock()
        except BudgetExceeded:
            pass
        report["ok"] = all(report["checks"].values())
        return report
