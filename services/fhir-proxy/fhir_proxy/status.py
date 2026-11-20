"""``GET /status``: the header strip's data, always answered with 200.

``watchSync`` is health-sync's ``GET /status`` (its README): it answers 200 in every state, so
**any other answer, or none, means health-sync itself failed**, which is reported as
``state: "unknown"`` with a ``reason`` code and never as a proxy 5xx or as a disconnection.
health-sync's ``googleHealth`` is sent as ``state`` (the dashboard README asks for it), and its
``reason``, ``notes``, ``strandedPreliminary`` and ``checkedAt`` are passed on after checking:
an unknown state becomes ``unknown``, unknown notes are dropped, a count that is not a whole
number becomes ``null`` ("not known", never zero).

The rest comes from the FHIR store, in three cheap searches, reused for
``STATUS_CACHE_SECONDS`` (default 60 s; the strip reloads every 5 minutes):

- ``lastSyncAt``: the **time of the newest heart-rate write**, ``meta.lastUpdated`` of the most
  recently updated heart-rate Observation (``_sort=-_lastUpdated&_count=1``). health-sync writes
  only windows that changed, so this moves only when there is new data (README, open question 9).
- ``observationCounts``: ``_total=accurate&_count=0`` (Cloud Healthcare has no
  ``_summary``) for heart rate (watch) and the blood-pressure panel
  (cuff), for the configured patient, inside the shown window (T47: ``date=ge<boundary>``, the
  boundary taken when the counts are made; they are reused for ``STATUS_CACHE_SECONDS``).
  ``lastSyncAt`` is limited to the window the same way.
- ``fhirStore``: reachable when those searches answered. When they did not (an error, or the
  request's time budget ran out), the counts are ``null`` ("not known", never 0) and
  ``lastSyncAt`` is null.

health-sync and the three searches run at the same time, so the worst case is the slower of
health-sync's timeout and one store search, both capped by the request budget (50 s), not
their sum. Any failure of the health-sync call, even an unexpected exception, is
``state: "unknown"`` with ``reason: "health-sync-error"``: ``/status`` never answers 5xx.

``openemr`` is the share extension's probe (T15, ``fhir_proxy.share.probe``), run at the same
time as the rest: ``not-configured`` without the OpenEMR settings, otherwise a TLS handshake
pinned to OpenEMR's certificate and its readiness endpoint, cached briefly. Without an extension
it says ``not-checked``.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from pulsebridge_gcp import HealthcareApiError

from fhir_proxy import deadline
from fhir_proxy.errors import reachability_reason
from fhir_proxy.history import instant_text as boundary_text
from fhir_proxy.history import window_start
from fhir_proxy.logs import log
from fhir_proxy.messages import instant_text
from fhir_proxy.search import BLOOD_PRESSURE_PANEL, HEART_RATE, parse_instant
from fhir_proxy.store import Stores
from fhir_proxy.upstream import UpstreamError, UpstreamTimeout

STATES = ("connected", "reconnect-needed", "not-configured", "unknown")
NOTES = ("backfill-needed", "refresh-token-rotated")
_REASON = re.compile(r"[a-z0-9][a-z0-9:,._-]{0,199}", re.ASCII)


def _now_text(now: datetime) -> str:
    return now.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def unknown_watch(reason: str, now: datetime) -> dict:
    return {
        "state": "unknown",
        "reason": reason,
        "notes": [],
        "strandedPreliminary": None,
        "checkedAt": _now_text(now),
    }


def normalise_health_sync(body: object, now: datetime) -> dict:
    """health-sync's ``/status`` body, checked field by field (see the module docstring)."""
    if not isinstance(body, dict):
        return unknown_watch("health-sync-bad-answer", now)
    state = body.get("googleHealth")
    if state not in STATES:
        return unknown_watch("health-sync-bad-answer", now)
    reason = body.get("reason")
    notes = body.get("notes")
    stranded = body.get("strandedPreliminary")
    checked = body.get("checkedAt")
    return {
        "state": state,
        "reason": reason if isinstance(reason, str) and _REASON.fullmatch(reason) else None,
        "notes": sorted({n for n in notes if n in NOTES}) if isinstance(notes, list) else [],
        "strandedPreliminary": (
            stranded
            if isinstance(stranded, int) and not isinstance(stranded, bool) and stranded >= 0
            else None
        ),
        "checkedAt": (
            checked
            if isinstance(checked, str) and parse_instant(checked) is not None
            else _now_text(now)
        ),
    }


class StatusService:
    def __init__(
        self,
        stores: Stores,
        health_sync: Callable[[], object] | None,
        *,
        cache_seconds: float,
        openemr_probe: Callable[[], dict] | None = None,
        history_days: int = 7,
        workers: int = 8,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stores = stores
        self._health_sync = health_sync
        self._cache_seconds = cache_seconds
        self.openemr_probe = openemr_probe
        self._history_days = history_days
        # Two pools of /status's own: the outer one runs health-sync and the store part, the
        # inner one the store part's three searches. Nothing is submitted into the pool it runs
        # in, and slow /messages requests (which use the shared pool) cannot delay /status.
        self._outer = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="status")
        self._inner = ThreadPoolExecutor(max_workers=workers * 3, thread_name_prefix="status-q")
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._cached: tuple[float, dict] | None = None

    def _watch(self, now: datetime) -> dict:
        if self._health_sync is None:
            return unknown_watch("health-sync-not-deployed", now)
        try:
            body = self._health_sync()
        except UpstreamTimeout:
            log(logging.WARNING, "health-sync-status", outcome="timeout")
            return unknown_watch("health-sync-timeout", now)
        except UpstreamError as exc:
            log(logging.WARNING, "health-sync-status", outcome=exc.reason, http_status=exc.status)
            return unknown_watch(f"health-sync-{exc.reason}", now)
        except Exception as exc:  # noqa: BLE001 - /status never answers 5xx for health-sync
            log(
                logging.WARNING,
                "health-sync-status",
                outcome="error",
                error_type=type(exc).__name__,
            )
            return unknown_watch("health-sync-error", now)
        return normalise_health_sync(body, now)

    def _store_unknown(self, now: datetime, detail: str) -> dict:
        # Counts are null ("not known"), never 0, when the store could not be asked.
        return {
            "lastSyncAt": None,
            "observationCounts": {"watch": None, "cuff": None},
            "fhirStore": {"reachable": False, "checkedAt": _now_text(now), "detail": detail},
        }

    def _from_store(self, now: datetime) -> dict:
        with self._lock:
            cached = self._cached
        if cached is not None and self._monotonic() - cached[0] < self._cache_seconds:
            return cached[1]
        checked = _now_text(now)
        try:
            refs = self._stores.patient_references()
            if refs:
                since = boundary_text(window_start(now, self._history_days))
                # The three searches are independent: run them at once, within the budget.
                parts = deadline.run_parallel(
                    self._inner,
                    [
                        lambda: self._stores.count_observations(HEART_RATE, refs, since),
                        lambda: self._stores.count_observations(BLOOD_PRESSURE_PANEL, refs, since),
                        lambda: self._stores.newest_write(HEART_RATE, refs, since),
                    ],
                )
                for part in parts:
                    if part is deadline.NOT_FINISHED:
                        raise deadline.BudgetExhausted("status searches")
                    if isinstance(part, BaseException):
                        raise part
                # Type guards: only a count or a time ever reaches the response.
                if not all(
                    isinstance(c, int) and not isinstance(c, bool) and c >= 0 for c in parts[:2]
                ) or not (parts[2] is None or isinstance(parts[2], str)):
                    raise ValueError("unexpected status search result")
                counts = {"watch": parts[0], "cuff": parts[1]}
                newest = parts[2]
            else:
                counts, newest = {"watch": 0, "cuff": 0}, None  # no Patient yet: truly no data
            parsed = parse_instant(newest) if newest else None
            result = {
                "lastSyncAt": None if parsed is None else instant_text(parsed),
                "observationCounts": counts,
                "fhirStore": {"reachable": True, "checkedAt": checked, "detail": None},
            }
        except (HealthcareApiError, ValueError) as exc:
            reason = reachability_reason(exc)
            log(logging.WARNING, "fhir-store-status", outcome=reason, error_type=type(exc).__name__)
            return self._store_unknown(now, reason)  # not cached: the next load tries again
        with self._lock:
            self._cached = (self._monotonic(), result)
        return result

    def _openemr(self, now: datetime) -> dict:
        """The share extension's reachability probe (T15), or ``not-checked`` without one. The
        probe bounds itself (TLS handshake plus OpenEMR's readiness endpoint, each call capped
        by the request budget); anything it raises or returns that is not the strip's shape is
        ``error``: /status never answers 5xx for OpenEMR."""
        if self.openemr_probe is None:
            return {"reachable": False, "checkedAt": _now_text(now), "detail": "not-checked"}
        try:
            result = self.openemr_probe()
        except Exception as exc:  # noqa: BLE001 - a probe failure is a state, not a 500
            log(logging.WARNING, "openemr-probe-failed", error_type=type(exc).__name__)
            return {"reachable": False, "checkedAt": _now_text(now), "detail": "error"}
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("reachable"), bool)
            or not isinstance(result.get("checkedAt", ""), str)
            or not (result.get("detail") is None or isinstance(result.get("detail"), str))
        ):
            log(logging.WARNING, "openemr-probe-failed", error_type="unexpected-shape")
            return {"reachable": False, "checkedAt": _now_text(now), "detail": "error"}
        return {
            "reachable": result["reachable"],
            "checkedAt": result.get("checkedAt") or _now_text(now),
            "detail": result.get("detail"),
        }

    def summary(self) -> dict:
        """health-sync, the store and OpenEMR's probe are asked at the same time, each within
        the request's budget; a part not answered in time is unknown (``state: unknown``,
        counts null, OpenEMR ``timeout``)."""
        now = self._clock()
        watch, store, openemr = deadline.run_parallel(
            self._outer,
            [lambda: self._watch(now), lambda: self._from_store(now), lambda: self._openemr(now)],
        )
        if not isinstance(watch, dict):
            watch = unknown_watch("health-sync-timeout", now)
        if not isinstance(store, dict):
            store = self._store_unknown(now, "timeout")
        if not isinstance(openemr, dict):
            openemr = {"reachable": False, "checkedAt": _now_text(now), "detail": "timeout"}
        watch = dict(watch)
        watch["lastSyncAt"] = store["lastSyncAt"]
        return {
            "watchSync": watch,
            "observationCounts": store["observationCounts"],
            "fhirStore": store["fhirStore"],
            "openemr": openemr,
        }
