"""What may be shared, and exactly what is sent to OpenEMR for it.

**Only two kinds of reading are shared**, each checked by its structure, not only its code:

- heart rate: LOINC ``8867-4`` with a ``valueQuantity`` in UCUM ``/min`` (the R4 heartrate
  profile's code and unit; T06's five-minute windows add a min and max as ``component``);
- blood pressure: the LOINC panel ``85354-9`` with systolic ``8480-6`` and diastolic ``8462-4``
  components in UCUM ``mm[Hg]`` (the R4 bp profile).

S1 saw OpenEMR require exactly ``/min`` and ``mm[Hg]`` in ``valueQuantity.code``.

**Only ``final`` Observations are shared** (R4 ``ObservationStatus``). ``preliminary`` can still
change in our store and the clinic's copy would not follow; ``amended`` (changed after being
final, T33) is not shared either: OpenEMR refuses a second reading at the same time in the same
Encounter, so a corrected value could not replace the first one there, and sharing it would
leave the clinic with whichever arrived first.

**The Observation sent is built from allow-lists**, element by element, not by deleting fields:
``status``; ``category``, ``code`` and ``method`` as CodeableConcepts (``coding`` and ``text``),
each Coding only ``system``/``code``/``display`` and only from the system the builders use
(observation-category, LOINC; the method is text only); ``valueQuantity`` and each
``component`` (``code``, ``valueQuantity``) with Quantities of ``value``/``unit``/``system``
(UCUM)/``code``; one ``effectiveDateTime``; then ``subject`` and ``encounter`` set to OpenEMR's
own references. Nothing else is copied at any depth: no ``id``, ``meta``, ``identifier``,
``device``, ``performer``, ``extension``, ``_``-prefixed primitive extensions, notes or
references. A concept with nothing left is dropped, so no empty object is sent.

**A heart-rate window is sent at one time.** OpenEMR 8.5.0 reads only ``effectiveDateTime`` for
a vitals write and never ``effectivePeriod`` (its ``FhirObservationVitalsService``), so the
window's start, with its offset, is sent as ``effectiveDateTime``. OpenEMR files one pulse per
time and skips component codes it has no column for, so it keeps the average, not the minimum
and maximum. Both from reading OpenEMR's source; not verified live.

**The Encounter.** OpenEMR files vitals as a form on a visit, and refuses an Observation without
``encounter`` (S1). One Encounter is used per patient and UTC calendar day of the reading:
the time sent (``effectiveDateTime``; a heart-rate window's start), converted to UTC.
S1's create needed ``status``, ``class`` (v3 ActCode ``AMB``, ambulatory), ``subject`` and
``period``; the period covers that UTC day.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

LOINC = "http://loinc.org"
UCUM = "http://unitsofmeasure.org"
HEART_RATE = "8867-4"
BLOOD_PRESSURE_PANEL = "85354-9"
SYSTOLIC = "8480-6"
DIASTOLIC = "8462-4"
V3_ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
# R4 ObservationStatus, to word a refusal without quoting an unknown value.
OBSERVATION_STATUSES = (
    "registered",
    "preliminary",
    "final",
    "amended",
    "corrected",
    "cancelled",
    "entered-in-error",
    "unknown",
)
CATEGORY_SYSTEM = "http://terminology.hl7.org/CodeSystem/observation-category"
# The code systems each CodeableConcept may carry (the systems T06's builders use). A coding
# from any other system is dropped; a concept left with neither coding nor text is dropped.
CATEGORY_SYSTEMS = frozenset({CATEGORY_SYSTEM})
CODE_SYSTEMS = frozenset({LOINC})
METHOD_SYSTEMS: frozenset[str] = frozenset()  # T06 writes the method as text only
_TEXT_LIMIT = 1024


def _codes(concept: object) -> set[tuple[object, object]]:
    if not isinstance(concept, dict):
        return set()
    return {
        (c.get("system"), c.get("code")) for c in concept.get("coding") or [] if isinstance(c, dict)
    }


def _is_quantity(value: object, unit: str) -> bool:
    return (
        isinstance(value, dict)
        and value.get("system") == UCUM
        and value.get("code") == unit
        and isinstance(value.get("value"), int | float)
        and not isinstance(value.get("value"), bool)
    )


def kind_of(observation: dict) -> str | None:
    """``heart-rate``, ``blood-pressure`` or None (not shareable)."""
    codes = _codes(observation.get("code"))
    if (LOINC, HEART_RATE) in codes and _is_quantity(observation.get("valueQuantity"), "/min"):
        return "heart-rate"
    if (LOINC, BLOOD_PRESSURE_PANEL) in codes:
        found = {}
        for component in observation.get("component") or []:
            if not isinstance(component, dict):
                continue
            for code in (SYSTOLIC, DIASTOLIC):
                if (LOINC, code) in _codes(component.get("code")) and _is_quantity(
                    component.get("valueQuantity"), "mm[Hg]"
                ):
                    found[code] = True
        if found.get(SYSTOLIC) and found.get(DIASTOLIC):
            return "blood-pressure"
    return None


def effective_instant(observation: dict) -> datetime | None:
    """When the reading was taken: ``effectiveDateTime``, else ``effectivePeriod.start``. Only
    a full date and time with a zone (an R4 dateTime with a time must carry one)."""
    value = observation.get("effectiveDateTime")
    if value is None and isinstance(observation.get("effectivePeriod"), dict):
        value = observation["effectivePeriod"].get("start")
    if not isinstance(value, str) or "T" not in value or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def utc_day(observation: dict) -> str | None:
    """The UTC calendar day of the reading, ``YYYY-MM-DD`` (the Encounter rule)."""
    at = effective_instant(observation)
    return None if at is None else at.astimezone(UTC).date().isoformat()


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= _TEXT_LIMIT else None


def _coding(value: object, systems: frozenset[str]) -> dict | None:
    """R4 Coding, allow-listed: ``system``, ``code``, ``display`` only, from a known system."""
    if not isinstance(value, dict) or value.get("system") not in systems:
        return None
    out = {k: _string(value.get(k)) for k in ("system", "code", "display")}
    out = {k: v for k, v in out.items() if v is not None}
    return out if "code" in out else None


def _concept(value: object, systems: frozenset[str]) -> dict | None:
    """R4 CodeableConcept, allow-listed: ``coding`` (see :func:`_coding`) and ``text``."""
    if not isinstance(value, dict):
        return None
    codings = [c for c in (_coding(x, systems) for x in value.get("coding") or []) if c]
    out: dict = {}
    if codings:
        out["coding"] = codings
    text = _string(value.get("text"))
    if text is not None:
        out["text"] = text
    return out or None


def _quantity(value: object) -> dict | None:
    """R4 Quantity, allow-listed: ``value``, ``unit``, ``system`` (UCUM only), ``code``."""
    if not isinstance(value, dict) or value.get("system") != UCUM:
        return None
    number = value.get("value")
    if not isinstance(number, int | float) or isinstance(number, bool):
        return None
    out: dict = {"value": number}
    for key in ("unit", "system", "code"):
        text = _string(value.get(key))
        if text is not None:
            out[key] = text
    return out


def sent_effective_time(stored: dict) -> str | None:
    """The one time OpenEMR files the reading at: ``effectiveDateTime`` as stored, or for a
    heart-rate window ``effectivePeriod.start`` with its own offset.

    OpenEMR 8.5.0 reads only ``effectiveDateTime`` for a vitals write and refuses an
    Observation without it (``FhirObservationVitalsService``: "Observation.effectiveDateTime is
    required; it identifies the vitals reading being written"); it never reads
    ``effectivePeriod``. So a five-minute window is sent as its start. From reading OpenEMR's
    source; not verified against a live server (README, "Verify against a live OpenEMR")."""
    if effective_instant(stored) is None:
        return None
    value = stored.get("effectiveDateTime")
    if value is None:
        value = stored["effectivePeriod"]["start"]
    return value


def observation_for_openemr(stored: dict, patient_ref: str, encounter_ref: str) -> dict:
    """The Observation as POSTed to OpenEMR, built element by element from allow-lists (see the
    module docstring): nothing is copied that is not named here, at any depth."""
    sent: dict = {"resourceType": "Observation", "status": "final"}
    categories = [
        c for c in (_concept(x, CATEGORY_SYSTEMS) for x in stored.get("category") or []) if c
    ]
    if categories:
        sent["category"] = categories
    code = _concept(stored.get("code"), CODE_SYSTEMS)
    if code is not None:
        sent["code"] = code
    effective = sent_effective_time(stored)
    if effective is not None:
        sent["effectiveDateTime"] = effective
    quantity = _quantity(stored.get("valueQuantity"))
    if quantity is not None:
        sent["valueQuantity"] = quantity
    method = _concept(stored.get("method"), METHOD_SYSTEMS)
    if method is not None:
        sent["method"] = method
    components = []
    for component in stored.get("component") or []:
        if not isinstance(component, dict):
            continue
        part = {
            "code": _concept(component.get("code"), CODE_SYSTEMS),
            "valueQuantity": _quantity(component.get("valueQuantity")),
        }
        part = {k: v for k, v in part.items() if v is not None}
        if "code" in part:
            components.append(part)
    if components:
        sent["component"] = components
    sent["subject"] = {"reference": patient_ref}
    sent["encounter"] = {"reference": encounter_ref}
    return sent


def sent_code(stored: dict) -> str:
    """The LOINC code of the reading (8867-4 or 85354-9): the one OpenEMR's duplicate text names."""
    return HEART_RATE if kind_of(stored) == "heart-rate" else BLOOD_PRESSURE_PANEL


def encounter_for_openemr(patient_ref: str, day: str) -> dict:
    """The Encounter created for a patient's UTC day (S1: status, class, subject, period)."""
    return {
        "resourceType": "Encounter",
        # finished: the "visit" is a record of self-measured readings, not an open visit.
        "status": "finished",
        "class": {"system": V3_ACT_CODE, "code": "AMB", "display": "ambulatory"},
        "subject": {"reference": patient_ref},
        "period": {"start": f"{day}T00:00:00Z", "end": f"{day}T23:59:59Z"},
    }


def leaks(sent: object, internal: list[str]) -> list[str]:
    """Which of our internal values appear anywhere in a resource we are about to send
    (internal ids, the Google Health user id, our own identifier systems). A guard run before
    OpenEMR is called; the allow-list should make it always empty."""
    text = json.dumps(sent)
    return [value for value in internal if value and value in text]


def references(value: object, path: str = "") -> list[str]:
    """Every ``reference`` path in a resource (to check only subject and encounter remain)."""
    found = []
    if isinstance(value, dict):
        for k, v in value.items():
            here = f"{path}.{k}" if path else k
            if k == "reference":
                found.append(path)
            found.extend(references(v, here))
    elif isinstance(value, list):
        for v in value:
            found.extend(references(v, path))
    return found
