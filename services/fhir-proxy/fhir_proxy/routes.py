"""The route table: every route the proxy serves, in one list.

The Flask app registers exactly these routes, and the CapabilityStatement is generated from the
same list, so the two cannot drift (a test compares the CapabilityStatement with the app's URL
map as well). A route is one of:

- ``fhir``: a FHIR REST interaction (``search-type`` on Observation, ``capabilities``), listed in
  the CapabilityStatement's ``rest.resource`` / ``rest.interaction``;
- ``view``: a JSON view the proxy assembles for the dashboard (not FHIR), listed in
  ``rest.documentation`` so the statement still says what else the server answers;
- ``auth``: the demo-session routes (D16): ``GET /auth/config``, ``POST /auth/session`` and
  ``POST /auth/logout``, the only routes that need no ``__session`` cookie. (Logout needs none
  so that it can always clear the cookie, an expired one included; like every ``POST`` it
  still needs an allowed ``Origin``.)

T15's share route is added through :mod:`fhir_proxy.extensions`, into this same list.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    method: str
    path: str  # Flask rule without the PATH_PREFIX, e.g. "/trace/heart-rate/<observation_id>"
    endpoint: str
    kind: str  # "fhir", "view" or "auth"
    summary: str
    interaction: str | None = None  # FHIR: "search-type" or "capabilities"
    resource_type: str | None = None

    @property
    def public(self) -> bool:
        return self.kind == "auth"


CORE_ROUTES: tuple[Route, ...] = (
    Route(
        "GET",
        "/auth/config",
        "auth_config",
        "auth",
        "The bot check's public site key and action, for the page",
    ),
    Route(
        "POST",
        "/auth/session",
        "auth_session",
        "auth",
        "Run the bot check (one Fraud Defense assessment); on a pass, set the __session cookie",
    ),
    Route("POST", "/auth/logout", "auth_logout", "auth", "Clear the __session cookie"),
    Route("GET", "/status", "status", "view", "Status strip: watch sync, counts, reachability"),
    Route(
        "GET",
        "/fhir/metadata",
        "metadata",
        "fhir",
        "This CapabilityStatement",
        interaction="capabilities",
    ),
    Route(
        "GET",
        "/fhir/Observation",
        "search_observations",
        "fhir",
        "Observation search, pinned to the configured patient",
        interaction="search-type",
        resource_type="Observation",
    ),
    Route(
        "GET",
        "/fhir/Observation/<observation_id>",
        "read_observation",
        "fhir",
        "Observation read by id, pinned to the configured patient (what entry.fullUrl names)",
        interaction="read",
        resource_type="Observation",
    ),
    Route(
        "GET",
        "/trace/heart-rate/<observation_id>",
        "heart_rate_trace",
        "view",
        "Heart-rate trace: Observation, Provenance, Device and the reconstructed roll-up window",
    ),
    Route(
        "GET",
        "/trace/cuff-attempts/<attempt_id>",
        "cuff_attempt_trace",
        "view",
        "Cuff attempt trace: the attempt, the HL7v2 message as sent, AuditEvent, Observation, "
        "Provenance",
    ),
    Route("GET", "/messages", "messages", "view", "Messages log, newest first"),
    Route("GET", "/cuff/samples", "cuff_samples", "view", "cuff-sim's malformed samples"),
    Route(
        "POST", "/cuff/readings", "cuff_readings", "view", "One cuff send, forwarded to cuff-sim"
    ),
)
