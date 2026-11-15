"""Where T15's share to OpenEMR plugs in, without editing the rest of the proxy.

T15 adds the package ``services/fhir-proxy/fhir_proxy/share/`` (tests under
``services/fhir-proxy/tests/share/``) with one function in its ``__init__.py``:

```python
def register(proxy: ProxyContext) -> None:
    proxy.add_route(Route("POST", "/share", "share", "view", "Share Observations with OpenEMR"),
                    handler)
    proxy.set_openemr_probe(probe)      # optional: fills the status strip's `openemr`
```

``create_app`` imports it if it exists and calls ``register`` once at start-up. What the share
code gets from :class:`ProxyContext`:

- ``add_route``: the route joins the same route table, so it is behind the same session-token
  check, gets CORS, the security headers, the request budget and the error handling, and appears
  in the CapabilityStatement's route list. The public routes are fixed by the core table (login
  only): an extension cannot add a public route, reuse a core endpoint name, or register a
  method and path that already exist.
- ``stores``: read access to the health-data store with the patient pin
  (``stores.patient_references()``, ``stores.read(...)``, ``stores.is_ours(...)``).
- ``config``: the proxy's validated configuration (T15 adds its own variables in its package).
- ``read_body(limit)`` (``fhir_proxy.bodies``): the request body, read completely or refused
  with 413; never ``request.get_data()``, which a chunked body can truncate silently.
- ``Problem`` (``fhir_proxy.errors``): raise it for a refusal; it becomes an OperationOutcome
  with the given status (422 before OpenEMR is called, 502 if OpenEMR is unreachable).
- ``trace_step`` (below): one step of the share trace in the dashboard's ``ShareStep`` shape,
  with secrets replaced.
- ``log`` (``fhir_proxy.logs.log``): structured logs; never a token, key, assertion or value.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from fhir_proxy.config import ServiceConfig
from fhir_proxy.routes import Route
from fhir_proxy.store import Stores

EXTENSION_MODULE = "fhir_proxy.share"
# Keys whose values never reach a trace, at any depth, compared without regard to case: OAuth 2.0
# (RFC 6749) and JWT bearer assertion (RFC 7523) parameter names, HTTP credentials, passwords.
SECRET_KEYS = frozenset(
    {
        "authorization",
        "client_secret",
        "client_assertion",
        "assertion",
        "password",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "code",
        "code_verifier",
        "private_key",
    }
)
NOT_SHOWN = "[not shown]"
_PATH = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*", re.ASCII)


def redact(value: object) -> object:
    """A copy of a JSON value with every secret key's value replaced by ``[not shown]``."""
    if isinstance(value, dict):
        return {
            k: (NOT_SHOWN if isinstance(k, str) and k.lower() in SECRET_KEYS else redact(v))
            for k, v in value.items()
        }
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
    """One ``ShareStep`` (web/src/api/contract.ts). ``path`` is a path only: no scheme or host
    (OpenEMR's host is not the page's business), and no query string, which could carry a
    credential such as an authorization code."""
    if not _PATH.fullmatch(path) or path.startswith("//"):
        raise ValueError("a trace step's url is a path with no scheme, host or query")
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
    core_routes: tuple[Route, ...] = ()
    routes: list[tuple[Route, Callable]] = field(default_factory=list)
    openemr_probe: Callable[[], dict] | None = None

    def _taken(self) -> Iterable[Route]:
        yield from self.core_routes
        yield from (route for route, _ in self.routes)

    def add_route(self, route: Route, handler: Callable) -> None:
        if route.public:
            raise ValueError("an extension cannot add a route without the session-token check")
        for other in self._taken():
            if other.endpoint == route.endpoint:
                raise ValueError("an extension cannot reuse an endpoint name")
            if (other.method, other.path) == (route.method, route.path):
                raise ValueError("an extension cannot register a method and path that exist")
        self.routes.append((route, handler))

    def set_openemr_probe(self, probe: Callable[[], dict]) -> None:
        self.openemr_probe = probe


def load_extension(context: ProxyContext, module: str = EXTENSION_MODULE) -> bool:
    """Import and register the share extension if it is installed. True if it was."""
    if importlib.util.find_spec(module) is None:
        return False
    importlib.import_module(module).register(context)
    return True
