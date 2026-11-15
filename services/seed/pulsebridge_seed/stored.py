"""What the FHIR store already holds for the run's range: read once, write only what changed.

**Copied, in reduced form, from ``services/health-sync/health_sync/stored.py``** (task T10):
``stored_windows`` and ``plan_writes`` follow its logic and edge cases. Services do not import
from each other, and sharing it would need a change to a library, so the minimal part is copied
here; a later task can move both into ``libs/`` (reported to the orchestrator).

Without this, a scheduled top-up over the last few hours rewrote every window it covered on
every run: each window three or more times (preliminary, then final on each later run that
still covered it), and with versioning on, each rewrite stores a new version of the Observation
and of its Provenance. With it, a window is written when it is new, when it has become due for
``final``, or (only after a change of seed or zone) when its values differ: then ``amended``
if it was already final.

One search per run, R4 standard parameters, following ``next`` links::

    Observation?subject=Patient/<id>&code=http://loinc.org|8867-4&date=ge<start>&date=lt<end>
               &_elements=identifier,status,effectivePeriod,valueQuantity,component&_count=1000

``date`` on an ``effectivePeriod`` matches when the period overlaps the range, so the window
ending exactly at the range start is returned too; it is simply not among the windows written.

If the search fails or answers something unusable, every window is written, as before: reading
saves writes, it is never a condition for writing. Values are compared, never logged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pulsebridge_fhir import codes
from pulsebridge_fhir.keys import escape_search_value

SEARCH_PAGE_SIZE = 1000
ELEMENTS = "identifier,status,effectivePeriod,valueQuantity,component"
FINAL_STATUSES = ("final", "amended", "corrected")
# R4 observation-status "entered-in-error" ("withdrawn following previous final release") and
# "cancelled": a decision someone made about the stored copy. Rewriting the window would undo
# it, so such windows are left alone (as health-sync does).
WITHDRAWN_STATUSES = ("entered-in-error", "cancelled")

logger = logging.getLogger("pulsebridge_seed")


class SearchClient(Protocol):
    def search(self, fhir_store: str, resource_type: str, params: object) -> dict: ...
    def next_page(self, fhir_store: str, bundle: dict) -> dict | None: ...


class StoredReadError(Exception):
    """A search answered something this module cannot use (HTTP errors are the client's)."""


@dataclass(frozen=True)
class StoredWindow:
    status: str
    end: datetime | None
    values: tuple[float | int | None, float | int | None, float | int | None]  # min, avg, max


def _instant(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _token(system: str, value: str) -> str:
    return f"{escape_search_value(system)}|{escape_search_value(value)}"


def _number(quantity: object) -> float | int | None:
    if isinstance(quantity, dict):
        value = quantity.get("value")
        if isinstance(value, int | float) and not isinstance(value, bool):
            return value
    return None


def _component(resource: dict, code: str) -> float | int | None:
    for component in resource.get("component") or []:
        if not isinstance(component, dict):
            continue
        codings = (component.get("code") or {}).get("coding") or []
        if any(
            isinstance(c, dict) and c.get("system") == codes.LOINC and c.get("code") == code
            for c in codings
        ):
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
    client: SearchClient, store: str, patient_ref: str, start: datetime, end: datetime
) -> dict[str, StoredWindow]:
    """Every stored heart-rate window overlapping ``[start, end)``, by idempotency key.

    A key found more than once (two Observations with one identifier, which should not exist)
    is left out, so that window is written and its conditional update reports the duplicate.
    Only the count is logged. Pages are bounded by the range: a server may send smaller pages
    than ``_count`` asks for, but every useful page holds at least one window, so more pages
    than windows (plus one) means a ``next`` chain that would never end."""
    params = [
        ("subject", patient_ref),
        ("code", _token(codes.LOINC, codes.HEART_RATE["code"])),
        ("date", f"ge{_instant(start)}"),
        ("date", f"lt{_instant(end)}"),
        ("_elements", ELEMENTS),
        ("_count", str(SEARCH_PAGE_SIZE)),
    ]
    max_pages = int((end - start).total_seconds() // 300) + 2
    found: dict[str, StoredWindow] = {}
    duplicated: set[str] = set()
    page: dict | None = client.search(store, "Observation", params)
    for _ in range(max_pages):
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
        entries = page.get("entry", [])
        if not isinstance(entries, list):
            raise StoredReadError("search entry is not a list")
        for entry in entries:
            resource = entry.get("resource") if isinstance(entry, dict) else None
            parsed = _parse(resource) if isinstance(resource, dict) else None
            if parsed is None:
                continue
            if parsed[0] in found:
                duplicated.add(parsed[0])
            found[parsed[0]] = parsed[1]
        page = client.next_page(store, page)
    raise StoredReadError("too many search pages")


@dataclass(frozen=True)
class Plan:
    write: list[dict]  # in the order given (oldest first)
    unchanged: int = 0
    new: int = 0
    changed: int = 0
    finalised: int = 0
    amended: int = 0
    withdrawn_kept: int = 0
    # Stored final, values changed, but it would be preliminary now: not written.
    held_final: int = 0
    # Keys (heart_rate_key) of the windows in ``write`` to be written ``amended`` (T33).
    amend_keys: frozenset[str] = frozenset()


def plan_writes(
    items: list[dict],
    stored: dict[str, StoredWindow] | None,
    *,
    key_of,
    status_of,
) -> Plan:
    """Decide per window, as health-sync does. ``stored=None`` (no read, or it failed) writes
    everything.

    - not stored: write (new);
    - stored ``entered-in-error`` or ``cancelled``: skip, whatever the values (withdrawn_kept);
    - same end and values, and stored with the status it would get now or already final: skip
      (unchanged);
    - stored final (or amended), end or values changed, and complete now: write with status
      ``amended`` (its key goes in ``amend_keys``). R4 observation-status ``amended``:
      "Subsequent to being Final, the observation has been modified". Here that happens only
      after a change of ``--seed`` or ``TIME_ZONE``;
    - stored final, changed, but it would be ``preliminary`` now (for example
      ``FINAL_AFTER_MINUTES`` was raised): **not written**, the stored ``final`` is kept
      (held_final). R4 has no path from ``final`` back to ``preliminary``, the window may already
      have been shared, and T06 refuses ``amended`` for a window that is not complete. A later
      run writes it ``amended`` once the cutoff passes it;
    - same values, stored preliminary, now due final: write (finalised);
    - otherwise (preliminary, values changed): write (changed).
    """
    write: list[dict] = []
    amend_keys: set[str] = set()
    unchanged = new = changed = finalised = amended = withdrawn = held = 0
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
        was_final = before.status in FINAL_STATUSES
        if same and (before.status == wanted or was_final):
            unchanged += 1
            continue
        if was_final and wanted != "final":
            held += 1  # changed after final, but still filling now: never written back
            continue
        write.append(item)
        if same:
            finalised += 1
        elif was_final:
            amended += 1
            amend_keys.add(key)
        else:
            changed += 1
    return Plan(
        write,
        unchanged,
        new,
        changed,
        finalised,
        amended,
        withdrawn,
        held_final=held,
        amend_keys=frozenset(amend_keys),
    )
