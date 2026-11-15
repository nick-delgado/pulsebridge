"""The messages log (D7, D12, D15): one row per cuff send attempt and per FHIR transaction.

Three sources, merged newest first:

1. **Send attempts**, from the send-attempt AuditEvents in the **audit** FHIR store (D15),
   read with T31's ``parse_send_attempt``. Each becomes a ``cuff-send`` row with the
   commit-level acknowledgment (D7: the HL7v2 ACK the store returned, or none). An AuditEvent
   that is ours but cannot be read becomes an ``unreadable-send-attempt`` row and is counted,
   never hidden (T31); one that is not ours (``NotASendAttempt``) is skipped. An attempt for
   another patient or another HL7v2 store is skipped too (the patient pin).
2. **Stored messages with no AuditEvent** ("store-only"), from the HL7v2 store: for example a
   message sent straight to the adapter, or a send whose AuditEvent could not be written.
3. **FHIR transactions**, from the ``Provenance`` each ingest Bundle writes (README, open
   question 4): Provenances with the same ``recorded`` time and the same assembler agent were
   written by one ``executeBundle``, so they form one row. The HTTP status of that call was not
   recorded anywhere, so it is ``null``.

**Joining an accepted attempt to its stored message** (the application-level outcome, D7):

- HTTPS: by the name the store returned in the ingest response (T31 ``message_name``);
- MLLP: by the name cuff-sim **derived** (T31 ``derived_message_name``, T33), which is a
  computation, not the store's word. It is never trusted unconfirmed: the proxy ``get``s that
  name from the store. Found: the row shows the labels and ``messageLookup: "confirmed"``. Not
  found: ``messageLookup: "not-found"`` and no outcome, because the derived rule (spike S5,
  observed once) was wrong for this message. A control ID is never used as a key: T07's samples
  share them.

Either way the name must lie inside the configured HL7v2 store, or nothing is fetched. The
application-level outcome comes from the stored message's labels (T12): ``application_outcome``
``accepted`` or ``error`` with ``reason``; no label yet is ``pending`` (mapper not run, failed,
or dead-lettered). A stored message's commit-level ACK is ``AA`` by definition.

**Order: newest first by send time** (the acceptance criterion). An attempt row sorts by the
attempt's send time (T31's ``period.start``), a store-only row by the message's ``createTime``
(the store's clock; a sample's MSH-7 is a fixed date years earlier, so it would misplace the
row), a transaction row by its Provenance ``recorded`` time. Ties break on the row id,
descending. The audit store can only be searched and sorted by ``recorded`` (R4's ``date`` on
AuditEvent), which is never before the send time and at most ``SEND_RECORD_SKEW`` after it, so
the proxy searches by ``recorded`` with that margin and orders by send time itself.

**Paging.** A cursor names a sort-key instant and the ids already shown at exactly that instant.
Each page asks every source for its newest items at or before the cursor, merges them, and keeps
only what no source could still be missing (a source that returned a full page may have more
just below what it returned). Store-only messages are looked for in the same time window, by
``createTime``.

**Time budget.** Independent calls run in parallel, and the page stops starting calls when the
request's budget is spent (``fhir_proxy.deadline``). A page cut short that way says
``partial: true``: rows of its window may be missing (a message lookup not finished is
``messageLookup: "failed"``; store-only rows not yet checked are left out, never guessed).

**A wrong derived-name rule.** If cuff-sim's derived MLLP name is not in the store, the attempt
stays ``messageLookup: "not-found"`` with no application-level outcome and no Observation: no
outcome label is ever moved from one record to another. Its real message, if stored, then
appears as a store-only row. That row carries ``possibleDuplicateOf: <attempt id>`` only when
exactly one attempt qualifies: an accepted MLLP attempt with the same control ID whose own
derived name the store does not have, sent at most ``MATCH_WINDOW`` (120 s) before the message
was created. Candidates come from the same audit search that decides "store-only" (it spans
``MATCH_WINDOW`` before the oldest message, so a page boundary cannot hide the attempt), and
only messages that no attempt anywhere claims are considered. Two or more candidates give no
hint. The hint is a pointer for the page, not a join.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial

from pulsebridge_fhir import (
    NotASendAttempt,
    SendAttemptRecord,
    UnreadableSendAttempt,
    parse_send_attempt,
    send_attempt_search_params,
)
from pulsebridge_fhir.keys import deterministic_uuid
from pulsebridge_gcp import HealthcareApiError, Hl7v2Message, NotFoundError

from fhir_proxy import deadline, hl7
from fhir_proxy.errors import Problem
from fhir_proxy.logs import log
from fhir_proxy.store import Stores, entries, next_link

DEFAULT_COUNT = 25
MAX_COUNT = 50
KINDS = ("cuff-send", "unreadable-send-attempt", "fhir-transaction")
OUTCOMES = ("accepted", "error", "pending")
# Provenances read per page for the transaction rows; one health-sync Bundle writes at most
# 100 windows (its batch size), so 200 always holds at least one whole Bundle.
PROVENANCE_PAGE = 200
STORE_ONLY_SCAN_PAGES = 3  # of 100 messages each, per page of the log
# An attempt is recorded after its ACK or timeout: cuff-sim's worst case is about 80 s after the
# send (its README, "Time limits"). The margin is generous on purpose.
SEND_RECORD_SKEW = timedelta(minutes=10)
MATCH_WINDOW = timedelta(seconds=120)
# Wide enough to read every attempt that could be a store-only message's possible duplicate
# (sent up to MATCH_WINDOW before the message was created; its recorded time is later still).
AUDIT_CHECK_BEFORE = MATCH_WINDOW + timedelta(seconds=10)
AUDIT_CHECK_AFTER = SEND_RECORD_SKEW
AUDIT_CHECK_PAGE = 1000  # Cloud Healthcare's largest _count
AUDIT_CHECK_PAGES = 3
ATTEMPT_PAGES = 5
MAX_CURSOR_LENGTH = 8192
MAX_CURSOR_SEEN = 200
LOOKUP_WORKERS = 8

WRITERS = {
    "PulseBridge health-sync": "health-sync",
    "PulseBridge v2-to-fhir": "v2-to-fhir",
    "PulseBridge seed (synthetic data)": "seed",
}
NO_ACK_DETAIL = {
    "timeout": "No acknowledgment arrived before the deadline.",
    "connection-closed": "The connection closed without an acknowledgment.",
    "api-error": "The ingest endpoint answered an HTTP error without an HL7 acknowledgment.",
    "unreadable-reply": "A reply came back, but it was not a readable HL7v2 acknowledgment.",
}
_REASON_LABEL = re.compile(r"[a-z0-9_-]{1,63}", re.ASCII)
_REFERENCE = re.compile(r"[A-Z][A-Za-z]{1,63}/[A-Za-z0-9.-]{1,64}", re.ASCII)
_CURSOR_ID = re.compile(r"[A-Za-z0-9._:/=-]{1,400}", re.ASCII)
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def instant_text(value: datetime) -> str:
    """UTC, milliseconds, ``Z`` (how the rows and FHIR date searches write an instant)."""
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _micro_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_instant(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


# ---- cursor -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Cursor:
    before: datetime
    seen: frozenset[str]

    def encode(self) -> str:
        body = {"v": 1, "b": _micro_text(self.before), "s": sorted(self.seen)}
        raw = json.dumps(body, separators=(",", ":")).encode("ascii")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def decode_cursor(text: str, now: datetime | None = None) -> Cursor:
    """A cursor the proxy wrote, checked strictly: it comes back from the caller. Its instant
    must lie between 1970 and a day from now: a forged far-future time would otherwise overflow
    the search bounds computed from it."""
    bad = Problem(
        400,
        "invalid",
        "invalid-cursor",
        "The cursor is not valid; load the log from the start.",
        expression=["cursor"],
    )
    if not isinstance(text, str) or not 1 <= len(text) <= MAX_CURSOR_LENGTH:
        raise bad
    if not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise bad
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        body = json.loads(raw.decode("ascii"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise bad from None
    if not isinstance(body, dict) or set(body) != {"v", "b", "s"} or body["v"] != 1:
        raise bad
    before = _parse_instant(body["b"])
    seen = body["s"]
    if before is None or not isinstance(seen, list) or len(seen) > MAX_CURSOR_SEEN:
        raise bad
    latest = (now or datetime.now(UTC)) + timedelta(days=1)
    if not EPOCH <= before <= latest:
        raise bad
    if not all(isinstance(i, str) and _CURSOR_ID.fullmatch(i) for i in seen):
        raise bad
    return Cursor(before, frozenset(seen))


# ---- rows -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Lookup:
    """What the HL7v2 store said about a message name: ``confirmed`` (it exists),
    ``not-found``, or ``failed`` (the store could not be asked in time)."""

    status: str
    message: Hl7v2Message | None = None


def application_from_labels(labels: dict[str, str]) -> dict:
    """The application-level outcome from T12's labels; anything unrecognised is ``pending``:
    an odd label never becomes a stronger claim than "not mapped yet"."""
    outcome = labels.get("application_outcome")
    if outcome == "accepted":
        return {"outcome": "accepted", "reason": None}
    if outcome == "error":
        reason = labels.get("reason")
        return {
            "outcome": "error",
            "reason": reason
            if isinstance(reason, str) and _REASON_LABEL.fullmatch(reason)
            else None,
        }
    return {"outcome": "pending", "reason": None}


def _decoded(raw: bytes | None) -> str | None:
    return None if raw is None else raw.decode("utf-8", errors="replace")


def commit_ack(record: SendAttemptRecord) -> dict:
    """The commit-level acknowledgment as the contract's ``CommitAck``. The raw ACK is
    **decoded** here for display; the AuditEvent itself keeps it base64 (T31)."""
    if record.ack_code is not None:
        return {
            "received": True,
            "code": record.ack_code,
            # T31 keeps MSH-10 of the attempt (the row's controlId), not MSA-2.
            "controlId": None,
            "reason": record.reason,
            "raw": _decoded(record.raw_ack),
        }
    why = record.no_ack_reason or "timeout"
    return {
        "received": False,
        "why": why,
        "httpStatus": record.http_status,
        "detail": NO_ACK_DETAIL.get(why, "No acknowledgment was received."),
        "replyPrefixBase64": (
            None
            if record.reply_prefix is None
            else base64.b64encode(record.reply_prefix).decode("ascii")
        ),
    }


def attempt_row(record: SendAttemptRecord, lookup: Lookup | None, observation: str | None) -> dict:
    application = None
    # Only the store's own confirmation of THIS attempt's name gives it an outcome.
    if record.result == "accepted" and lookup is not None and lookup.status == "confirmed":
        application = application_from_labels(lookup.message.labels if lookup.message else {})
    return {
        "kind": "cuff-send",
        "id": record.id,
        "sentAt": instant_text(record.sent_at),
        "recordedAt": instant_text(record.recorded),
        "transport": record.transport,
        "controlId": record.control_id,
        "result": record.result,
        "outcome": record.outcome,
        "sample": record.sample_name,
        "commitAck": commit_ack(record),
        "application": application,
        "messageName": record.message_name,
        "derivedMessageName": record.derived_message_name,
        # Amendment to the dashboard contract (README): whether the name was confirmed.
        "messageLookup": None if lookup is None else lookup.status,
        "observation": observation,
        "source": "audit-event",
    }


def store_only_row(
    message: Hl7v2Message,
    at: datetime,
    observation: str | None,
    possible_duplicate_of: str | None = None,
) -> dict:
    sent = _parse_instant(message.send_time)
    transport = "https" if message.labels.get("transport") == "https" else None
    return {
        "kind": "cuff-send",
        "id": message.name,
        "sentAt": instant_text(sent or at),
        "recordedAt": instant_text(at),
        # cuff-sim labels HTTPS ingests; the adapter cannot label, so an unlabelled message's
        # transport is unknown (it is probably MLLP, but that is not a fact).
        "transport": transport,
        "controlId": hl7.control_id(message.data),
        "result": "accepted",
        "outcome": "0",
        "sample": None,
        "commitAck": {
            "received": True,
            "code": "AA",
            "controlId": None,
            "reason": None,
            "raw": None,
        },
        "application": application_from_labels(message.labels),
        "messageName": message.name,
        "derivedMessageName": None,
        "messageLookup": "confirmed",
        "observation": observation,
        "source": "store-only",
        # Amendment: the one attempt this message may belong to (a wrong derived name), as a
        # pointer only; the message's outcome stays its own and the attempt gets none.
        "possibleDuplicateOf": possible_duplicate_of,
    }


def _clean_problem(text: str) -> str:
    text = _CONTROL.sub("�", text).strip()
    return text if len(text) <= 300 else text[:299] + "…"


def unreadable_row(resource: dict, problem: str) -> dict:
    resource_id = resource.get("id")
    recorded = _parse_instant(resource.get("recorded"))
    return {
        "kind": "unreadable-send-attempt",
        "id": resource_id if isinstance(resource_id, str) else "",
        "recordedAt": None if recorded is None else instant_text(recorded),
        "problem": _clean_problem(problem),
    }


# ---- streams and the merge ---------------------------------------------------------------------


@dataclass
class Item:
    at: datetime | None
    id: str
    kind: str  # "attempt", "unreadable", "transaction"
    payload: object = None


@dataclass
class Stream:
    items: list[Item]
    truncated: bool = False
    # When truncated: items below this instant may be missing; at it, too unless inclusive.
    frontier: datetime | None = None
    inclusive: bool = True


def _allowed(item: Item, streams: Iterable[Stream]) -> bool:
    for stream in streams:
        if not stream.truncated or stream.frontier is None:
            continue
        if item.at < stream.frontier or (item.at == stream.frontier and not stream.inclusive):
            return False
    return True


@dataclass
class Merged:
    page: list[Item]
    undated: list[Item]
    lower: datetime | None  # the page's oldest instant when more may follow; None otherwise
    more: bool


def merge(streams: list[Stream], count: int, cursor: Cursor | None) -> Merged:
    """Newest first, ``count`` at most, skipping what the cursor says was shown."""
    dated = []
    undated = []
    for stream in streams:
        for item in stream.items:
            if item.at is None:
                if cursor is None:
                    undated.append(item)  # an unreadable row with no time: first page only
                continue
            if cursor is not None and (
                item.at > cursor.before or (item.at == cursor.before and item.id in cursor.seen)
            ):
                continue
            dated.append(item)
    candidates = [i for i in dated if _allowed(i, streams)]
    if not candidates and dated:
        # Degenerate: nothing is above every truncated stream's frontier even after the sources
        # read their bounded number of pages (many rows at one instant, or send times far below
        # their recorded times). Show what was read, in order; the seen ids stop repeats. Order
        # across this page boundary may be off by the rows still unread.
        log(logging.WARNING, "messages-frontier-degenerate", rows=len(dated))
        candidates = dated
    candidates.sort(key=lambda i: (i.at, i.id), reverse=True)
    page = candidates[:count]
    more = len(candidates) > count or any(s.truncated for s in streams)
    lower = page[-1].at if (more and page) else None
    if more and not page:
        more = False
    return Merged(page, undated, lower, more)


# ---- the log -----------------------------------------------------------------------------------


def _writer(resource: dict) -> str:
    for agent in resource.get("agent") or []:
        who = agent.get("who") if isinstance(agent, dict) else None
        display = who.get("display") if isinstance(who, dict) else None
        if display in WRITERS:
            return WRITERS[display]
    return "unknown"


def groups_from_provenances(bundle: dict, patients: Iterable[str]) -> tuple[list[Item], int]:
    """One item per (recorded, assembler): the Provenances one ingest Bundle wrote, **pinned to
    the configured patient**. Each Provenance's targets come back in the same Bundle
    (``_include=Provenance:target``); only targets that are the patient's Observations are kept,
    and a Provenance with none is left out. Returns the items and how many Provenances were read
    (for the truncation check)."""
    patients = set(patients)
    ours: set[str] = set()
    for resource in entries(bundle, "Observation"):
        subject = resource.get("subject")
        if (
            isinstance(resource.get("id"), str)
            and isinstance(subject, dict)
            and subject.get("reference") in patients
        ):
            ours.add(f"Observation/{resource['id']}")
    provenances = entries(bundle, "Provenance")
    groups: dict[tuple[datetime, str], set[str]] = {}
    for resource in provenances:
        at = _parse_instant(resource.get("recorded"))
        resource_id = resource.get("id")
        if at is None or not isinstance(resource_id, str):
            continue
        targets = {
            t.get("reference")
            for t in resource.get("target") or []
            if isinstance(t, dict) and t.get("reference") in ours
        }
        if not targets:
            continue  # another patient's, or a target that could not be confirmed as ours
        refs = groups.setdefault((at, _writer(resource)), set())
        refs.add(f"Provenance/{resource_id}")
        refs.update(r for r in targets if _REFERENCE.fullmatch(r))
    items = []
    for (at, writer), refs in groups.items():
        tx_id = deterministic_uuid("fhir-transaction", writer, instant_text(at))
        row = {
            "kind": "fhir-transaction",
            "id": tx_id,
            "recordedAt": instant_text(at),
            "writer": writer,
            # The executeBundle status is not recorded anywhere; the Provenance only shows that
            # the transaction committed (a transaction is all or nothing).
            "httpStatus": None,
            "resources": sorted(refs)[:200],
        }
        items.append(Item(at, tx_id, "transaction", row))
    items.sort(key=lambda i: (i.at, i.id), reverse=True)
    return items, len(provenances)


@dataclass
class _Listing:
    messages: list[tuple[datetime, Hl7v2Message]]
    complete: bool


class MessagesLog:
    def __init__(
        self,
        stores: Stores,
        *,
        patient_id: str,
        executor: ThreadPoolExecutor | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._stores = stores
        self._patient_id = patient_id
        self._executor = executor or ThreadPoolExecutor(max_workers=LOOKUP_WORKERS)
        self._now = now

    # ---- sources -------------------------------------------------------------------------

    def _attempts(self, count: int, cursor: Cursor | None) -> tuple[Stream, int]:
        """Attempts by send time. The search is by ``recorded`` (the only date the audit store
        indexes), so it reaches ``SEND_RECORD_SKEW`` above the cursor and reads further pages
        until ``count`` rows are known to be in order (above the lowest ``recorded`` read)."""
        params = send_attempt_search_params(count=min(max(count * 2, 50), 200))
        if cursor is not None:
            params.insert(2, ("date", "le" + instant_text(cursor.before + SEND_RECORD_SKEW)))
        bundle = self._stores.audit_events(params)
        items: list[Item] = []
        skipped = 0
        recorded: list[datetime] = []
        pages = 1
        while True:
            for resource in entries(bundle, "AuditEvent"):
                at = _parse_instant(resource.get("recorded"))
                if at is not None:
                    recorded.append(at)
                try:
                    record = parse_send_attempt(resource)
                except NotASendAttempt:
                    skipped += 1
                    continue
                except UnreadableSendAttempt as exc:
                    log(
                        logging.WARNING,
                        "unreadable-send-attempt",
                        attempt_id=_safe_id(resource.get("id")),
                    )
                    rid = resource.get("id") if isinstance(resource.get("id"), str) else ""
                    items.append(Item(at, rid, "unreadable", unreadable_row(resource, str(exc))))
                    continue
                if (
                    record.patient_id != self._patient_id
                    or record.destination != self._stores.hl7v2_store_name
                ):
                    skipped += 1  # the patient pin: another patient's or another store's attempt
                    continue
                items.append(Item(record.sent_at, record.id, "attempt", record))
            more = next_link(bundle) is not None
            floor = min(recorded) if recorded else None
            ready = [
                i
                for i in items
                if i.at is not None
                and (floor is None or i.at > floor)
                and not (
                    cursor
                    and (i.at > cursor.before or (i.at == cursor.before and i.id in cursor.seen))
                )
            ]
            if not more or len(ready) > count or pages >= ATTEMPT_PAGES or deadline.expired():
                break
            try:
                bundle = self._stores.next_audit_page(bundle)
            except HealthcareApiError:
                break  # more stays True: the stream is truncated where it stopped
            pages += 1
            if bundle is None:
                more = False
                break
        frontier = min(recorded) if (more and recorded) else None
        return Stream(items, more, frontier, inclusive=False), skipped

    def _transactions(self, cursor: Cursor | None, patients: tuple[str, ...]) -> Stream:
        params = [
            ("_sort", "-recorded"),
            ("_count", str(PROVENANCE_PAGE)),
            ("_include", "Provenance:target"),
        ]
        if cursor is not None:
            params.insert(0, ("recorded", "le" + instant_text(cursor.before)))
        bundle = self._stores.provenances(params)
        items, read = groups_from_provenances(bundle, patients)
        truncated = read >= PROVENANCE_PAGE or next_link(bundle) is not None
        if truncated and len(items) > 1:
            # The oldest group may continue on the next store page: drop it here, and let
            # nothing at or below its instant onto this page (it comes back next time).
            dropped = items.pop()
            return Stream(items, True, dropped.at, inclusive=False)
        frontier = items[-1].at if (truncated and items) else None
        return Stream(items, truncated, frontier, inclusive=True)

    def _lookup(self, name: str) -> Lookup:
        if not self._stores.is_our_message(name):
            return Lookup("failed")
        try:
            return Lookup("confirmed", self._stores.get_message(name))
        except NotFoundError:
            return Lookup("not-found")
        except (HealthcareApiError, ValueError) as exc:
            log(logging.WARNING, "message-lookup-failed", error_type=type(exc).__name__)
            return Lookup("failed")

    def lookups(self, names: Iterable[str]) -> dict[str, Lookup]:
        """``messages.get`` for each name, in parallel, within the request's budget. A lookup
        that did not finish in time is ``failed``."""
        unique = sorted(set(names))
        results = deadline.run_parallel(self._executor, [partial(self._lookup, n) for n in unique])
        out = {}
        for name, result in zip(unique, results, strict=True):
            out[name] = result if isinstance(result, Lookup) else Lookup("failed")
        return out

    def _listing(self, lower: datetime | None, cursor: Cursor | None) -> _Listing:
        """Stored messages created in the page's window (``create_time``), newest first."""
        clauses = []
        if lower is not None:
            clauses.append(f'create_time >= "{_micro_text(lower)}"')
        if cursor is not None:
            clauses.append(f'create_time <= "{_micro_text(cursor.before)}"')
        filter_text = " AND ".join(clauses) or None
        found = []
        token = None
        for _ in range(STORE_ONLY_SCAN_PAGES):
            page = self._stores.list_messages(filter=filter_text, page_token=token)
            for message in page.messages:
                at = _parse_instant(message.create_time)
                if at is None or (lower is not None and at < lower):
                    continue
                if cursor is not None and (
                    at > cursor.before or (at == cursor.before and message.name in cursor.seen)
                ):
                    continue
                ids = [p.value for p in message.patient_ids]
                if ids and self._patient_id not in ids:
                    continue  # the patient pin: a message about someone else
                found.append((at, message))
            token = page.next_page_token
            if token is None or deadline.expired():
                break
        if token is not None:
            log(logging.WARNING, "store-only-scan-truncated", pages=STORE_ONLY_SCAN_PAGES)
        return _Listing(found, token is None)

    def _audited(self, times: list[datetime]) -> list[SendAttemptRecord] | None:
        """Every readable attempt recorded around ``times``, from ONE audit search over the
        whole span (oldest - 130 s to newest + 10 min), paged to the end. None when that could
        not be read completely: then nothing can be called store-only."""
        params = send_attempt_search_params(
            count=AUDIT_CHECK_PAGE,
            since=min(times) - AUDIT_CHECK_BEFORE,
            before=max(times) + AUDIT_CHECK_AFTER,
        )
        records: list[SendAttemptRecord] = []
        try:
            bundle = self._stores.audit_events(params)
            for _ in range(AUDIT_CHECK_PAGES):
                for resource in entries(bundle, "AuditEvent"):
                    try:
                        record = parse_send_attempt(resource)
                    except (NotASendAttempt, UnreadableSendAttempt):
                        continue
                    if record.patient_id == self._patient_id:
                        records.append(record)
                if next_link(bundle) is None:
                    return records
                bundle = self._stores.next_audit_page(bundle)
                if bundle is None:
                    return records
        except HealthcareApiError:
            return None
        log(logging.WARNING, "store-only-check-truncated", pages=AUDIT_CHECK_PAGES)
        return None  # cut off: cannot tell which messages have an AuditEvent

    @staticmethod
    def _duplicate_candidates(
        message: Hl7v2Message, at: datetime, records: list[SendAttemptRecord]
    ) -> list[SendAttemptRecord]:
        """Attempts a store-only message may belong to: accepted MLLP with a derived name (whose
        lookup the caller checks), the same control ID, sent at most MATCH_WINDOW before the
        message was created."""
        control_id = hl7.control_id(message.data)
        if control_id is None:
            return []
        return [
            r
            for r in records
            if r.transport == "mllp"
            and r.result == "accepted"
            and r.derived_message_name is not None
            and r.control_id == control_id
            and r.sent_at <= at <= r.sent_at + MATCH_WINDOW
        ]

    def _observations(self, data_by_ref: dict[str, bytes | None]) -> dict[str, str]:
        """``Observation/<id>`` per key of ``data_by_ref`` whose stored bytes map to one."""
        identifiers: dict[str, str] = {}
        for ref, data in data_by_ref.items():
            value = hl7.observation_identifier(data, self._patient_id)
            if value is not None:
                identifiers[ref] = value
        if not identifiers:
            return {}
        patients = self._stores.patient_references()
        found = self._stores.observations_by_identifier(list(identifiers.values()))
        result = {}
        for ref, value in identifiers.items():
            resource = found.get(value)
            if (
                resource is not None
                and isinstance(resource.get("id"), str)
                and self._stores.is_ours(resource, patients)
            ):
                result[ref] = f"Observation/{resource['id']}"
        return result

    # ---- one page ------------------------------------------------------------------------

    def page(
        self,
        *,
        kind: str | None,
        outcome: str | None,
        count: int,
        cursor: Cursor | None,
    ) -> dict:
        want_attempts = kind in (None, "cuff-send", "unreadable-send-attempt")
        want_transactions = kind in (None, "fhir-transaction") and outcome is None
        want_store_only = kind in (None, "cuff-send")
        partial_page = False

        # 1. The sources, in parallel (the patient lookup is cached after the first time).
        patients = self._stores.patient_references()
        calls = []
        if want_attempts:
            calls.append(lambda: self._attempts(count, cursor))
        if want_transactions:
            calls.append(lambda: self._transactions(cursor, patients))
        results = deadline.run_parallel(self._executor, calls)
        streams: list[Stream] = []
        for result in results:
            if isinstance(result, BaseException):
                raise result
            if result is deadline.NOT_FINISHED:
                raise deadline.BudgetExhausted("messages log sources")
            if isinstance(result, tuple):
                stream, skipped = result
                streams.append(stream)
                if skipped:
                    log(logging.INFO, "attempts-skipped", count=skipped)
            else:
                streams.append(result)
        merged = merge(streams, count, cursor)

        # 2. Message lookups and the store listing, in parallel: one flat submission (no task
        # submits into the pool it runs in).
        records = [i.payload for i in merged.page if i.kind == "attempt"]
        names = sorted(
            {
                n
                for r in records
                if r.result == "accepted"
                for n in (r.message_name or r.derived_message_name,)
                if n
            }
        )
        lower = merged.lower if merged.more else None
        step = [partial(self._lookup, n) for n in names]
        if want_store_only:
            step.append(partial(self._listing, lower, cursor))
        results = deadline.run_parallel(self._executor, step)
        looked = {
            n: (r if isinstance(r, Lookup) else Lookup("failed"))
            for n, r in zip(names, results[: len(names)], strict=True)
        }
        partial_page |= any(v.status == "failed" for v in looked.values())
        listing = results[-1] if want_store_only else _Listing([], True)
        if not isinstance(listing, _Listing):
            listing = _Listing([], False)

        # 3. Store-only rows: stored messages that NO attempt in the audit span claims. The
        # same audit search gives the candidates for a possible-duplicate hint.
        claimed = {n for r in records for n in (r.message_name, r.derived_message_name) if n}
        unclaimed = [(at, m) for at, m in listing.messages if m.name not in claimed]
        lookup_for: dict[str, Lookup | None] = {}
        for record in records:
            name = record.message_name or record.derived_message_name
            lookup_for[record.id] = (
                looked.get(name) if (record.result == "accepted" and name) else None
            )
        store_only: list[tuple[datetime, Hl7v2Message, str | None]] = []
        if want_store_only and unclaimed:
            audited = self._audited([at for at, _ in unclaimed])
            if audited is None:
                partial_page = True  # cannot tell; never guess "store-only"
            else:
                everywhere = {
                    n for r in audited for n in (r.message_name, r.derived_message_name) if n
                }
                orphans = [(at, m) for at, m in unclaimed if m.name not in everywhere]
                candidates = {
                    m.name: self._duplicate_candidates(m, at, audited) for at, m in orphans
                }
                # A candidate qualifies only if the store does not have its own derived name.
                to_check = sorted(
                    {r.derived_message_name for rs in candidates.values() for r in rs} - set(looked)
                )
                extra = deadline.run_parallel(
                    self._executor, [partial(self._lookup, n) for n in to_check]
                )
                known = dict(looked)
                known.update(
                    {
                        n: (r if isinstance(r, Lookup) else Lookup("failed"))
                        for n, r in zip(to_check, extra, strict=True)
                    }
                )
                for at, m in orphans:
                    qualifying = [
                        r
                        for r in candidates[m.name]
                        if known.get(r.derived_message_name, Lookup("failed")).status == "not-found"
                    ]
                    hint = qualifying[0].id if len(qualifying) == 1 else None
                    store_only.append((at, m, hint))
        if want_store_only and not listing.complete:
            partial_page = True

        # 4. The resulting Observation, for every message whose outcome is accepted.
        data_by_ref: dict[str, bytes | None] = {}
        for lookup in lookup_for.values():
            if (
                lookup is not None
                and lookup.message is not None
                and application_from_labels(lookup.message.labels)["outcome"] == "accepted"
            ):
                data_by_ref[lookup.message.name] = lookup.message.data
        for _, message, _ in store_only:
            if application_from_labels(message.labels)["outcome"] == "accepted":
                data_by_ref[message.name] = message.data
        observations: dict[str, str] = {}
        if data_by_ref:
            try:
                observations = self._observations(data_by_ref)
            except HealthcareApiError:
                partial_page = True  # the rows stand; only their Observation link is missing

        rows: list[tuple[datetime, str, dict]] = []
        for item in merged.page:
            if item.kind == "attempt":
                record = item.payload
                lookup = lookup_for.get(record.id)
                obs = observations.get(lookup.message.name) if lookup and lookup.message else None
                rows.append((item.at, item.id, attempt_row(record, lookup, obs)))
            else:
                rows.append((item.at, item.id, item.payload))
        for at, message, hint in store_only:
            obs = observations.get(message.name)
            rows.append((at, message.name, store_only_row(message, at, obs, hint)))
        rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
        ordered = [r[2] for r in rows] + [i.payload for i in merged.undated]

        if kind is not None:
            ordered = [r for r in ordered if r["kind"] == kind]
        if outcome is not None:
            # A row with no application-level outcome (not stored, or a name the store could
            # not confirm) matches no outcome filter, "pending" included: pending means "stored,
            # not labelled yet", which is not known for such a row.
            ordered = [
                r
                for r in ordered
                if r["kind"] == "cuff-send"
                and r["application"] is not None
                and r["application"]["outcome"] == outcome
            ]

        next_cursor = None
        if merged.more and merged.lower is not None:
            seen = {i.id for i in merged.page if i.at == merged.lower}
            seen |= {m.name for at, m, _ in store_only if at == merged.lower}
            if cursor is not None and cursor.before == merged.lower:
                seen |= cursor.seen
            next_cursor = Cursor(merged.lower, frozenset(sorted(seen)[:MAX_CURSOR_SEEN])).encode()
        return {
            "entries": ordered,
            "nextCursor": next_cursor,
            "unreadable": sum(1 for r in ordered if r["kind"] == "unreadable-send-attempt"),
            # Amendment: true when the page was cut short by the time budget or a store that
            # could not answer completely; reload to fill it in.
            "partial": partial_page,
        }


def _safe_id(value: object) -> str | None:
    return value if isinstance(value, str) and _CURSOR_ID.fullmatch(value) else None
