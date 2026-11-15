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
- ``observationCounts``: ``_summary=count`` for heart rate (watch) and the blood-pressure panel
  (cuff), for the configured patient.
- ``fhirStore``: reachable when those searches answered.

``openemr`` is checked by the share extension (T15) when it is installed; until then it says
so (``reachable: false``, ``detail: "not-checked"``).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from pulsebridge_gcp import HealthcareApiError

from fhir_proxy.errors import reachability_reason
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
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stores = stores
        self._health_sync = health_sync
        self._cache_seconds = cache_seconds
        self.openemr_probe = openemr_probe
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
        return normalise_health_sync(body, now)

    def _from_store(self, now: datetime) -> dict:
        with self._lock:
            cached = self._cached
        if cached is not None and self._monotonic() - cached[0] < self._cache_seconds:
            return cached[1]
        checked = _now_text(now)
        try:
            refs = self._stores.patient_references()
            if refs:
                counts = {
                    "watch": self._stores.count_observations(HEART_RATE, refs),
                    "cuff": self._stores.count_observations(BLOOD_PRESSURE_PANEL, refs),
                }
                newest = self._stores.newest_write(HEART_RATE, refs)
            else:
                counts, newest = {"watch": 0, "cuff": 0}, None
            parsed = parse_instant(newest) if newest else None
            result = {
                "lastSyncAt": None if parsed is None else instant_text(parsed),
                "observationCounts": counts,
                "fhirStore": {"reachable": True, "checkedAt": checked, "detail": None},
            }
        except (HealthcareApiError, ValueError) as exc:
            reason = reachability_reason(exc)
            log(logging.WARNING, "fhir-store-status", outcome=reason, error_type=type(exc).__name__)
            # Not cached: the next load tries again.
            return {
                "lastSyncAt": None,
                "observationCounts": {"watch": 0, "cuff": 0},
                "fhirStore": {"reachable": False, "checkedAt": checked, "detail": reason},
            }
        with self._lock:
            self._cached = (self._monotonic(), result)
        return result

    def summary(self) -> dict:
        now = self._clock()
        watch = self._watch(now)
        store = self._from_store(now)
        watch["lastSyncAt"] = store["lastSyncAt"]
        openemr = {"reachable": False, "checkedAt": _now_text(now), "detail": "not-checked"}
        if self.openemr_probe is not None:
            try:
                openemr = self.openemr_probe()
            except Exception as exc:  # noqa: BLE001 - a probe failure is a state, not a 500
                log(logging.WARNING, "openemr-probe-failed", error_type=type(exc).__name__)
                openemr = {"reachable": False, "checkedAt": _now_text(now), "detail": "error"}
        return {
            "watchSync": watch,
            "observationCounts": store["observationCounts"],
            "fhirStore": store["fhirStore"],
            "openemr": openemr,
        }
