"""What the proxy reads from a stored HL7v2 message, and how it finds the Observation made from it.

The bytes are parsed with ``hl7apy`` through T07's ``parse_er7`` (D3); nothing here splits ER7
text itself. A stored message came from outside (any sender may reach the adapter), so every
function here treats the bytes as hostile: any failure is ``None``, never an exception.

**From a stored message to its Observation.** ``v2-to-fhir`` (T12) writes the Observation with a
conditional update keyed on a deterministic identifier (T06): UUIDv5 of ``["Observation",
<patient key>, "85354-9", <source key>]``, where the source key is the JSON list of the raw
MSH-3.1 (sending application), MSH-4.1 (sending facility) and MSH-10 (message control ID) text
(``v2_to_fhir/mapping.py``, ``_Header.source_key``). The proxy computes the same identifier from
the stored bytes and searches ``Observation?identifier=urn:pulsebridge:observation|<key>``: an
identifier search, which Cloud Healthcare answers from a synchronous index (no indexing delay).
The Provenance is then read by its own deterministic id (UUIDv5 of the Observation's key, T06),
and its source entity names the stored message (T12), which confirms the join.

A test runs T07's samples through the real mapper and checks that both sides compute the same
identifier, so a change in the mapper's key cannot silently break the join.
"""

from __future__ import annotations

import json

from pulsebridge_fhir import PatientInput, build_patient, codes
from pulsebridge_fhir.keys import deterministic_uuid
from pulsebridge_hl7v2 import parse_er7

HL7_NULL = '""'


def _text(element: object) -> str:
    value = element.to_er7().strip()  # type: ignore[attr-defined]
    return "" if value == HL7_NULL else value


def _first_component(segment: object, field: str, number: int) -> str:
    """Component ``number`` of the first non-empty repetition (the mapper's ``_component``)."""
    repetitions = [rep for rep in getattr(segment, field) if _text(rep)]
    return _text(getattr(repetitions[0], f"{field}_{number}")) if repetitions else ""


def _header(data: bytes) -> tuple[str, str, str] | None:
    """Raw MSH-3.1, MSH-4.1 and MSH-10, as the mapper reads them; None if unreadable."""
    try:
        message = parse_er7(data)
        msh = message.msh
        application = _first_component(msh, "msh_3", 1)
        facility = _first_component(msh, "msh_4", 1)
        control_id = _text(msh.msh_10)
    except Exception:  # noqa: BLE001 - hostile bytes: any failure means "cannot tell"
        return None
    return application, facility, control_id


def control_id(data: bytes | None) -> str | None:
    """MSH-10, or None (no MSH, unreadable, empty). At most 64 characters are kept."""
    if not data:
        return None
    header = _header(data)
    if header is None or not header[2]:
        return None
    return header[2][:64]


def patient_key(patient_id: str) -> str:
    """T06's key for the configured Patient (what the Observation keys are derived from)."""
    return build_patient(PatientInput(patient_id)).key


def observation_identifier(data: bytes | None, patient_id: str) -> str | None:
    """The identifier value v2-to-fhir gives the Observation it maps from these bytes, or None
    when the header cannot be read (then the mapper cannot have written one either)."""
    if not data:
        return None
    header = _header(data)
    if header is None or not header[0] or not header[2]:
        return None
    source_key = json.dumps(list(header), ensure_ascii=False, separators=(",", ":"))
    return deterministic_uuid(
        "Observation", patient_key(patient_id), codes.BP_PANEL["code"], source_key
    )


def provenance_id(observation_identifier_value: str) -> str:
    """T06: a Provenance's id is UUIDv5 of ``["Provenance", <the Observation's key>]``."""
    return deterministic_uuid("Provenance", observation_identifier_value)


def message_text(data: bytes | None) -> str | None:
    """The stored bytes as text for the trace panel ("HL7v2 as sent"). Segments stay separated
    by ``\\r``; bytes that are not UTF-8 show as U+FFFD instead of failing the trace."""
    if data is None:
        return None
    return data.decode("utf-8", errors="replace")
