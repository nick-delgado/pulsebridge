"""What the FHIR store already holds for a range: read once, then write only what changed.

Every run re-requests three hours, so without this each window would be rewritten about twelve
times, and with versioning on each rewrite stores a new version of the Observation and of its
Provenance. Instead a run makes two FHIR searches (R4 search, standard parameters only):

1. ``Patient?identifier=urn:pulsebridge:patient|<PATIENT_ID>`` for the Patient's server id
   (the Observations refer to it as ``Patient/<id>``; the setup step wrote it, D11).
2. ``Observation?subject=Patient/<id>&code=http://loinc.org|8867-4&date=ge<start>&date=lt<end>``
   with ``_elements`` (only the fields compared) and ``_count``, following the ``next`` links.

and :func:`count_stranded` counts preliminary windows that have fallen behind every future run
(``status=preliminary&date=lt<range start>&_summary=count``).

``date`` on an Observation searches ``effective[x]``; for an ``effectivePeriod`` R4 matches when
the period overlaps the searched range, so the window that ends exactly at the range start is
also returned. That is harmless: it is simply not among the windows being written.

If any search fails, the caller writes everything, as before: reading is an optimisation,
never a condition for writing. Values here are compared, never logged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pulsebridge_fhir import codes
from pulsebridge_fhir.keys import escape_search_value

SEARCH_PAGE_SIZE = 1000  # 14 days of windows is 4,032: five pages at most
MAX_SEARCH_PAGES = 20
ELEMENTS = "identifier,status,effectivePeriod,valueQuantity,component"
FINAL_STATUSES = ("final", "amended", "corrected")
# R4 observation-status: "entered-in-error" ("the observation has been withdrawn following
# previous final release") and "cancelled" ("the observation is unavailable because the
# measurement was not started or not completed"). Either is a decision someone made about the
# stored copy; rewriting the window as final would undo it, so such windows are left alone.
WITHDRAWN_STATUSES = ("entered-in-error", "cancelled")

logger = logging.getLogger("health_sync")


class SearchClient(Protocol):
    def search(self, fhir_store: str, resource_type: str, params: object) -> dict: ...
    def next_page(self, fhir_store: str, bundle: dict) -> dict | None: ...


class StoredReadError(Exception):
    """A search answered something this module cannot use (not the HTTP errors, which the
    client raises itself)."""


@dataclass(frozen=True)
class StoredWindow:
    status: str
    end: datetime | None
    values: tuple[float | int | None, float | int | None, float | int | None]  # min, avg, max

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_STATUSES


def _fhir_instant(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _token(system: str, value: str) -> str:
    return f"{escape_search_value(system)}|{escape_search_value(value)}"


def find_patient_ids(client: SearchClient, store: str, patient_id: str) -> list[str]:
    """The server ids of the Patients with our identifier: normally exactly one (the setup
    step's). None means no Patient yet; more than one is the duplicate case T06's README warns
    about (the conditional creates will then fail with 412 until it is fixed by hand)."""
    bundle = client.search(
        store,
        "Patient",
        [("identifier", _token(codes.PB_PATIENT_ID, patient_id)), ("_elements", "id")],
    )
    ids = [
        entry["resource"]["id"]
        for entry in bundle.get("entry") or []
        if isinstance(entry, dict)
        and isinstance(entry.get("resource"), dict)
        and isinstance(entry["resource"].get("id"), str)
    ]
    return ids


def _number(quantity: object) -> float | int | None:
    if isinstance(quantity, dict):
        value = quantity.get("value")
        if isinstance(value, int | float) and not isinstance(value, bool):
            return value
    return None


def _component(resource: dict, code: str) -> float | int | None:
    for component in resource.get("component") or []:
        codings = (component.get("code") or {}).get("coding") or []
        if any(c.get("system") == codes.LOINC and c.get("code") == code for c in codings):
            return _number(component.get("valueQuantity"))
    return None


def _parse(resource: dict) -> tuple[str, StoredWindow] | None:
    key = next(
        (
            i.get("value")
            for i in resource.get("identifier") or []
            if isinstance(i, dict) and i.get("system") == codes.PB_OBSERVATION_ID
        ),
        None,
    )
    status = resource.get("status")
    if not isinstance(key, str) or not isinstance(status, str):
        return None
    end = None
    period = resource.get("effectivePeriod")
    if isinstance(period, dict) and isinstance(period.get("end"), str):
        try:
            end = datetime.fromisoformat(period["end"].replace("Z", "+00:00"))
        except ValueError:
            end = None
    values = (
        _component(resource, codes.HEART_RATE_MIN["code"]),
        _number(resource.get("valueQuantity")),
        _component(resource, codes.HEART_RATE_MAX["code"]),
    )
    return key, StoredWindow(status=status, end=end, values=values)


def stored_windows(
    client_for: Callable[[], SearchClient],
    store: str,
    patient_ref: str,
    start: datetime,
    end: datetime,
) -> dict[str, StoredWindow]:
    """Every stored heart-rate window overlapping ``[start, end)``, by idempotency key.

    ``client_for`` gives a client for each page, so each page's timeout is cut to the time the
    run has left. A key found more than once (two Observations with one identifier, which should
    not exist) is left out, so the window is written; the write's conditional update then
    reports the duplicate itself. Only the count is logged, never the keys."""
    params = [
        ("subject", patient_ref),
        ("code", _token(codes.LOINC, codes.HEART_RATE["code"])),
        ("date", f"ge{_fhir_instant(start)}"),
        ("date", f"lt{_fhir_instant(end)}"),
        ("_elements", ELEMENTS),
        ("_count", str(SEARCH_PAGE_SIZE)),
    ]
    found: dict[str, StoredWindow] = {}
    duplicated: set[str] = set()
    page: dict | None = client_for().search(store, "Observation", params)
    for _ in range(MAX_SEARCH_PAGES):
        if page is None:
            if duplicated:
                logger.warning(
                    "an observation key is stored more than once; writing those windows",
                    extra={
                        "fields": {
                            "event": "stored-duplicate-key",
                            "duplicate_keys": len(duplicated),
                        }
                    },
                )
            return {k: v for k, v in found.items() if k not in duplicated}
        if not isinstance(page, dict) or page.get("resourceType") != "Bundle":
            raise StoredReadError("search did not return a Bundle")
        for entry in page.get("entry") or []:
            resource = entry.get("resource") if isinstance(entry, dict) else None
            parsed = _parse(resource) if isinstance(resource, dict) else None
            if parsed is None:
                continue
            if parsed[0] in found:
                duplicated.add(parsed[0])
            found[parsed[0]] = parsed[1]
        page = client_for().next_page(store, page)
    raise StoredReadError("too many search pages")


def count_stranded(client: SearchClient, store: str, patient_ref: str, before: datetime) -> int:
    """Stored ``preliminary`` windows that start before ``before`` (the earliest range start).
    No later run will request them again, so they stay preliminary until a backfill covers
    them. ``_summary=count`` returns only ``Bundle.total``."""
    bundle = client.search(
        store,
        "Observation",
        [
            ("subject", patient_ref),
            ("code", _token(codes.LOINC, codes.HEART_RATE["code"])),
            ("status", "preliminary"),
            ("date", f"lt{_fhir_instant(before)}"),
            ("_summary", "count"),
        ],
    )
    total = bundle.get("total") if isinstance(bundle, dict) else None
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise StoredReadError("count search has no total")
    return total


@dataclass(frozen=True)
class Plan:
    """Which windows to write, and why. Items stay in the order given (oldest first)."""

    write: list[dict]
    unchanged: int
    new: int
    changed: int
    finalised: int
    amended: int
    withdrawn_kept: int = 0
    # Keys (heart_rate_key) of the windows in ``write`` to be written ``amended`` (T33).
    amend_keys: frozenset[str] = frozenset()


def plan_writes(
    items: list[dict],
    stored: dict[str, StoredWindow] | None,
    *,
    key_of: Callable[[dict], tuple[str, datetime, tuple]],
    status_of: Callable[[dict], str],
) -> Plan:
    """Decide per window. ``stored=None`` (the read failed) writes everything.

    - not stored: write (new);
    - stored with the same end and values and the status it would get now, or already final
      with the same values: skip (unchanged);
    - stored preliminary, now complete, same values: write as final (finalised);
    - stored final (or amended), values changed, and complete now: write with status
      ``amended`` (its key goes in ``amend_keys``; R4: "Subsequent to being Final, the
      observation has been modified");
    - stored final, values changed, but it would be ``preliminary`` now (the cutoff moved back,
      for example ``FINAL_AFTER_MINUTES`` was raised): write as ``preliminary`` (changed). T06
      refuses ``amended`` for a window that is not complete, and the data is still changing;
    - stored ``entered-in-error`` or ``cancelled``: skip, whatever the values (withdrawn_kept);
    - otherwise (preliminary with new values): write (changed).
    """
    write: list[dict] = []
    amend_keys: set[str] = set()
    unchanged = new = changed = finalised = amended = withdrawn = 0
    for item in items:
        key, end, values = key_of(item)
        if stored is None or key not in stored:
            new += 1
            write.append(item)
            continue
        before = stored[key]
        if before.status in WITHDRAWN_STATUSES:
            withdrawn += 1
            continue
        same = before.values == values and before.end == end
        wanted = status_of(item)
        if same and (before.status == wanted or before.is_final):
            unchanged += 1
            continue
        write.append(item)
        if same:
            finalised += 1
        elif before.is_final and wanted == "final":
            amended += 1
            amend_keys.add(key)
        else:
            changed += 1
    return Plan(
        write, unchanged, new, changed, finalised, amended, withdrawn, frozenset(amend_keys)
    )
