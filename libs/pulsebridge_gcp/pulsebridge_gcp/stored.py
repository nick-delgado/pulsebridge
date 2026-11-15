"""Read the heart-rate windows already stored for a range: one search, following ``next`` links.

Shared by ``health-sync`` (T10) and the seeder (T09) since T35. The FHIR side (the search
parameters, how a page is read, and what to write afterwards with ``plan_writes``) is pure and
lives in :mod:`pulsebridge_fhir.stored`; this module only makes the requests, through a client
with ``search`` and ``next_page`` (:class:`pulsebridge_gcp.HealthcareClient`).

The two services differ in two ways, both parameters here:

- ``client_for``: a client for each request. ``health-sync`` builds a fresh one per page, so
  each page's timeout is cut to the time its run has left; the seeder passes the same client
  every time (``lambda: client``).
- ``max_pages``: how many pages may be read before the read gives up. ``health-sync`` uses the
  default, :data:`MAX_SEARCH_PAGES`; the seeder bounds it by its range
  (:func:`max_pages_for_range`), because its range can be a year long.

If a search fails (an HTTP error from the client, or :class:`StoredReadError` from here), the
caller writes everything: reading is an optimisation, never a condition for writing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from pulsebridge_fhir.stored import (
    StoredReadError,
    StoredWindow,
    page_windows,
    stored_window_search_params,
)

# health-sync's bound: its longest range (a 14-day backfill piece, 4,032 windows) is five pages
# of SEARCH_PAGE_SIZE.
MAX_SEARCH_PAGES = 20
WINDOW_SECONDS = 300  # the five-minute roll-up window (decision D8)

_default_logger = logging.getLogger("pulsebridge_gcp")


class SearchClient(Protocol):
    def search(self, fhir_store: str, resource_type: str, params: object) -> dict: ...
    def next_page(self, fhir_store: str, bundle: dict) -> dict | None: ...


def max_pages_for_range(start: datetime, end: datetime) -> int:
    """A page bound that grows with the range (the seeder's): a server may send smaller pages
    than ``_count`` asks for, but every useful page holds at least one window, so more pages
    than windows (plus one) means a ``next`` chain that would never end."""
    return int((end - start).total_seconds() // WINDOW_SECONDS) + 2


def stored_windows(
    client_for: Callable[[], SearchClient],
    store: str,
    patient_ref: str,
    start: datetime,
    end: datetime,
    *,
    max_pages: int = MAX_SEARCH_PAGES,
    logger: logging.Logger | None = None,
) -> dict[str, StoredWindow]:
    """Every stored heart-rate window overlapping ``[start, end)``, by idempotency key.

    ``client_for()`` is called once per request: the first search, then each ``next_page``
    (including the last one, which finds no ``next`` link). The read stops with
    :class:`StoredReadError` after ``max_pages`` pages, and when a page is not a usable
    ``Bundle``.

    A key found more than once (two Observations with one identifier, which should not exist)
    is left out, so the window is written; the write's conditional update then reports the
    duplicate itself. Only the count is logged, never the keys, on ``logger`` (the caller's,
    so the warning carries the service's logger name)."""
    log = logger or _default_logger
    params = stored_window_search_params(patient_ref, start, end)
    found: dict[str, StoredWindow] = {}
    duplicated: set[str] = set()
    page: dict | None = client_for().search(store, "Observation", params)
    for _ in range(max_pages):
        if page is None:
            if duplicated:
                log.warning(
                    "an observation key is stored more than once; writing those windows",
                    extra={
                        "fields": {
                            "event": "stored-duplicate-key",
                            "duplicate_keys": len(duplicated),
                        }
                    },
                )
            return {k: v for k, v in found.items() if k not in duplicated}
        for key, window in page_windows(page):
            if key in found:
                duplicated.add(key)
            found[key] = window
        page = client_for().next_page(store, page)
    raise StoredReadError("too many search pages")
