"""The proxy's FHIR R4 ``CapabilityStatement`` (``GET /fhir/metadata``), built from the route
table and the search validator, so it lists exactly what is implemented (brief: "The proxy
publishes a CapabilityStatement listing only what it supports").

What it says, and why:

- ``kind: instance`` (R4: "describes an actual running instance of software"), ``fhirVersion:
  4.0.1``, ``format: json`` only (the proxy never answers XML).
- One ``rest`` entry in ``server`` mode with one resource, ``Observation``, two interactions,
  ``search-type`` and ``read`` (both pinned to the configured patient; ``read`` is what each
  search result's ``fullUrl`` names), and the search criteria :mod:`fhir_proxy.search` accepts
  (``code``, ``date``). ``_sort`` and ``_count`` are result parameters, not criteria, so they are
  described in the resource's ``documentation`` rather than as ``searchParam``. No ``create`` or
  any write: the proxy is read-only.
- ``date`` is required by R4 ("the date (and optionally time) when the capability statement was
  published or last changed"). It is the date this instance started: the statement describes a
  running instance (``kind: instance``) and changes only when a new revision starts.
- **Security, stated truthfully.** R4 ``rest.security.service`` is bound (extensibly) to
  ``restful-security-service``, whose codes are OAuth, SMART-on-FHIR, NTLM, Basic, Kerberos and
  Certificates. None describes this access, so no code from that set is claimed (in particular
  not SMART-on-FHIR): ``service`` carries only text, and ``description`` says what it is: an
  anonymous demo session (D16), granted after a bot check and carried in a cookie, with no
  user identity, no scopes and no launch context. ``cors: false`` (R4: "Server adds CORS
  headers when responding to requests"): the page and the proxy share one origin, and the
  proxy sends no CORS headers.
- The dashboard's other routes are not FHIR interactions; ``rest.documentation`` lists them so
  the statement does not hide what else the server answers.
"""

from __future__ import annotations

from collections.abc import Iterable

from fhir_proxy.routes import Route
from fhir_proxy.search import SEARCH_PARAMETERS

SEARCH_PARAMETER_DOCS = {
    "code": "Exactly http://loinc.org|8867-4 (heart rate) or http://loinc.org|85354-9 "
    "(blood-pressure panel). One value; no modifiers.",
    "date": "effective[x]: at most one lower bound (gt, ge) and one upper bound (lt, le), each "
    "a full instant with a time zone. On a Period, gt matches a period that ends after the "
    "value and lt one that starts before it (R4 prefix rules), so gt<start>&lt<end> returns "
    "the windows overlapping (start, end).",
}
RESULT_PARAMETER_DOCS = (
    "Result parameters: _sort (date or -date; default date) and _count (1 to 1000; default "
    "100); follow link next for further pages. On a boundary instant, ge and le include a "
    "period that ends or starts exactly there; gt and lt do not."
)
SEARCH_PARAMETER_DEFINITIONS = {
    "code": "http://hl7.org/fhir/SearchParameter/clinical-code",
    "date": "http://hl7.org/fhir/SearchParameter/clinical-date",
}


def capability_statement(
    routes: Iterable[Route], *, prefix: str, ttl_hours: float, software_version: str, date: str
) -> dict:
    routes = list(routes)
    fhir = [r for r in routes if r.kind == "fhir"]
    others = [r for r in routes if r.kind != "fhir"]
    resources: dict[str, dict] = {}
    for route in fhir:
        if route.resource_type is None:
            continue
        entry = resources.setdefault(
            route.resource_type,
            {
                "type": route.resource_type,
                "documentation": RESULT_PARAMETER_DOCS,
                "interaction": [],
                "searchParam": [],
            },
        )
        entry["interaction"].append({"code": route.interaction})
        if route.interaction == "search-type":
            for name, kind in SEARCH_PARAMETERS.items():
                param = {"name": name, "type": kind, "documentation": SEARCH_PARAMETER_DOCS[name]}
                if name in SEARCH_PARAMETER_DEFINITIONS:
                    param["definition"] = SEARCH_PARAMETER_DEFINITIONS[name]
                entry["searchParam"].append(param)
    listing = "\n".join(
        f"- `{r.method} {prefix}{r.path}`: {r.summary}"
        + (" (no session needed)" if r.public else "")
        for r in others
    )
    hours = f"{ttl_hours:g}"
    return {
        "resourceType": "CapabilityStatement",
        "status": "active",
        "date": date,
        "kind": "instance",
        "software": {"name": "PulseBridge fhir-proxy", "version": software_version},
        "implementation": {
            "description": "PulseBridge fhir-proxy: a narrow, read-only FHIR R4 front for a "
            "personal health record (one patient)."
        },
        "fhirVersion": "4.0.1",
        "format": ["json"],
        "rest": [
            {
                "mode": "server",
                "documentation": "Besides the FHIR interactions below, this server answers these "
                "JSON routes for the PulseBridge dashboard (not FHIR interactions):\n" + listing,
                "security": {
                    "cors": False,
                    "service": [
                        {
                            "text": "Anonymous demo session after a bot check (PulseBridge, "
                            "Google Cloud Fraud Defense); not SMART on FHIR, not OAuth 2.0"
                        }
                    ],
                    "description": f"Every request except those under {prefix}/auth/ needs the "
                    f"__session cookie, which POST {prefix}/auth/session sets after an "
                    "invisible bot check (a reCAPTCHA score). The cookie holds an HS256 JWT "
                    f"valid for {hours} hours that carries no user identity, no scopes and no "
                    "launch context; Authorization headers are not accepted. Every POST must "
                    "come from the dashboard's own origin (Origin header). Every search is "
                    "limited to the one configured patient. Requests are rate limited per "
                    "client address and per session (T46): a refusal is HTTP 429 with "
                    "Retry-After in seconds and an OperationOutcome (issue code throttled, "
                    "reason rate-limited).",
                },
                "resource": list(resources.values()),
            }
        ],
    }


def implemented_interactions(statement: dict) -> set[tuple[str, str]]:
    """``(resource type, interaction)`` pairs the statement claims (for the test)."""
    pairs = set()
    for rest in statement.get("rest", []):
        for resource in rest.get("resource", []):
            for interaction in resource.get("interaction", []):
                pairs.add((resource["type"], interaction["code"]))
    return pairs
