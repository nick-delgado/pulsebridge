"""The FHIR R4 rules a blood-pressure reading must meet once it is in the FHIR store, and once it
is read back from OpenEMR. Pure functions: each returns a problem sentence, or None when fine.

**Terms.** FHIR (Fast Healthcare Interoperability Resources) R4 stores health data as JSON
*resources*. A measurement is an ``Observation``; for blood pressure the vital-signs profile
(R4 ``StructureDefinition/bp``) says it is a *panel*: ``code`` is LOINC 85354-9 ("Blood pressure
panel with all children optional") and the two numbers are ``component`` entries, LOINC 8480-6
(systolic) and 8462-4 (diastolic), each a ``valueQuantity`` in UCUM ``mm[Hg]``. LOINC is the
standard catalogue of observation codes; UCUM is the standard unit syntax (``mm[Hg]``, not
``mmHg``, is the code; ``mmHg`` is only the human-readable ``unit``). ``category`` vital-signs
is what lets any FHIR client find it as a vital sign. ``device`` names the cuff. ``status``
``final`` means complete; the share to the clinic only sends ``final`` readings.

A ``Provenance`` resource records where a resource came from: its ``target`` is the Observation,
its source ``entity`` the stored HL7v2 message. CLAUDE.md requires one for every Observation.
Without these the reading would still be numbers in a database, but no other FHIR system could
interpret it, and nobody could tell which message it was made from.
"""

from __future__ import annotations

from datetime import datetime

LOINC = "http://loinc.org"
UCUM = "http://unitsofmeasure.org"
OBSERVATION_CATEGORY = "http://terminology.hl7.org/CodeSystem/observation-category"
BP_PANEL = "85354-9"
SYSTOLIC = "8480-6"
DIASTOLIC = "8462-4"
MM_HG = "mm[Hg]"


def _list(value: object) -> list:
    """``value`` if it is a JSON array, else empty: an answer of another shape never raises."""
    return value if isinstance(value, list) else []


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def parse_instant(text: object) -> datetime | None:
    """An R4 dateTime with a time and a zone (``2026-11-17T10:00:00+00:00`` or ``...Z``)."""
    if not isinstance(text, str) or "T" not in text:
        return None
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def _codings(concept: object) -> list[dict]:
    if not isinstance(concept, dict) or not isinstance(concept.get("coding"), list):
        return []
    return [c for c in concept["coding"] if isinstance(c, dict)]


def has_code(concept: object, system: str, code: str) -> bool:
    return any(c.get("system") == system and c.get("code") == code for c in _codings(concept))


def component_quantity(observation: dict, loinc: str) -> dict | None:
    """The ``valueQuantity`` of the component coded ``loinc``, or None (absent or repeated)."""
    found = [
        c
        for c in _list(observation.get("component"))
        if isinstance(c, dict) and has_code(c.get("code"), LOINC, loinc)
    ]
    if len(found) != 1 or not isinstance(found[0].get("valueQuantity"), dict):
        return None
    return found[0]["valueQuantity"]


def _number(quantity: dict | None) -> float | None:
    value = quantity.get("value") if quantity else None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def values_problem(observation: dict, systolic: int, diastolic: int) -> str | None:
    """Both components present with exactly the values that were sent."""
    got = {
        "systolic": _number(component_quantity(observation, SYSTOLIC)),
        "diastolic": _number(component_quantity(observation, DIASTOLIC)),
    }
    want = {"systolic": systolic, "diastolic": diastolic}
    if got != want:
        return f"components are {got}, expected {want} (LOINC {SYSTOLIC} / {DIASTOLIC})"
    return None


def units_problem(observation: dict) -> str | None:
    """Each component's quantity in UCUM ``mm[Hg]`` (system and code, R4 Quantity)."""
    for name, loinc in (("systolic", SYSTOLIC), ("diastolic", DIASTOLIC)):
        quantity = component_quantity(observation, loinc) or {}
        if quantity.get("system") != UCUM or quantity.get("code") != MM_HG:
            return (
                f"{name} quantity has system {quantity.get('system')!r} and code "
                f"{quantity.get('code')!r}; expected {UCUM!r} and {MM_HG!r}"
            )
    return None


def time_problem(observation: dict, expected: datetime | None) -> str | None:
    """``effectiveDateTime`` is the same instant as OBX-14 of the message that was sent."""
    at = parse_instant(observation.get("effectiveDateTime"))
    if at is None:
        return f"effectiveDateTime {observation.get('effectiveDateTime')!r} is not a zoned time"
    if expected is None:
        return "the sent message has no readable OBX-14, so the time cannot be compared"
    if at != expected:
        return f"effectiveDateTime is {at.isoformat()}, the message said {expected.isoformat()}"
    return None


def store_observation_checks(
    observation: object, *, systolic: int, diastolic: int, observed_at: datetime | None
) -> list[tuple[str, str | None]]:
    """(what, problem) for every rule of a blood-pressure Observation as stored by v2-to-fhir."""
    if not isinstance(observation, dict) or observation.get("resourceType") != "Observation":
        return [("is an Observation", "the trace's resource is not an Observation")]
    device = observation.get("device")
    reference = device.get("reference") if isinstance(device, dict) else None
    subject = observation.get("subject")
    subject_ref = subject.get("reference") if isinstance(subject, dict) else None
    category_ok = any(
        has_code(c, OBSERVATION_CATEGORY, "vital-signs") for c in _list(observation.get("category"))
    )
    return [
        (
            "code is the LOINC blood-pressure panel 85354-9",
            None
            if has_code(observation.get("code"), LOINC, BP_PANEL)
            else f"code has no {LOINC} {BP_PANEL} coding",
        ),
        (
            "category is vital-signs",
            None if category_ok else "no observation-category vital-signs coding",
        ),
        (
            "status is final",
            None
            if observation.get("status") == "final"
            else f"status is {observation.get('status')!r}",
        ),
        ("values equal what was sent", values_problem(observation, systolic, diastolic)),
        ("units are UCUM mm[Hg]", units_problem(observation)),
        ("effectiveDateTime equals OBX-14", time_problem(observation, observed_at)),
        (
            "device reference is set",
            None
            if isinstance(reference, str) and reference.startswith("Device/")
            else f"device.reference is {reference!r}",
        ),
        (
            "subject is a Patient",
            None
            if isinstance(subject_ref, str) and subject_ref.startswith("Patient/")
            else f"subject.reference is {subject_ref!r}",
        ),
    ]


def provenance_problem(
    provenance: object, observation_id: str, message_name: str | None
) -> str | None:
    """The Provenance targets this Observation and names the stored message as its source."""
    if not isinstance(provenance, dict) or provenance.get("resourceType") != "Provenance":
        return "the trace has no Provenance for the Observation"
    targets = [_dict(t).get("reference") for t in _list(provenance.get("target"))]
    if f"Observation/{observation_id}" not in targets:
        return f"Provenance.target is {targets}, expected Observation/{observation_id}"
    if not provenance.get("recorded"):
        return "Provenance.recorded is missing"
    sources = [_dict(e) for e in _list(provenance.get("entity"))]
    names = [
        _dict(_dict(s.get("what")).get("identifier")).get("value")
        for s in sources
        if s.get("role") == "source"
    ]
    # Compared on the message id (after "/messages/"): the source value may be the full
    # resource name or the store-relative one, depending on what the notification carried.
    wanted = _message_id(message_name)
    if wanted is not None and wanted not in {_message_id(n) for n in names}:
        return "Provenance's source entity does not name the stored HL7v2 message"
    return None


def _message_id(name: object) -> str | None:
    if not isinstance(name, str) or "/messages/" not in name:
        return None
    return name.rsplit("/messages/", 1)[1] or None


def openemr_observation_checks(
    stored: object, *, systolic: int, diastolic: int, effective: datetime | None
) -> list[tuple[str, str | None]]:
    """What OpenEMR keeps of a shared blood pressure (D13: the read-back, as stored there).
    OpenEMR files vitals as a form on an Encounter and keeps ``effectiveDateTime`` (confirmed on
    the first dev deploy), the panel code and both components."""
    if not isinstance(stored, dict) or stored.get("resourceType") != "Observation":
        return [("read back from OpenEMR", "readBack.observation.stored is not an Observation")]
    return [
        (
            "OpenEMR has the panel code 85354-9",
            None
            if has_code(stored.get("code"), LOINC, BP_PANEL)
            else f"OpenEMR's code has no {LOINC} {BP_PANEL} coding",
        ),
        ("OpenEMR has the values that were sent", values_problem(stored, systolic, diastolic)),
        ("OpenEMR has the time of the reading", time_problem(stored, effective)),
    ]
