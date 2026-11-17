"""Read the HL7v2 ORU^R01 that cuff-sim reports it sent (``messageSent`` in its result).

**Terms.** HL7 version 2 (HL7v2) is the pipe-delimited message format most hospital devices and
systems still exchange. A message is a list of *segments* separated by a carriage return; each
segment is a three-letter name and *fields* separated by ``|``, and a field's *components* are
separated by ``^``. ``ORU^R01`` ("unsolicited observation result") is the message a device sends
to report a measurement. The segments used here (the brief's segment usage):

- ``MSH`` (message header): MSH-9 the message type ``ORU^R01``, MSH-10 the *message control
  ID*, unique per message, which the ACK echoes and v2-to-fhir keys the Observation on;
- ``PID`` (patient identification): PID-3.1 the patient identifier;
- ``OBR`` (observation request): the panel, LOINC 85354-9;
- ``OBX`` (observation result), one per value: OBX-3 the LOINC code (8480-6 systolic, 8462-4
  diastolic), OBX-5 the value, OBX-6 the unit (UCUM ``mm[Hg]``), OBX-14 when it was measured.

The text is parsed with hl7apy through T07's :func:`pulsebridge_hl7v2.parse_er7` (CLAUDE.md:
HL7v2 is never split by hand). Reading what cuff-sim sent lets the smoke test compare the
Observation with the message's own values and time, not only with the numbers it asked for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pulsebridge_hl7v2 import parse_er7

HL7_NULL = '""'
SYSTOLIC = "8480-6"
DIASTOLIC = "8462-4"


@dataclass(frozen=True)
class SentReading:
    message_type: str  # MSH-9, e.g. "ORU^R01^ORU_R01"
    control_id: str  # MSH-10
    patient_id: str  # PID-3.1 (never printed: on a real-data stack it identifies a person)
    values: dict[str, str]  # OBX-3.1 LOINC code -> OBX-5 text
    units: dict[str, str]  # OBX-3.1 -> OBX-6.1
    observed_at: dict[str, datetime | None]  # OBX-3.1 -> OBX-14 as an aware datetime


def _text(element: object) -> str:
    value = element.to_er7().strip()  # type: ignore[attr-defined]
    return "" if value == HL7_NULL else value


def _component(segment: object, field: str, number: int) -> str:
    """Component ``number`` of the first non-empty repetition of ``field`` (e.g. PID-3.1)."""
    repetitions = [rep for rep in getattr(segment, field) if _text(rep)]
    return _text(getattr(repetitions[0], f"{field}_{number}")) if repetitions else ""


def _segments(element: object, name: str) -> list:
    """Every segment called ``name`` under ``element`` (in ORU^R01 they sit inside groups)."""
    found = []
    for child in element.children:  # type: ignore[attr-defined]
        if child.classname == "Segment":
            if child.name == name:
                found.append(child)
        elif child.classname == "Group":
            found.extend(_segments(child, name))
    return found


def parse_dtm(text: str) -> datetime | None:
    """An HL7 DTM with seconds and a UTC offset (``YYYYMMDDHHMMSS+ZZZZ``, what cuff-sim writes
    in OBX-14), as an aware datetime; None for anything else."""
    try:
        return datetime.strptime(text, "%Y%m%d%H%M%S%z")
    except ValueError:
        return None


def read_sent_reading(er7: str) -> SentReading:
    """The fields above, from the message text. Raises ``ValueError`` (T07's ``HL7ParseError``
    is one) when hl7apy cannot read it."""
    message = parse_er7(er7)
    msh = message.msh
    pids = _segments(message, "PID")
    values: dict[str, str] = {}
    units: dict[str, str] = {}
    times: dict[str, datetime | None] = {}
    for obx in _segments(message, "OBX"):
        code = _component(obx, "obx_3", 1)
        values[code] = _text(obx.obx_5)
        units[code] = _component(obx, "obx_6", 1)
        times[code] = parse_dtm(_text(obx.obx_14))
    return SentReading(
        message_type=_text(msh.msh_9),
        control_id=_text(msh.msh_10),
        patient_id=_component(pids[0], "pid_3", 1) if pids else "",
        values=values,
        units=units,
        observed_at=times,
    )
