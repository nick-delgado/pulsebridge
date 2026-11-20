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
  check (the ``__session`` cookie, and ``Origin`` on a ``POST``), gets the security headers, the
  request budget and the error handling, and appears in the CapabilityStatement's route list.
  The public routes are fixed by the core table (the three ``/auth/`` session routes only): an
  extension cannot add a public route, reuse a core endpoint name, register a method
  and path that already exist, or register a pattern that could match a core URL (for example
  ``/fhir/Observation/<int:n>``, which would win over the patient-pinned read route). Use
  distinct literal paths. ``create_app`` checks again after registering that every core URL
  still reaches its core endpoint, and refuses to start otherwise.
- ``stores``: read access to the health-data store with the patient pin
  (``stores.patient_references()``, ``stores.read(...)``, ``stores.is_ours(...)``).
- ``config``: the proxy's validated configuration (T15 adds its own variables in its package).
- ``read_body(limit)`` (``fhir_proxy.bodies``): the request body, read completely or refused
  with 413; never ``request.get_data()``, which a chunked body can truncate silently.
- ``Problem`` (``fhir_proxy.errors``): raise it for a refusal; it becomes an OperationOutcome
  with the given status (422 before OpenEMR is called, 502 if OpenEMR is unreachable).
- ``trace_step`` (below): one step of the share trace in the dashboard's ``ShareStep`` shape,
  with credentials replaced (the token step's OAuth parameters; elsewhere only names that are
  never FHIR elements) and a search's parameters in ``request.query``.
- ``clock``, ``monotonic``: the app's clocks; ``options``: test doubles from ``create_app``.
- ``log`` (``fhir_proxy.logs.log``): structured logs; never a token, key, assertion or value.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from werkzeug.exceptions import MethodNotAllowed, NotFound
from werkzeug.routing import Map, Rule

from fhir_proxy.config import ServiceConfig
from fhir_proxy.routes import CORE_ROUTES, Route
from fhir_proxy.store import Stores

EXTENSION_MODULE = "fhir_proxy.share"

# Trace redaction, the FINAL rule agreed with the dashboard (web/README.md, "For T15: the share
# route"; web/src/api/contract.ts SHARE_TOKEN_STEP_SECRETS and SHARE_FHIR_STEP_SECRETS, which
# tests/share/test_redaction.py reads and compares with these two lists). Values of these keys
# are replaced by "[not shown]" at any depth, key names compared without regard to case.
#
# - In the `token` step's bodies: every OAuth 2.0 (RFC 6749) and JWT bearer assertion (RFC 7523)
#   parameter that carries a credential, including the generic `code` and `token`.
TOKEN_STEP_SECRET_KEYS = frozenset(
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
        "registration_access_token",
    }
)
# - In every other step (FHIR bodies): only names that can never be a FHIR R4 element, so
#   `Observation.code`, `Coding.code`, `Quantity.code` and `OperationOutcome.issue.code` stay
#   visible. (`code` and `token` ARE FHIR element names; hiding them blanked every LOINC code.)
FHIR_STEP_SECRET_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "client_assertion",
        "client_secret",
        "assertion",
        "private_key",
        "password",
        "authorization",
        "registration_access_token",
    }
)
# Search parameter names a trace step's `request.query` may never carry: a credential in a URL
# ends up in logs. (`code` is allowed: it is an ordinary FHIR search parameter.)
FORBIDDEN_QUERY_NAMES = FHIR_STEP_SECRET_KEYS | {"token", "code_verifier"}
NOT_SHOWN = "[not shown]"
_PATH = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*", re.ASCII)
_QUERY_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}", re.ASCII)
MAX_QUERY_VALUE_CHARS = 256


def secret_keys_for(step: str) -> frozenset[str]:
    """The redaction list for a step: the OAuth list for ``token``, the FHIR-safe list else."""
    return TOKEN_STEP_SECRET_KEYS if step == "token" else FHIR_STEP_SECRET_KEYS


def redact(value: object, secrets: frozenset[str] = TOKEN_STEP_SECRET_KEYS) -> object:
    """A copy of a JSON value with every listed key's value replaced by ``[not shown]``."""
    if isinstance(value, dict):
        return {
            k: (NOT_SHOWN if isinstance(k, str) and k.lower() in secrets else redact(v, secrets))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    return value


def _checked_query(query: dict | None) -> dict | None:
    """``request.query``: parameter name -> one value or a list of values, or None. Names are
    plain search parameter names, never a credential name; values are short strings."""
    if query is None:
        return None
    if not isinstance(query, dict) or not query:
        raise ValueError("a trace step's query is a non-empty object or None")
    out: dict[str, str | list[str]] = {}
    for name, value in query.items():
        if not isinstance(name, str) or not _QUERY_NAME.fullmatch(name):
            raise ValueError("a trace step's query has plain parameter names")
        if name.lower() in FORBIDDEN_QUERY_NAMES:
            raise ValueError("a trace step's query never carries a credential parameter")
        values = value if isinstance(value, list) else [value]
        if not values or not all(
            isinstance(v, str) and len(v) <= MAX_QUERY_VALUE_CHARS and v.isprintable()
            for v in values
        ):
            raise ValueError("a trace step's query values are short printable strings")
        out[name] = list(values) if isinstance(value, list) else value
    return out


def trace_step(
    step: str,
    label: str,
    *,
    method: str,
    path: str,
    query: dict | None = None,
    request_body: object,
    status: int,
    response_body: object,
    result: str,
    duration_ms: int,
) -> dict:
    """One ``ShareStep`` (web/src/api/contract.ts). ``path`` is a path only: no scheme or host
    (OpenEMR's host is not the page's business), and no query string, which could carry a
    credential such as an authorization code; a search's parameters go in ``query`` instead.
    No request or response header is ever part of a step (the Authorization header carries the
    access token)."""
    if not _PATH.fullmatch(path) or path.startswith("//"):
        raise ValueError("a trace step's url is a path with no scheme, host or query")
    secrets = secret_keys_for(step)
    return {
        "step": step,
        "label": label,
        "request": {
            "method": method,
            "url": path,
            "query": _checked_query(query),
            "body": redact(request_body, secrets),
        },
        "response": {"status": status, "body": redact(response_body, secrets)},
        "result": result,
        "durationMs": duration_ms,
    }


def sample_urls(path: str) -> list[str]:
    """Concrete URLs a rule's pattern stands for, to test for overlaps: every ``<...>`` filled
    with a word and with a number (so an ``<int:...>`` or ``<path:...>`` rule cannot slip by)."""
    return [re.sub(r"<[^>]+>", value, path) for value in ("sample-id", "1", "a.b")]


def shadows_core(route: Route) -> bool:
    """Whether ``route``'s pattern could match the URL of a core route (with its method)."""
    adapter = Map([Rule(route.path, endpoint="extension", methods=[route.method])]).bind("")
    for core in CORE_ROUTES:
        for url in sample_urls(core.path):
            try:
                adapter.match(url, method=core.method)
            except (NotFound, MethodNotAllowed):
                continue
            return True
    return False


@dataclass
class ProxyContext:
    config: ServiceConfig
    stores: Stores
    core_routes: tuple[Route, ...] = ()
    routes: list[tuple[Route, Callable]] = field(default_factory=list)
    openemr_probe: Callable[[], dict] | None = None
    # The app's injected clocks (tests pass fakes): wall time for checkedAt and JWT claims,
    # monotonic seconds for caches.
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    monotonic: Callable[[], float] = time.monotonic
    # What create_app(extension_options=...) passed: test doubles for the extension (the share's
    # OpenEMR transport). Empty in production.
    options: Mapping[str, object] = field(default_factory=dict)

    def _taken(self) -> Iterable[Route]:
        # The module-level core table, never a field an extension could change.
        yield from CORE_ROUTES
        yield from (route for route, _ in self.routes)

    def add_route(self, route: Route, handler: Callable) -> None:
        if route.public:
            raise ValueError("an extension cannot add a route without the session-token check")
        for other in self._taken():
            if other.endpoint == route.endpoint:
                raise ValueError("an extension cannot reuse an endpoint name")
            if (other.method, other.path) == (route.method, route.path):
                raise ValueError("an extension cannot register a method and path that exist")
        if shadows_core(route):
            raise ValueError("an extension rule cannot match the URL of a core route")
        self.routes.append((route, handler))

    def set_openemr_probe(self, probe: Callable[[], dict]) -> None:
        self.openemr_probe = probe


def load_extension(context: ProxyContext, module: str = EXTENSION_MODULE) -> bool:
    """Import and register the share extension if it is installed. True if it was."""
    if importlib.util.find_spec(module) is None:
        return False
    importlib.import_module(module).register(context)
    return True
