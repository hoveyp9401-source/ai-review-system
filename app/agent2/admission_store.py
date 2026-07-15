from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from copy import deepcopy
import json
import threading
from typing import Iterator, Mapping, Protocol

from app.agent2.business.contracts import BusinessCommandError


class AdmissionTicketLease(Protocol):
    ticket_id: str

    def consume(self, receipt_ref: str) -> None: ...


class AdmissionTicketStore(Protocol):
    def acquire(
        self,
        ticket: Mapping[str, object],
    ) -> AbstractContextManager[AdmissionTicketLease]: ...


class InMemoryAdmissionTicketLease:
    def __init__(self, store: "InMemoryAdmissionTicketStore", ticket_id: str) -> None:
        self._store = store
        self.ticket_id = ticket_id
        self._consumed = False

    def consume(self, receipt_ref: str) -> None:
        if self._consumed:
            raise BusinessCommandError(
                "admission_ticket_already_consumed",
                "admission",
                "ticket lease was already consumed",
            )
        record = self._store._records[self.ticket_id]
        if str(record.get("ticket_status") or "") != "issued":
            raise BusinessCommandError(
                "admission_ticket_inactive", "admission", "ticket is not issued"
            )
        record["ticket_status"] = "consumed"
        record["consumed_receipt_ref"] = receipt_ref
        self._consumed = True


class InMemoryAdmissionTicketStore:
    """Authoritative single-consumer Ticket store for deterministic tests/replay."""

    def __init__(self, tickets: tuple[Mapping[str, object], ...] = ()) -> None:
        self._records: dict[str, dict] = {}
        self._lock = threading.RLock()
        for ticket in tickets:
            self.issue(ticket)

    def issue(self, ticket: Mapping[str, object]) -> None:
        payload = deepcopy(dict(ticket))
        ticket_id = str(payload.get("ticket_id") or "")
        if not ticket_id:
            raise ValueError("admission ticket requires ticket_id")
        with self._lock:
            existing = self._records.get(ticket_id)
            if existing is not None and _canonical(existing) != _canonical(payload):
                raise ValueError("ticket id collision with different claims")
            self._records[ticket_id] = payload

    @contextmanager
    def acquire(
        self,
        ticket: Mapping[str, object],
    ) -> Iterator[InMemoryAdmissionTicketLease]:
        ticket_id = str(ticket.get("ticket_id") or "")
        self._lock.acquire()
        try:
            stored = self._records.get(ticket_id)
            if stored is None:
                raise BusinessCommandError(
                    "admission_ticket_not_found",
                    "admission",
                    "authoritative admission ticket does not exist",
                )
            if str(stored.get("ticket_status") or "") != "issued":
                raise BusinessCommandError(
                    "admission_ticket_inactive",
                    "admission",
                    "authoritative admission ticket is not issued",
                )
            if _canonical(stored) != _canonical(dict(ticket)):
                raise BusinessCommandError(
                    "admission_ticket_authority_mismatch",
                    "admission",
                    "command ticket differs from authoritative ticket",
                )
            yield InMemoryAdmissionTicketLease(self, ticket_id)
        finally:
            self._lock.release()

    def status(self, ticket_id: str) -> str:
        with self._lock:
            record = self._records.get(ticket_id)
            return str(record.get("ticket_status") or "") if record else "missing"


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
