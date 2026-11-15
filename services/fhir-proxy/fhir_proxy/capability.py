"""The proxy's FHIR R4 ``CapabilityStatement`` (``GET /fhir/metadata``), built from the route
table and the search validator, so it lists exactly what is implemented (brief: "The proxy
publishes a CapabilityStatement listing only what it supports").

What it says, and why:

- ``kind: instance`` (R4: "describes an actual running instance of software"), ``fhirVersion:
  4.0.1``, ``format: json`` only (the proxy never answers XML).
- One ``rest`` entry in ``server`` mode with one resource, ``Observation``, one interaction,
  ``search-type``, and the search parameters :mod:`fhir_proxy.search` accepts. No ``read``,
  ``create`` or any write: the proxy is read-only, and the dashboard needs nothing else.
- **Security, stated truthfully.** R4 ``rest.security.service`` is bound (extensibly) to
  ``restful-security-service``, whose codes are OAuth, SMART-on-FHIR, NTLM, Basic, Kerberos and
  Certificates. None describes this login, so no code from that set is claimed (in particular
  not SMART-on-FHIR): ``service`` carries only text, and ``description`` says what it is: a
  shared demo password exchanged for an 8-hour bearer token (D9), with no per-user identity,
  no scopes and no launch context. ``cors: true`` because the dashboard calls it cross-origin.
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
    "_sort": "date or -date.",
    "_count": "1 to 1000; follow link next for further pages.",
}
SEARCH_PARAMETER_DEFINITIONS = {
    "code": "http://hl7.org/fhir/SearchParameter/clinical-code",
    "date": "http://hl7.org/fhir/SearchParameter/clinical-date",
}


def capability_statement(
    routes: Iterable[Route], *, prefix: str, ttl_hours: float, software_version: str
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
            {"type": route.resource_type, "interaction": [], "searchParam": []},
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
        + (" (no session token needed)" if r.public else "")
        for r in others
    )
    hours = f"{ttl_hours:g}"
    return {
        "resourceType": "CapabilityStatement",
        "status": "active",
        "date": "2026-11-15",
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
                    "cors": True,
                    "service": [
                        {
                            "text": "Shared-password bearer token (PulseBridge reviewer login); "
                            "not SMART on FHIR, not OAuth 2.0"
                        }
                    ],
                    "description": "Every request except POST "
                    f"{prefix}/auth/login needs Authorization: Bearer <token>. The token comes "
                    "from POST /auth/login with the one shared demo password, is an HS256 JWT "
                    f"valid for {hours} hours, and carries no user identity, no scopes and no "
                    "launch context. Changing the password ends every session. Every search is "
                    "limited to the one configured patient.",
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
