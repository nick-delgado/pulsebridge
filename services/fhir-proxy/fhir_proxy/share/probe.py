"""The status strip's ``openemr``: is OpenEMR reachable, cheaply and within bounds?

One ``GET /meta/health/readyz`` (the readiness endpoint module openemr's boot hook waits on,
answered without a token) over the same pinned TLS as the share. So a ``reachable: true`` means
the TLS handshake succeeded against OpenEMR's own certificate for the configured IP AND OpenEMR
said it is ready. No token is fetched: the probe never uses the client's key.

``detail`` codes (short, never the server's text): ``not-configured`` (no OpenEMR settings),
``client-not-registered`` (no client document yet), ``client-document-invalid``,
``tls-verification-failed``, ``timeout``, ``unavailable`` (no connection), ``not-ready`` (an
answer other than 200), ``error`` (anything else). The result is reused for
``PROBE_CACHE_SECONDS`` so a reloading page does not hammer the e2-micro VM. The call is capped
by ``/status``'s request budget and by ``PROBE_TIMEOUT_SECONDS``; it never raises.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from fhir_proxy import deadline
from fhir_proxy.config import OpenEmrSettings
from fhir_proxy.logs import log
from fhir_proxy.share.document import DocumentProblem, load_document
from fhir_proxy.share.transport import OpenEmrError, OpenEmrTimeout, Transport

READINESS_PATH = "/meta/health/readyz"
PROBE_TIMEOUT_SECONDS = 5.0
PROBE_CACHE_SECONDS = 30.0


def _text(now: datetime) -> str:
    return now.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


class OpenEmrProbe:
    def __init__(
        self,
        settings: OpenEmrSettings | None,
        transport: Transport | None,
        *,
        clock: Callable[[], datetime],
        monotonic: Callable[[], float],
        cache_seconds: float = PROBE_CACHE_SECONDS,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._clock = clock
        self._monotonic = monotonic
        self._cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cached: tuple[float, dict] | None = None

    def __call__(self) -> dict:
        now = self._clock()
        if self._settings is None or self._transport is None:
            return {"reachable": False, "checkedAt": _text(now), "detail": "not-configured"}
        with self._lock:
            cached = self._cached
        if cached is not None and self._monotonic() - cached[0] < self._cache_seconds:
            return cached[1]
        result = {"reachable": False, "checkedAt": _text(now), "detail": self._check()}
        result["reachable"] = result["detail"] is None
        with self._lock:
            self._cached = (self._monotonic(), result)
        return result

    def _check(self) -> str | None:
        try:
            document = load_document(self._settings)  # type: ignore[arg-type]
        except DocumentProblem as problem:
            if problem.code == "missing":
                return "client-not-registered"
            return "client-document-invalid"
        try:
            answer = self._transport.request(  # type: ignore[union-attr]
                "GET",
                READINESS_PATH,
                query=None,
                headers={"Accept": "*/*"},
                body=None,
                timeout=deadline.capped(PROBE_TIMEOUT_SECONDS),
                context=document.ssl_context(),
            )
        except (deadline.BudgetExhausted, OpenEmrTimeout):
            return "timeout"
        except OpenEmrError as exc:
            log(logging.WARNING, "openemr-probe-failed", reason=exc.reason)
            if exc.reason == "tls-verification-failed":
                return "tls-verification-failed"
            return "unavailable"
        except Exception as exc:  # noqa: BLE001 - the probe never raises
            log(logging.WARNING, "openemr-probe-failed", error_type=type(exc).__name__)
            return "error"
        return None if answer.status == 200 else "not-ready"
