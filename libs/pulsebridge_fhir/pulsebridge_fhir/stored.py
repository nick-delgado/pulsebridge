"""What the FHIR store already holds for a range of heart-rate windows, and what to write.

"Read once, write only what changed" (tasks T09, T10, T33; one shared copy since T35). Without
it, every run over a range rewrites every window it covers: a scheduled run that re-requests
the last few hours would write each five-minute window several times (``preliminary``, then
``final`` on every later run that still covers it), and with versioning on, each rewrite stores
a new version of the Observation and of its Provenance.

This module is the pure FHIR part, with no I/O: the search parameters, how one search page is
read, and the decision per window (:func:`plan_writes`). The search itself, following ``next``
links through the Cloud Healthcare client, is :func:`pulsebridge_gcp.stored.stored_windows`.

The search is standard R4, one query for the whole range::

    Observation?subject=Patient/<id>&code=http://loinc.org|8867-4&date=ge<start>&date=lt<end>
               &_elements=identifier,status,effectivePeriod,valueQuantity,component&_count=1000

``date`` on an Observation searches ``effective[x]``; for an ``effectivePeriod`` R4 matches when
the period overlaps the searched range, so the window that ends exactly at the range start is
also returned. That is harmless: it is simply not among the windows being written.

``_elements`` asks only for the fields compared here. Values are compared, never logged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from pulsebridge_fhir import codes
from pulsebridge_fhir.keys import escape_search_value

SEARCH_PAGE_SIZE = 1000  # 14 days of windows is 4,032: five pages at most
ELEMENTS = "identifier,status,effectivePeriod,valueQuantity,component"
# R4 observation-status codes that mean "complete": final, and its two later revisions.
FINAL_STATUSES = ("final", "amended", "corrected")
# R4 observation-status: "entered-in-error" ("the observation has been withdrawn following
# previous final release") and "cancelled" ("the observation is unavailable because the
# measurement was not started or not completed"). Either is a decision someone made about the
# stored copy; rewriting the window as final would undo it, so such windows are left alone.
WITHDRAWN_STATUSES = ("entered-in-error", "cancelled")


class StoredReadError(Exception):
    """A search answered something this module cannot use (not the HTTP errors, which the
    client raises itself). The caller then writes every window."""


@dataclass(frozen=True)
class StoredWindow:
    """The parts of a stored heart-rate Observation that decide whether to write it again."""

    status: str
    end: datetime | None
    values: tuple[float | int | None, float | int | None, float | int | None]  # min, avg, max

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_STATUSES


def search_instant(value: datetime) -> str:
    """An R4 ``instant`` in UTC with ``Z`` and whole seconds, for a ``date`` search prefix."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def search_token(system: str, value: str) -> str:
    """An R4 token search value ``<system>|<code>``, each part escaped (R4 search, "Escaping
    Search Parameters")."""
    return f"{escape_search_value(system)}|{escape_search_value(value)}"


def stored_window_search_params(
    patient_ref: str, start: datetime, end: datetime
) -> list[tuple[str, str]]:
    """The Observation search for every heart-rate window overlapping ``[start, end)``.

    Pairs, not a mapping, because ``date`` is repeated (``ge`` and ``lt``)."""
    return [
        ("subject", patient_ref),
        # LOINC 8867-4 "Heart rate", the code of every heart-rate Observation T06 builds.
        ("code", search_token(codes.LOINC, codes.HEART_RATE["code"])),
        ("date", f"ge{search_instant(start)}"),
        ("date", f"lt{search_instant(end)}"),
        ("_elements", ELEMENTS),
        ("_count", str(SEARCH_PAGE_SIZE)),
    ]


def _number(quantity: object) -> float | int | None:
    if isinstance(quantity, dict):
        value = quantity.get("value")
        if isinstance(value, int | float) and not isinstance(value, bool):
            return value
    return None


def _array(element: dict, name: str) -> list:
    """An R4 element that is an array in JSON (``identifier``, ``component``, ``coding``):
    absent is empty; any other shape makes the page unusable (FHIR JSON never sends a single
    value, ``null`` or a number in place of an array)."""
    value = element.get(name)
    if value is None:
        return []
    if not isinstance(value, list):
        raise StoredReadError(f"{name} is not an array")
    return value


def _object(element: dict, name: str) -> dict:
    """An R4 element that is an object in JSON (``component.code``, a CodeableConcept)."""
    value = element.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise StoredReadError(f"{name} is not an object")
    return value


def _component(resource: dict, code: str) -> float | int | None:
    """``Observation.component[code=<LOINC code>].valueQuantity.value`` (T06 puts the minimum
    and maximum there: LOINC 96935-2 and 55426-1, ``codes.HEART_RATE_MIN`` and ``_MAX``)."""
    for component in _array(resource, "component"):
        if not isinstance(component, dict):
            continue
        codings = _array(_object(component, "code"), "coding")
        if any(
            isinstance(c, dict) and c.get("system") == codes.LOINC and c.get("code") == code
            for c in codings
        ):
            return _number(component.get("valueQuantity"))
    return None


def parse_stored_window(resource: dict) -> tuple[str, StoredWindow] | None:
    """``(idempotency key, StoredWindow)`` of one stored heart-rate Observation, or ``None``
    when it has no key in ``urn:pulsebridge:observation`` or no status (not one of ours).

    An end or value that cannot be read becomes ``None``, which never equals a window's real
    end or values, so that window is written again. ``identifier``, ``component`` or
    ``component.coding`` that is not an array, or a ``component.code`` that is not an object,
    raises :class:`StoredReadError` (the read fails and every window is written)."""
    key = next(
        (
            i.get("value")
            for i in _array(resource, "identifier")
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


def page_windows(page: object) -> list[tuple[str, StoredWindow]]:
    """The windows on one search page, in page order (a key may appear twice; the caller
    decides what that means). Raises :class:`StoredReadError` unless the page is a ``Bundle``
    whose ``entry`` is absent or a list (R4 JSON: ``Bundle.entry`` is an array), and when an
    Observation's arrays or ``component.code`` have another shape (:func:`parse_stored_window`).
    Entries that are not one of our heart-rate Observations are skipped."""
    if not isinstance(page, dict) or page.get("resourceType") != "Bundle":
        raise StoredReadError("search did not return a Bundle")
    entries = page.get("entry", [])
    if not isinstance(entries, list):
        raise StoredReadError("search entry is not a list")
    found = []
    for entry in entries:
        resource = entry.get("resource") if isinstance(entry, dict) else None
        parsed = parse_stored_window(resource) if isinstance(resource, dict) else None
        if parsed is not None:
            found.append(parsed)
    return found


@dataclass(frozen=True)
class Plan:
    """Which windows to write, and why. Items stay in the order given (oldest first)."""

    write: list[dict]
    unchanged: int = 0
    new: int = 0
    changed: int = 0
    finalised: int = 0
    amended: int = 0
    withdrawn_kept: int = 0
    # Stored final, values changed, but still filling by the cutoff: not written (T33 review).
    held_final: int = 0
    # Keys (heart_rate_key) of the windows in ``write`` to be written ``amended`` (T33).
    amend_keys: frozenset[str] = frozenset()


def plan_writes(
    items: list[dict],
    stored: dict[str, StoredWindow] | None,
    *,
    key_of: Callable[[dict], tuple[str, datetime, tuple]],
    status_of: Callable[[dict], str],
) -> Plan:
    """Decide per window. ``stored=None`` (no read, or the read failed) writes everything.

    ``key_of(item)`` gives the window's ``(idempotency key, end, (min, avg, max))`` and
    ``status_of(item)`` the status it would be written with now (``preliminary`` or ``final``).

    - not stored: write (new);
    - stored ``entered-in-error`` or ``cancelled``: skip, whatever the values (withdrawn_kept);
    - stored with the same end and values and the status it would get now, or already final
      with the same values: skip (unchanged);
    - stored final (or amended), end or values changed, and complete now: write with status
      ``amended`` (its key goes in ``amend_keys``; R4 observation-status ``amended``:
      "Subsequent to being Final, the observation has been modified");
    - stored final, changed, but it would be ``preliminary`` now (the cutoff moved back, for
      example ``FINAL_AFTER_MINUTES`` was raised): **not written**, the stored ``final`` is
      kept (held_final). R4 has no path from ``final`` back to ``preliminary``, and the window
      may already have been shared; T06 refuses ``amended`` for a window that is not complete.
      Once the cutoff passes it, a later run writes it ``amended``;
    - stored preliminary, same values, now due final: write (finalised);
    - otherwise (stored preliminary, end or values changed): write (changed).
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
        if same and (before.status == wanted or before.is_final):
            unchanged += 1
            continue
        if before.is_final and wanted != "final":
            # Changed after final, but still filling now: never write it back as preliminary.
            held += 1
            continue
        write.append(item)
        if same:
            finalised += 1
        elif before.is_final:
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
