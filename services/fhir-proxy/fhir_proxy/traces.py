"""The two trace lookups behind the dashboard's trace panels.

``GET /trace/heart-rate/{observationId}`` (right panel): the Observation as stored, its
Provenance and Device, and the Google Health roll-up window it came from. No raw Google Health
response is stored anywhere (health-sync README, "For T14"), so the window is **rebuilt** from
the stored Observation exactly as health-sync's table says, and sent as
``{"kind": "reconstructed", "window": ...}``:

| Roll-up field | From the Observation |
| --- | --- |
| ``startTime``, ``endTime`` | ``effectivePeriod.start`` / ``.end``, UTC with ``Z``, seconds |
| ``heartRate.beatsPerMinuteAvg`` | ``valueQuantity.value`` |
| ``heartRate.beatsPerMinuteMin`` | ``component[code=96935-2].valueQuantity.value`` |
| ``heartRate.beatsPerMinuteMax`` | ``component[code=55426-1].valueQuantity.value`` |

``GET /trace/cuff-attempts/{attemptId}`` (left panel): the attempt as a messages-log row, the
HL7v2 message as sent, and the AuditEvent, Observation and Provenance as stored. Where the
message text comes from (open question 3):

- an accepted send: the **stored** message (``view=RAW_ONLY``), found by the store-returned name
  (HTTPS) or by the derived name once a ``get`` confirmed it (MLLP);
- a "send malformed" sample that was not stored: T07's sample bytes, with the PID rebuilt for the
  configured patient by T07's ``with_patient`` exactly as cuff-sim does (when the proxy has the
  same patient variables), or unchanged for the samples ``with_patient`` leaves alone;
- a built reading that was rejected or unacknowledged: nothing (``null``). T31 keeps no message
  bytes, on purpose, and the store did not keep it.

Both lookups are pinned to the configured patient: a resource about anyone else is "not found".
"""

from __future__ import annotations

from datetime import UTC

from pulsebridge_fhir import (
    NotASendAttempt,
    UnreadableSendAttempt,
    codes,
    parse_send_attempt,
)
from pulsebridge_gcp import NotFoundError
from pulsebridge_hl7v2 import Patient
from pulsebridge_hl7v2.samples import load as load_sample

from fhir_proxy import hl7
from fhir_proxy.config import SamplePatient
from fhir_proxy.errors import Problem
from fhir_proxy.messages import MessagesLog, application_from_labels, attempt_row
from fhir_proxy.search import HEART_RATE, LOINC, parse_instant
from fhir_proxy.store import Stores


def not_found(what: str) -> Problem:
    return Problem(404, "not-found", "not-found", f"{what} was not found.")


def collection(resources: list[dict | None]) -> dict:
    """A FHIR ``collection`` Bundle of the resources that exist, in order."""
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"resource": r} for r in resources if r is not None],
    }


def _has_code(concept: object, system: str, code: str) -> bool:
    codings = concept.get("coding") if isinstance(concept, dict) else None
    return isinstance(codings, list) and any(
        isinstance(c, dict) and c.get("system") == system and c.get("code") == code for c in codings
    )


def _number(quantity: object) -> int | float | None:
    if isinstance(quantity, dict):
        value = quantity.get("value")
        if isinstance(value, int | float) and not isinstance(value, bool):
            return value
    return None


def _utc_seconds(value: object) -> str | None:
    parsed = parse_instant(value) if isinstance(value, str) else None
    if parsed is None:
        return None
    return parsed.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def reconstruct_window(observation: dict) -> dict | None:
    """The Google Health roll-up window, rebuilt from a stored heart-rate Observation; None
    if any part is missing (then the trace sends ``source: null``, never a partial window)."""
    period = observation.get("effectivePeriod")
    if not isinstance(period, dict):
        return None
    start, end = _utc_seconds(period.get("start")), _utc_seconds(period.get("end"))
    average = _number(observation.get("valueQuantity"))
    minimum = maximum = None
    for component in observation.get("component") or []:
        if not isinstance(component, dict):
            continue
        if _has_code(component.get("code"), LOINC, codes.HEART_RATE_MIN["code"]):
            minimum = _number(component.get("valueQuantity"))
        elif _has_code(component.get("code"), LOINC, codes.HEART_RATE_MAX["code"]):
            maximum = _number(component.get("valueQuantity"))
    if None in (start, end, average, minimum, maximum):
        return None
    return {
        "startTime": start,
        "endTime": end,
        "heartRate": {
            "beatsPerMinuteMin": minimum,
            "beatsPerMinuteAvg": average,
            "beatsPerMinuteMax": maximum,
        },
    }


def _identifier_value(resource: dict) -> str | None:
    for identifier in resource.get("identifier") or []:
        if isinstance(identifier, dict) and identifier.get("system") == codes.PB_OBSERVATION_ID:
            value = identifier.get("value")
            return value if isinstance(value, str) else None
    return None


def _reference_id(reference: object, resource_type: str) -> str | None:
    if not isinstance(reference, dict) or not isinstance(reference.get("reference"), str):
        return None
    kind, _, rid = reference["reference"].partition("/")
    return rid if kind == resource_type and rid and "/" not in rid else None


def _read_or_none(stores: Stores, resource_type: str, resource_id: str | None) -> dict | None:
    if resource_id is None:
        return None
    try:
        return stores.read(resource_type, resource_id)
    except (NotFoundError, ValueError):
        return None


def _provenance_for(stores: Stores, observation: dict) -> dict | None:
    """T06: the Provenance's id is derived from the Observation's key; check it points back."""
    key = _identifier_value(observation)
    provenance = _read_or_none(stores, "Provenance", hl7.provenance_id(key) if key else None)
    if provenance is None:
        return None
    target = f"Observation/{observation.get('id')}"
    targets = [t.get("reference") for t in provenance.get("target") or [] if isinstance(t, dict)]
    return provenance if target in targets else None


def heart_rate_trace(stores: Stores, observation_id: str) -> dict:
    """The wire shape ``{"resources": Bundle, "source": {"kind", "window"} | null}``."""
    refs = stores.patient_references()
    try:
        observation = stores.read("Observation", observation_id)
    except NotFoundError:
        raise not_found("The heart-rate Observation") from None
    if not stores.is_ours(observation, refs) or not _has_code(
        observation.get("code"), LOINC, HEART_RATE
    ):
        # Another patient's, or not a heart rate: the same answer as absent, so the route
        # cannot be used to probe for other resources.
        raise not_found("The heart-rate Observation")
    device = _read_or_none(stores, "Device", _reference_id(observation.get("device"), "Device"))
    provenance = _provenance_for(stores, observation)
    window = reconstruct_window(observation)
    return {
        "resources": collection([observation, provenance, device]),
        "source": None if window is None else {"kind": "reconstructed", "window": window},
    }


def sample_text(name: str | None, patient_id: str, patient: SamplePatient | None) -> str | None:
    """A "send malformed" sample's bytes as cuff-sim sent them, when the proxy can know them."""
    if name is None:
        return None
    try:
        sample = load_sample(name)
    except (KeyError, ValueError):
        return None
    if sample.accepts_patient:
        if patient is None:
            return None  # cuff-sim rebuilt its PID; without the same values we cannot
        try:
            sample = sample.with_patient(
                Patient(
                    identifier=patient_id,
                    assigning_authority=patient.assigning_authority,
                    family_name=patient.family_name,
                    given_name=patient.given_name,
                )
            )
        except (TypeError, ValueError):
            return None
    return sample.payload.decode("utf-8", errors="replace")


def cuff_attempt_trace(
    stores: Stores,
    log: MessagesLog,
    attempt_id: str,
    *,
    patient_id: str,
    sample_patient: SamplePatient | None,
) -> dict:
    """The wire shape ``{"attempt", "messageSent", "resources": Bundle}``."""
    try:
        resource = stores.read_audit_event(attempt_id)
    except NotFoundError:
        raise not_found("The send attempt") from None
    try:
        record = parse_send_attempt(resource)
    except NotASendAttempt:
        raise not_found("The send attempt") from None
    except UnreadableSendAttempt:
        raise Problem(
            422,
            "processing",
            "unreadable-send-attempt",
            "This send attempt's AuditEvent could not be read; it is listed in the messages log "
            "as unreadable.",
        ) from None
    if record.patient_id != patient_id or record.destination != stores.hl7v2_store_name:
        raise not_found("The send attempt")

    name = record.message_name or record.derived_message_name
    lookup = None
    if record.result == "accepted" and name:
        lookup = log.lookups([name])[name]
    message = lookup.message if lookup is not None else None

    observation = provenance = None
    if message is not None and application_from_labels(message.labels)["outcome"] == "accepted":
        key = hl7.observation_identifier(message.data, patient_id)
        if key is not None:
            found = stores.observations_by_identifier([key]).get(key)
            if found is not None and stores.is_ours(found, stores.patient_references()):
                observation = found
                provenance = _provenance_for(stores, found)

    sent = hl7.message_text(message.data) if message is not None else None
    if sent is None:
        sent = sample_text(record.sample_name, patient_id, sample_patient)
    obs_ref = f"Observation/{observation['id']}" if observation and observation.get("id") else None
    return {
        "attempt": attempt_row(record, lookup, obs_ref),
        "messageSent": sent,
        "resources": collection([resource, observation, provenance]),
    }
