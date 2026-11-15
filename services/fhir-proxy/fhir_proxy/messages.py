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

**Paging.** A cursor names an instant and the ids already shown at exactly that instant. Each
page asks every source for its newest items at or before the cursor, merges them, and keeps
only what no source could still be missing (a source that returned a full page may have more
just below its last item). Store-only messages are looked for in the same time window as the
page, by their store ``createTime``.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pulsebridge_fhir import (
    NotASendAttempt,
    SendAttemptRecord,
    UnreadableSendAttempt,
    parse_send_attempt,
    send_attempt_search_params,
)
from pulsebridge_fhir.keys import deterministic_uuid
from pulsebridge_gcp import HealthcareApiError, Hl7v2Message, NotFoundError

from fhir_proxy import hl7
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
STORE_ONLY_CHECKS = 25  # candidates checked against the audit store, per page of the log
# An attempt is recorded after its ACK or timeout (cuff-sim: at most about 80 s after the send).
AUDIT_CHECK_BEFORE = timedelta(seconds=60)
AUDIT_CHECK_AFTER = timedelta(minutes=10)
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
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


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


def decode_cursor(text: str) -> Cursor:
    """A cursor the proxy wrote, checked strictly: it comes back from the caller."""
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
    if not all(isinstance(i, str) and _CURSOR_ID.fullmatch(i) for i in seen):
        raise bad
    return Cursor(before, frozenset(seen))


# ---- rows -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Lookup:
    """What the HL7v2 store said about a message name: ``confirmed`` (it exists),
    ``not-found``, or ``failed`` (the store could not be asked)."""

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


def store_only_row(message: Hl7v2Message, at: datetime, observation: str | None) -> dict:
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
        # Degenerate: every item sits exactly at a truncated stream's frontier (many rows at one
        # instant). Show them; the seen ids stop them from repeating.
        floor = max(s.frontier for s in streams if s.truncated and s.frontier is not None)
        candidates = [i for i in dated if i.at >= floor]
    candidates.sort(key=lambda i: (i.at, i.id), reverse=True)
    page = candidates[:count]
    more = len(candidates) > count or any(s.truncated for s in streams)
    lower = page[-1].at if (more and page) else None
    if more and not page:
        more = False
    return Merged(page, undated, lower, more)


# ---- the log -----------------------------------------------------------------------------------


def _groups_from_provenances(resources: list[dict]) -> list[Item]:
    """One item per (recorded, assembler): the Provenances one ingest Bundle wrote."""
    groups: dict[tuple[datetime, str], dict] = {}
    for resource in resources:
        at = _parse_instant(resource.get("recorded"))
        resource_id = resource.get("id")
        if at is None or not isinstance(resource_id, str):
            continue
        writer = "unknown"
        for agent in resource.get("agent") or []:
            who = agent.get("who") if isinstance(agent, dict) else None
            display = who.get("display") if isinstance(who, dict) else None
            if display in WRITERS:
                writer = WRITERS[display]
        group = groups.setdefault((at, writer), {"refs": set()})
        group["refs"].add(f"Provenance/{resource_id}")
        for target in resource.get("target") or []:
            ref = target.get("reference") if isinstance(target, dict) else None
            if isinstance(ref, str) and _REFERENCE.fullmatch(ref):
                group["refs"].add(ref)
    items = []
    for (at, writer), group in groups.items():
        tx_id = deterministic_uuid("fhir-transaction", writer, instant_text(at))
        row = {
            "kind": "fhir-transaction",
            "id": tx_id,
            "recordedAt": instant_text(at),
            "writer": writer,
            # The executeBundle status is not recorded anywhere; the Provenance only shows that
            # the transaction committed (a transaction is all or nothing).
            "httpStatus": None,
            "resources": sorted(group["refs"])[:200],
        }
        items.append(Item(at, tx_id, "transaction", row))
    items.sort(key=lambda i: (i.at, i.id), reverse=True)
    return items


@dataclass
class _Joined:
    lookups: dict[str, Lookup] = field(default_factory=dict)  # by message name


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
        params = send_attempt_search_params(count=count)
        if cursor is not None:
            params.insert(2, ("date", "le" + instant_text(cursor.before)))
        bundle = self._stores.audit_events(params)
        resources = entries(bundle, "AuditEvent")
        items: list[Item] = []
        skipped = 0
        for resource in resources:
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
                at = _parse_instant(resource.get("recorded"))
                rid = resource.get("id") if isinstance(resource.get("id"), str) else ""
                items.append(Item(at, rid, "unreadable", unreadable_row(resource, str(exc))))
                continue
            if (
                record.patient_id != self._patient_id
                or record.destination != self._stores.hl7v2_store_name
            ):
                skipped += 1  # the patient pin: another patient's or another store's attempt
                continue
            items.append(Item(record.recorded, record.id, "attempt", record))
        truncated = len(resources) >= count or next_link(bundle) is not None
        dated = [i.at for i in items if i.at is not None]
        frontier = min(dated) if (truncated and dated) else None
        return Stream(items, truncated, frontier, inclusive=True), skipped

    def _transactions(self, cursor: Cursor | None) -> Stream:
        params = [("_sort", "-recorded"), ("_count", str(PROVENANCE_PAGE))]
        if cursor is not None:
            params.insert(0, ("recorded", "le" + instant_text(cursor.before)))
        bundle = self._stores.provenances(params)
        resources = entries(bundle, "Provenance")
        items = _groups_from_provenances(resources)
        truncated = len(resources) >= PROVENANCE_PAGE or next_link(bundle) is not None
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
        unique = sorted(set(names))
        return dict(zip(unique, self._executor.map(self._lookup, unique), strict=True))

    def _audited_names(self, at: datetime) -> set[str] | None:
        """Every message name an attempt recorded around ``at`` claims; None if the check failed."""
        params = send_attempt_search_params(
            count=100, since=at - AUDIT_CHECK_BEFORE, before=at + AUDIT_CHECK_AFTER
        )
        try:
            bundle = self._stores.audit_events(params)
        except HealthcareApiError:
            return None
        names = set()
        for resource in entries(bundle, "AuditEvent"):
            try:
                record = parse_send_attempt(resource)
            except (NotASendAttempt, UnreadableSendAttempt):
                continue
            names.update(n for n in (record.message_name, record.derived_message_name) if n)
        return names

    def _store_only(
        self,
        lower: datetime | None,
        cursor: Cursor | None,
        claimed: set[str],
    ) -> list[tuple[datetime, Hl7v2Message]]:
        clauses = []
        if lower is not None:
            clauses.append(f'create_time >= "{_micro_text(lower)}"')
        if cursor is not None:
            clauses.append(f'create_time <= "{_micro_text(cursor.before)}"')
        filter_text = " AND ".join(clauses) or None
        candidates = []
        token = None
        for _ in range(STORE_ONLY_SCAN_PAGES):
            page = self._stores.list_messages(filter=filter_text, page_token=token)
            for message in page.messages:
                at = _parse_instant(message.create_time)
                if at is None or message.name in claimed:
                    continue
                if lower is not None and at < lower:
                    continue
                if cursor is not None and (
                    at > cursor.before or (at == cursor.before and message.name in cursor.seen)
                ):
                    continue
                ids = [p.value for p in message.patient_ids]
                if ids and self._patient_id not in ids:
                    continue  # the patient pin: a message about someone else
                candidates.append((at, message))
            token = page.next_page_token
            if token is None:
                break
        else:
            if token is not None:
                log(logging.WARNING, "store-only-scan-truncated", pages=STORE_ONLY_SCAN_PAGES)
        found = []
        for at, message in candidates[:STORE_ONLY_CHECKS]:
            names = self._audited_names(at)
            if names is None or message.name in names:
                continue  # it has an AuditEvent (on another page), or we cannot tell
            found.append((at, message))
        if len(candidates) > STORE_ONLY_CHECKS:
            log(logging.WARNING, "store-only-checks-capped", candidates=len(candidates))
        return found

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

        streams: list[Stream] = []
        if want_attempts:
            attempts, skipped = self._attempts(count, cursor)
            streams.append(attempts)
            if skipped:
                log(logging.INFO, "attempts-skipped", count=skipped)
        if want_transactions:
            streams.append(self._transactions(cursor))
        merged = merge(streams, count, cursor)

        records = [i.payload for i in merged.page if i.kind == "attempt"]
        names = [
            name
            for r in records
            if r.result == "accepted"
            for name in (r.message_name or r.derived_message_name,)
            if name
        ]
        lookups = self.lookups(names)

        store_only: list[tuple[datetime, Hl7v2Message]] = []
        if want_store_only:
            claimed = {n for r in records for n in (r.message_name, r.derived_message_name) if n}
            lower = merged.lower if merged.more else None
            store_only = self._store_only(lower, cursor, claimed)

        # The resulting Observation, for every message whose outcome is accepted.
        data_by_ref: dict[str, bytes | None] = {}
        for name, lookup in lookups.items():
            if (
                lookup.message is not None
                and application_from_labels(lookup.message.labels)["outcome"] == "accepted"
            ):
                data_by_ref[name] = lookup.message.data
        for _, message in store_only:
            if application_from_labels(message.labels)["outcome"] == "accepted":
                data_by_ref[message.name] = message.data
        observations = self._observations(data_by_ref) if data_by_ref else {}

        rows: list[tuple[datetime, str, dict]] = []
        for item in merged.page:
            if item.kind == "attempt":
                record = item.payload
                name = record.message_name or record.derived_message_name
                lookup = lookups.get(name) if (record.result == "accepted" and name) else None
                rows.append(
                    (item.at, item.id, attempt_row(record, lookup, observations.get(name or "")))
                )
            else:
                rows.append((item.at, item.id, item.payload))
        for at, message in store_only:
            rows.append(
                (at, message.name, store_only_row(message, at, observations.get(message.name)))
            )
        rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
        ordered = [r[2] for r in rows] + [i.payload for i in merged.undated]

        if kind is not None:
            ordered = [r for r in ordered if r["kind"] == kind]
        if outcome is not None:
            ordered = [
                r
                for r in ordered
                if r["kind"] == "cuff-send" and (r["application"] or {}).get("outcome") == outcome
            ]

        next_cursor = None
        if merged.more and merged.lower is not None:
            seen = {i.id for i in merged.page if i.at == merged.lower}
            seen |= {m.name for at, m in store_only if at == merged.lower}
            if cursor is not None and cursor.before == merged.lower:
                seen |= cursor.seen
            next_cursor = Cursor(merged.lower, frozenset(sorted(seen)[:MAX_CURSOR_SEEN])).encode()
        return {
            "entries": ordered,
            "nextCursor": next_cursor,
            "unreadable": sum(1 for r in ordered if r["kind"] == "unreadable-send-attempt"),
        }


def _safe_id(value: object) -> str | None:
    return value if isinstance(value, str) and _CURSOR_ID.fullmatch(value) else None
