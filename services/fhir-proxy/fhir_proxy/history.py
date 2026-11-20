"""The shown window (T47, D16): only the last ``HISTORY_DAYS`` days are visible through the proxy.

**Nothing is deleted.** The FHIR stores, the HL7v2 store and OpenEMR keep everything; the proxy
only hides what is older, and it hides it the same way everywhere:

- The boundary is ``now - HISTORY_DAYS``, computed on **every request** with the app's injected
  clock (so the window moves while the service runs, and a test can pin it), and cut to whole
  milliseconds so the instant the proxy compares with is exactly the one it writes into a
  search (``ge2026-01-01T12:00:00.000Z``).
- A **reading** (Observation) is inside when its *effective time*, the clinically relevant time
  (R4 ``Observation.effective[x]``: when it was measured, not when it was stored), reaches the
  boundary: an ``effectiveDateTime`` at or after it, or an ``effectivePeriod`` whose end is at
  or after it. So a heart-rate window that **overlaps** the boundary (starts before it, ends
  after it) is shown. This is R4's own rule for ``date=ge<boundary>`` on a Period ("the range
  above the search value intersects the range of the target value"), so a search with that
  lower bound and this check agree. A Period with no ``end`` is ongoing (R4) and so inside.
- A **send attempt** (its AuditEvent) is inside when its attempt time (T31's ``period.start``,
  the moment it was sent) is at or after the boundary.

Anything older answers exactly like something missing: the same ``404`` body, the same headers
and no extra log line, so nobody can tell older data exists.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fhir_proxy.search import DateBound, ObservationQuery, effective_range

# How far before the current boundary a ``ge`` lower bound in a paging link may lie and still be
# sent to the store unchanged (see :func:`bounded_query`). The dashboard follows its next links
# within seconds; ten minutes is generous.
PAGE_LINK_GRACE = timedelta(minutes=10)


def window_start(now: datetime, days: int) -> datetime:
    """``now - days`` in UTC, cut to whole milliseconds (the precision the proxy writes)."""
    start = (now - timedelta(days=days)).astimezone(UTC)
    return start.replace(microsecond=start.microsecond - start.microsecond % 1000)


def instant_text(value: datetime) -> str:
    """UTC with milliseconds and ``Z``: how the boundary is written into a FHIR date search."""
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def effective_in_window(resource: dict, start: datetime) -> bool:
    """Whether an Observation's effective time reaches ``start`` (see the module docstring).
    One with no readable effective time is not shown: no date search would match it either."""
    span = effective_range(resource)
    if span is None:
        return False
    _, end = span
    return end is None or end >= start


def attempt_in_window(sent_at: datetime, start: datetime) -> bool:
    """Whether a send attempt, by its attempt time, is inside the window."""
    return sent_at >= start


def bounded_query(query: ObservationQuery, start: datetime) -> ObservationQuery:
    """The Observation search with the window's boundary as its lower date bound.

    FHIR date prefixes (R4 search.html, "prefixes"): ``ge`` greater or equal, ``gt`` greater
    than, ``le`` / ``lt`` likewise below. A search's lower bound is a ``ge`` or ``gt`` value.

    - No lower bound: ``ge<boundary>`` is added.
    - A lower bound before the boundary is **raised** to ``ge<boundary>``, not refused: the
      dashboard asks for "the last 30 days" or an aligned "last 7 days" whose start is a little
      before the proxy's own boundary, and should simply get what is shown.
    - A lower bound at or after the boundary is kept (it is already narrower).
    - Paging: a next link carries the bound the store was given (``own_params``), so page 2
      asks the store the same question as page 1. By the time it is followed the boundary has
      moved on by a few seconds; a ``ge`` bound with a ``_page_token`` that lies at most
      ``PAGE_LINK_GRACE`` before the boundary is therefore kept as it is, so the store's page
      token still belongs to the query it is sent with (Cloud Healthcare's rule for a mismatch
      is not documented). What such a page returns is still checked against the current
      boundary (:func:`effective_in_window`), so nothing older is shown either way.

    An upper bound is never changed, and a range that ends before the boundary is not refused:
    R4's Period rule can still match a window that overlaps both (the store and the check
    decide; usually the answer is empty).
    """
    lower = query.lower
    if (
        lower is not None
        and query.page_token is not None
        and lower.prefix == "ge"
        and start - PAGE_LINK_GRACE <= lower.value < start
    ):
        return query
    if lower is not None and lower.value >= start:
        return query
    return replace(query, lower=DateBound("ge", start, instant_text(start)))
