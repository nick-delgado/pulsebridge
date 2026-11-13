# pulsebridge_hl7v2

Thin HL7v2 helpers over [`hl7apy`](https://github.com/crs4/hl7apy) 1.3.5 (pinned exactly), shared
by the `v2-to-fhir` mapper (T12) and the cuff simulator (T13). Pure Python, no GCP calls.

Decision D3: hl7apy builds, validates and parses messages. Nothing here splits ER7 text into
segments and fields itself. What is ours: the blood-pressure ORU^R01 builder, a validation wrapper
with readable errors, ACK reading into a small result type, MLLP framing, and the sample messages.
The v2-to-FHIR mapping is not here; it is T12.

## Public API

```python
from datetime import UTC, datetime
from pulsebridge_hl7v2 import (
    BloodPressureReading,
    Patient,
    build_bp_oru_r01,
    new_control_id,
    to_er7_bytes,
    validate_oru_r01,
    HL7ValidationError,
    parse_er7,
    HL7ParseError,
    parse_ack,
    Ack,
    AckCode,
    AckParseError,
    frame,
    MllpReader,
    MllpDecoder,
    MllpTimeout,
    MllpConnectionClosed,
)
from pulsebridge_hl7v2.samples import load, by_class
```

| Function / type | Use |
| --- | --- |
| `build_bp_oru_r01(reading, patient, *, control_id, sent_at=None, options=None) -> hl7apy Message` | Build the ORU^R01. Raises `ValueError` for impossible values (systolic must exceed diastolic, 1 to 400 mm[Hg], times must have a time zone). |
| `to_er7_bytes(message) -> bytes` | ER7 bytes for the wire: every segment ends with `\r`, ASCII. |
| `new_control_id() -> str` | A fresh 20-character MSH-10. |
| `validate_oru_r01(bytes \| str \| Message) -> Message` | hl7apy STRICT validation plus the checks hl7apy skips. Raises `HL7ValidationError` with `.problems`, one sentence each, all problems at once. |
| `parse_er7(bytes \| str, *, strict=False) -> Message` | hl7apy parse with groups found (`msg.oru_r01_patient_result...`). Tolerant by default: reads what the store accepted. Raises `HL7ParseError` with `.reason` (including for an unknown MSH-12 such as `9.9`, where hl7apy raises `UnsupportedVersion`). |
| `decode_escapes(text, encoding_chars) -> str` | Turn `\F\ \S\ \T\ \R\ \E\` back into characters (hl7apy has no unescape). |
| `parse_ack(bytes \| str) -> Ack` | Read an ACK: `code` (`AckCode.AA` ...), `acknowledged_control_id` (MSA-2 or `None`), `reason` (MSA-3, unescaped, or `None`), `accepted`, `message_type`, `ack_control_id`, `version`, `raw`. Raises `AckParseError`. Takes plain ACK bytes; remove the MLLP frame or the HTTPS base64/JSON first. |
| `frame(payload) -> bytes` | `0x0B` + payload + `0x1C 0x0D`. |
| `MllpDecoder().feed(chunk) -> list[bytes]` | Stream decoder: returns every frame completed by a chunk, keeps partial frames. |
| `MllpReader(sock).read_frame(timeout) -> bytes` | Next frame from a socket, or `MllpTimeout`, or `MllpConnectionClosed(partial=...)`: the three outcomes S4 found the adapter produces. |
| `pulsebridge_hl7v2.samples` | Shared sample messages; see [`samples/README.md`](samples/README.md). |

A typical send, as T13 would do it:

```python
msg = build_bp_oru_r01(
    BloodPressureReading(systolic_mmhg=120, diastolic_mmhg=80, observed_at=datetime.now(UTC)),
    Patient(
        identifier="SYN-000123",
        assigning_authority="PULSEBRIDGE",
        family_name="TESTPATIENT",
        given_name="SYNTHIA",
    ),
    control_id=new_control_id(),
)
payload = to_er7_bytes(msg)
validate_oru_r01(payload)  # never send what fails here
sock.sendall(frame(payload))
ack = parse_ack(MllpReader(sock).read_frame(timeout=5))
if not ack.accepted:
    print(ack.code, ack.reason)  # MSA-2 may be None on a NACK: do not require a match
```

## What the builder writes (HL7 v2.5.1)

Version 2.5.1, because spike S4 found the HL7v2 store accepts 2.3 to 2.8 and 2.5.1 is the most
widely implemented 2.5 release. Example output, segments shown one per line (on the wire each
ends with `\r`):

```
MSH|^~\&|CUFFSIM|PULSEBRIDGE|PHR|PULSEBRIDGE|20261113205329+0000||ORU^R01^ORU_R01|SYN-VALID-0001|P|2.5.1
PID|1||SYN-000123^^^PULSEBRIDGE^MR||TESTPATIENT^SYNTHIA
OBR|1||SYN-VALID-0001^CUFFSIM|85354-9^Blood pressure panel with all children optional^LN|||20261113205329+0000
OBX|1|NM|8480-6^Systolic blood pressure^LN||120|mm[Hg]^mm[Hg]^UCUM|||||F|||20261113205329+0000
OBX|2|NM|8462-4^Diastolic blood pressure^LN||80|mm[Hg]^mm[Hg]^UCUM|||||F|||20261113205329+0000
```

| Field | Value | Why |
| --- | --- | --- |
| MSH-1, MSH-2 | `\|`, `^~\&` | Field separator, then component, repetition, escape and subcomponent characters. hl7apy's defaults. |
| MSH-3 to MSH-6 | `CUFFSIM`, `PULSEBRIDGE`, `PHR`, `PULSEBRIDGE` | Sending and receiving application and facility; software names, configurable through `Endpoints`. The store shows MSH-4 as `sendFacility`. |
| MSH-7 | message time with UTC offset | Required in 2.5.1. The store reads it as `sendTime`. |
| MSH-9 | `ORU^R01^ORU_R01` | Message code, trigger event, message structure. |
| MSH-10 | control ID | Identifies the message; the ACK repeats it in MSA-2. See below. |
| MSH-11 | `P` | Processing ID: production (table 0103). |
| MSH-12 | `2.5.1` | Version ID. |
| MSH-15, MSH-16 | empty | Original acknowledgment mode. S4: the store ignores them. |
| PID-3 | `id^^^authority^MR` | Patient identifier (CX): ID, assigning authority (PID-3.4), type `MR` (PID-3.5). The store lists PID-3 as `patientIds`. |
| PID-5 | `family^given` | Patient name (XPN). Required in 2.5.1. |
| OBR-3 | `control-id^CUFFSIM` | Filler order number. 2.5.1 wants OBR-2 or OBR-3; one reading is one order. |
| OBR-4 | LOINC `85354-9` | Universal service identifier: the blood-pressure panel. |
| OBR-7 | observation time | Observation date/time of the order. |
| OBX-2 | `NM` | Value type: numeric. It decides how OBX-5 must look. |
| OBX-3 | LOINC `8480-6` / `8462-4` | What was measured: systolic / diastolic. |
| OBX-5 | integer | The value. |
| OBX-6 | `mm[Hg]^mm[Hg]^UCUM` | Unit, coded in UCUM. |
| OBX-11 | `F` | Result status: final (table 0085). Required. |
| OBX-14 | observation time with offset | When it was measured. Always written with its UTC offset so T12 never has to guess the time zone; a naive `datetime` is refused. |

Values from callers (patient identifier and names) are set through hl7apy datatype objects, so
hl7apy escapes any separator in them (`O|Brien` becomes `O\F\Brien`); they must be printable ASCII.

### The control ID (MSH-10) and resends

The store deduplicates on exact bytes, not on the control ID (spike S5). Resending identical
bytes gives `AA` and one stored message. A resend of the **same reading** with a new MSH-7 is a
**new stored message with the same control ID**. So: generate a new control ID per reading, keep
it when resending that reading, and let T12 key its conditional update on MSH-10. Then a resend
updates the same Observation instead of creating a second one.

## What hl7apy could and could not do

| Need | hl7apy 1.3.5 | What this package does |
| --- | --- | --- |
| Build ORU_R01 with groups, escape values | yes | uses it |
| Required segments and fields, data types, dates | yes (STRICT) | uses it; rewrites "Missing required child PID.PID_3" as "PID-3 (patient identifier list) is required but missing" |
| Report every error, not only the first | only via a report file | gives it a temporary report file |
| OBX-5 checked against OBX-2 | no: "varies" fields are skipped (`# TODO: it should check the real rule` in its validator) | checks each OBX-5 with hl7apy's own datatype class for the OBX-2 type |
| Line-feed separated messages | misleading `UnsupportedVersion` error | pre-check with a clear message |
| Empty MSH-9 at STRICT level | "Cannot instantiate an unknown Element" | checks the message structure first (with hl7apy's `get_message_info`) |
| Store's ACK with MSH-9 = `ACK` only | STRICT: refused; TOLERANT: parsed | reads ACKs at TOLERANT |
| Store's NACK with blank MSH fields and empty MSA-2 | TOLERANT: parsed | no fallback parser needed |
| ACK with no trailing `\r` (HTTPS ingest, S5) | parsed | tested |
| ACK or message with MSH-12 `9.9` | `UnsupportedVersion` | ACK: re-read with hl7apy's `parse_segments` at version 2.5.1. Message: `HL7ParseError` for the mapper to record |
| Unescape `\E\`, `\F\` ... when reading | no | `decode_escapes`, about ten lines |

## Tests

`make test` runs `libs/pulsebridge_hl7v2/tests`. The ACK tests use the raw store replies recorded
in `docs/spikes/s4.md` and `s5.md`; the MLLP reader tests use a loopback TCP socket on
`127.0.0.1` inside the test process. No network or GCP access.
