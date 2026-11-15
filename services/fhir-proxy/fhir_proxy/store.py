"""Reads from the three Cloud Healthcare stores, through ``pulsebridge_gcp``'s client.

The proxy is **read-only** (D12): its service account has the FHIR reader role on both FHIR
stores and the HL7v2 consumer role on the HL7v2 store (``infra/modules/healthcare``), and no
method here writes. Every store id comes from configuration; no caller value names a store, a
resource type or a path. Ids that come from a caller are checked by the client against the FHIR
id pattern before a request is built.

**The patient pin.** Health data is read only for the configured patient. Its server id is
found by identifier (``Patient?identifier=urn:pulsebridge:patient|<PATIENT_ID>``, the setup
step's record, D11) and kept for a few minutes. More than one match (T06's duplicate case) pins
to all of them: they carry the same identifier, so they are the same person.

Every call is bounded by the client's timeout (``FHIR_TIMEOUT_SECONDS``); there are no retries.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Sequence

from pulsebridge_fhir import codes
from pulsebridge_fhir.keys import escape_search_value
from pulsebridge_gcp import HealthcareClient, Hl7v2Message, MessagePage

# R4 id: 1-64 of [A-Za-z0-9-.]. A value made only of dots ("." or "..") also fits that pattern
# but is a path segment that climbs a level once it is in a URL, so at least one letter or
# digit is required (pulsebridge_gcp's own check allows it; reported to the orchestrator).
SAFE_ID = re.compile(r"(?=[A-Za-z0-9.-]*[A-Za-z0-9])[A-Za-z0-9.-]{1,64}", re.ASCII)

PATIENT_CACHE_SECONDS = 300.0
# No Patient yet (setup not run): looked up again sooner, so the first data shows quickly.
NO_PATIENT_CACHE_SECONDS = 30.0


def token(system: str, value: str) -> str:
    return f"{escape_search_value(system)}|{escape_search_value(value)}"


def entries(bundle: object, resource_type: str | None = None) -> list[dict]:
    """The resources of a Bundle, optionally of one type; anything malformed is skipped."""
    found = []
    if not isinstance(bundle, dict):
        return found
    for entry in bundle.get("entry") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("resource"), dict):
            continue
        resource = entry["resource"]
        if resource_type is None or resource.get("resourceType") == resource_type:
            found.append(resource)
    return found


def next_link(bundle: object) -> str | None:
    if not isinstance(bundle, dict):
        return None
    for link in bundle.get("link") or []:
        if isinstance(link, dict) and link.get("relation") == "next":
            url = link.get("url")
            return url if isinstance(url, str) and url else None
    return None


class Stores:
    def __init__(
        self,
        client: HealthcareClient,
        *,
        fhir_store: str,
        audit_store: str,
        hl7v2_store: str,
        patient_id: str,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.fhir_store = fhir_store
        self.audit_store = audit_store
        self.hl7v2_store = hl7v2_store
        self.patient_id = patient_id
        self.hl7v2_store_name = client.hl7v2_store_name(hl7v2_store)
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._patients: tuple[float, tuple[str, ...]] | None = None

    # ---- the patient pin ---------------------------------------------------------------------

    def patient_references(self) -> tuple[str, ...]:
        """``("Patient/<id>", ...)`` for the configured patient; empty if setup has not run."""
        with self._lock:
            cached = self._patients
        now = self._monotonic()
        if cached is not None:
            at, refs = cached
            ttl = PATIENT_CACHE_SECONDS if refs else NO_PATIENT_CACHE_SECONDS
            if now - at < ttl:
                return refs
        bundle = self.client.search(
            self.fhir_store,
            "Patient",
            [("identifier", token(codes.PB_PATIENT_ID, self.patient_id)), ("_elements", "id")],
        )
        refs = tuple(
            sorted(
                f"Patient/{r['id']}"
                for r in entries(bundle, "Patient")
                if isinstance(r.get("id"), str)
            )
        )
        with self._lock:
            self._patients = (now, refs)
        return refs

    def is_ours(self, resource: dict, refs: Sequence[str]) -> bool:
        """Whether a resource's ``subject`` is the configured patient."""
        subject = resource.get("subject")
        return isinstance(subject, dict) and subject.get("reference") in refs

    # ---- FHIR store --------------------------------------------------------------------------

    def search_observations(self, params: list[tuple[str, str]]) -> dict:
        return self.client.search(self.fhir_store, "Observation", params)

    def read(self, resource_type: str, resource_id: str) -> dict:
        if not SAFE_ID.fullmatch(resource_id):
            raise ValueError("not a FHIR id")
        return self.client.read_resource(self.fhir_store, resource_type, resource_id)

    def observations_by_identifier(self, values: Sequence[str]) -> dict[str, dict]:
        """Observations by our identifier (``urn:pulsebridge:observation|<value>``), keyed by
        value; one search for up to 50 values (FHIR token OR with ``,``)."""
        found: dict[str, dict] = {}
        values = sorted(set(values))
        for start in range(0, len(values), 50):
            chunk = values[start : start + 50]
            joined = ",".join(token(codes.PB_OBSERVATION_ID, v) for v in chunk)
            bundle = self.client.search(
                self.fhir_store, "Observation", [("identifier", joined), ("_count", "100")]
            )
            for resource in entries(bundle, "Observation"):
                for identifier in resource.get("identifier") or []:
                    if (
                        isinstance(identifier, dict)
                        and identifier.get("system") == codes.PB_OBSERVATION_ID
                        and identifier.get("value") in chunk
                    ):
                        found[identifier["value"]] = resource
        return found

    def count_observations(self, code: str, refs: Sequence[str]) -> int:
        bundle = self.client.search(
            self.fhir_store,
            "Observation",
            [
                ("subject", ",".join(refs)),
                ("code", token(codes.LOINC, code)),
                ("_summary", "count"),
            ],
        )
        total = bundle.get("total") if isinstance(bundle, dict) else None
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise ValueError("count search without a total")
        return total

    def newest_write(self, code: str, refs: Sequence[str]) -> str | None:
        """``meta.lastUpdated`` of the most recently written Observation with this code.

        ``_sort=-_lastUpdated`` is Cloud Healthcare's default order (its search documentation
        says so), so this is one cheap search for one resource."""
        bundle = self.client.search(
            self.fhir_store,
            "Observation",
            [
                ("subject", ",".join(refs)),
                ("code", token(codes.LOINC, code)),
                ("_sort", "-_lastUpdated"),
                ("_count", "1"),
                ("_elements", "id,meta"),
            ],
        )
        for resource in entries(bundle, "Observation"):
            meta = resource.get("meta")
            if isinstance(meta, dict) and isinstance(meta.get("lastUpdated"), str):
                return meta["lastUpdated"]
        return None

    def provenances(self, params: list[tuple[str, str]]) -> dict:
        return self.client.search(self.fhir_store, "Provenance", params)

    # ---- audit store (D15) -------------------------------------------------------------------

    def audit_events(self, params: list[tuple[str, str]]) -> dict:
        return self.client.search(self.audit_store, "AuditEvent", params)

    def next_audit_page(self, bundle: dict) -> dict | None:
        """The next page of an audit-store search; the link must stay inside that store."""
        return self.client.next_page(self.audit_store, bundle)

    def read_audit_event(self, resource_id: str) -> dict:
        if not SAFE_ID.fullmatch(resource_id):
            raise ValueError("not a FHIR id")
        return self.client.read_resource(self.audit_store, "AuditEvent", resource_id)

    # ---- HL7v2 store -------------------------------------------------------------------------

    def is_our_message(self, name: object) -> bool:
        """A well-formed message name inside the configured HL7v2 store. Nothing else is ever
        fetched, whatever an AuditEvent or a caller says."""
        return isinstance(name, str) and self.client.is_message_in_store(name, self.hl7v2_store)

    def get_message(self, name: str) -> Hl7v2Message:
        if not self.is_our_message(name):
            raise ValueError("not a message of the configured HL7v2 store")
        return self.client.get_message(name, view="RAW_ONLY")

    def list_messages(self, *, filter: str | None, page_token: str | None) -> MessagePage:  # noqa: A002
        return self.client.list_messages(
            self.hl7v2_store,
            filter=filter,
            order_by="send_time desc",
            page_size=100,
            page_token=page_token,
            view="RAW_ONLY",
        )
