"""The share to OpenEMR (T15), plugged into fhir-proxy through ``fhir_proxy.extensions``.

``register`` adds ``POST <prefix>/share`` (token-gated, a ``view`` route: listed in the
CapabilityStatement's ``rest.documentation``, not as a FHIR interaction, because it is not one)
and the status strip's OpenEMR probe. With no OpenEMR settings it still registers both: the
route then answers 503 ``share-not-configured`` and the probe ``not-configured``.

Nothing here reads a file, opens a connection or starts a thread at start-up (gunicorn
``--preload`` forks after ``create_app``): the client document is read on each share.
See README.md "Share to OpenEMR (T15)".
"""

from __future__ import annotations

from fhir_proxy.extensions import ProxyContext
from fhir_proxy.routes import Route
from fhir_proxy.share.oauth import TokenCache
from fhir_proxy.share.probe import OpenEmrProbe
from fhir_proxy.share.service import ShareService
from fhir_proxy.share.transport import HttpsTransport

ROUTE = Route(
    "POST",
    "/share",
    "share",
    "view",
    "Share one final Observation with OpenEMR (the clinic); answers the steps and the read-back",
)


def register(proxy: ProxyContext) -> None:
    settings = proxy.config.openemr
    transport = proxy.options.get("openemr_transport")
    if transport is None and settings is not None:
        transport = HttpsTransport(settings.host, settings.port)
    service = ShareService(
        settings=settings,
        stores=proxy.stores,
        transport=transport,  # type: ignore[arg-type]
        tokens=TokenCache(proxy.monotonic),
        clock=proxy.clock,
        history_days=proxy.config.history_days,
    )
    proxy.add_route(ROUTE, service.handle)
    proxy.set_openemr_probe(
        OpenEmrProbe(settings, transport, clock=proxy.clock, monotonic=proxy.monotonic)  # type: ignore[arg-type]
    )
