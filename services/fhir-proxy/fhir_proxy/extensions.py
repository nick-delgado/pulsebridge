"""Where T15's share to OpenEMR plugs in, without editing the rest of the proxy.

T15 adds the package ``fhir_proxy/share/`` with one function in its ``__init__.py``:

```python
def register(proxy: ProxyContext) -> None:
    proxy.add_route(Route("POST", "/share", "share", "view", "Share Observations with OpenEMR"),
                    handler)
    proxy.set_openemr_probe(probe)      # optional: fills the status strip's `openemr`
```

``create_app`` imports it if it exists and calls ``register`` once at start-up. What the share
code gets from :class:`ProxyContext`:

- ``add_route``: the route goes into the same route table as every other, so it is behind the
  same session-token check (only ``POST /auth/login`` is public, and an extension cannot add a
  public route), gets CORS, the security headers and the error handling, and appears in the
  CapabilityStatement's route list.
- ``stores``: read access to the health-data store with the patient pin
  (``stores.patient_references()``, ``stores.read(...)``, ``stores.is_ours(...)``).
- ``config``: the proxy's validated configuration (T15 adds its own variables in its package).
- ``Problem`` (``fhir_proxy.errors``): raise it for a refusal; it becomes an OperationOutcome
  with the given status (422 before OpenEMR is called, 502 if OpenEMR is unreachable).
- ``trace_step`` (below): one step of the share trace in the dashboard's ``ShareStep`` shape,
  with secrets replaced.
- ``log`` (``fhir_proxy.logs.log``): structured logs; never a token, key, assertion or value.
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Callable
from dataclasses import dataclass, field

from fhir_proxy.config import ServiceConfig
from fhir_proxy.routes import Route
from fhir_proxy.store import Stores

EXTENSION_MODULE = "fhir_proxy.share"
# Keys whose values never reach a trace, at any depth (OAuth 2.0 and JWT bearer assertion names).
SECRET_KEYS = frozenset(
    {"client_assertion", "access_token", "refresh_token", "id_token", "assertion", "private_key"}
)
NOT_SHOWN = "[not shown]"


def redact(value: object) -> object:
    """A copy of a JSON value with every secret key's value replaced by ``[not shown]``."""
    if isinstance(value, dict):
        return {k: (NOT_SHOWN if k in SECRET_KEYS else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def trace_step(
    step: str,
    label: str,
    *,
    method: str,
    path: str,
    request_body: object,
    status: int,
    response_body: object,
    result: str,
    duration_ms: int,
) -> dict:
    """One ``ShareStep`` (web/src/api/contract.ts). ``path`` must be a path only, never a URL
    with a host: OpenEMR's screens are not reachable and its host is not the page's business."""
    if not path.startswith("/") or "//" in path:
        raise ValueError("a trace step's url is a path")
    return {
        "step": step,
        "label": label,
        "request": {"method": method, "url": path, "body": redact(request_body)},
        "response": {"status": status, "body": redact(response_body)},
        "result": result,
        "durationMs": duration_ms,
    }


@dataclass
class ProxyContext:
    config: ServiceConfig
    stores: Stores
    routes: list[tuple[Route, Callable]] = field(default_factory=list)
    openemr_probe: Callable[[], dict] | None = None

    def add_route(self, route: Route, handler: Callable) -> None:
        if route.public:
            raise ValueError("an extension cannot add a route without the session-token check")
        self.routes.append((route, handler))

    def set_openemr_probe(self, probe: Callable[[], dict]) -> None:
        self.openemr_probe = probe


def load_extension(context: ProxyContext, module: str = EXTENSION_MODULE) -> bool:
    """Import and register the share extension if it is installed. True if it was."""
    if importlib.util.find_spec(module) is None:
        return False
    importlib.import_module(module).register(context)
    return True
