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
| `build_bp_oru_r01(reading, patient, *, control_id, sent_at=None, options=None) -> hl7apy Message` | Build the ORU^R01. Raises `ValueError` for impossible values: systolic must exceed diastolic, 1 to 400 mm[Hg]; times need a time zone whose offset is whole minutes; caller text must be printable ASCII without a backslash; `processing_id` must be `P`, `T` or `D`. |
| `to_er7_bytes(message) -> bytes` | ER7 bytes for the wire: every segment ends with `\r`, ASCII. |
| `new_control_id() -> str` | A fresh 20-character MSH-10. |
| `validate_oru_r01(bytes \| str \| Message) -> Message` | hl7apy STRICT validation plus the checks hl7apy skips (OBX-5 against OBX-2, and the stricter NM rule below). Raises `HL7ValidationError` with `.problems`, one sentence each, all problems at once, for any bad input including bytes that are not UTF-8. |
| `parse_er7(bytes \| str, *, strict=False) -> Message` | hl7apy parse with groups found (`msg.oru_r01_patient_result...`). Tolerant by default: reads what the store accepted. Raises `HL7ParseError` with `.reason`, including for an unknown or empty MSH-12 (`9.9`, or nothing), where hl7apy raises `UnsupportedVersion`. |
| `decode_escapes(text, encoding_chars) -> str` | Turn `\F\ \S\ \T\ \R\ \E\` back into characters (hl7apy has no unescape). |
| `parse_ack(bytes \| str) -> Ack` | Read an ACK: `code` (`AckCode.AA` ...), `acknowledged_control_id` (MSA-2 or `None`), `reason` (MSA-3, unescaped, or `None`), `accepted`, `message_type`, `ack_control_id`, `version`, `raw`. Raises `AckParseError`. Takes plain ACK bytes; remove the MLLP frame or the HTTPS base64/JSON first. |
| `frame(payload) -> bytes` | `0x0B` + payload + `0x1C 0x0D`. |
| `MllpDecoder(max_frame_bytes=1 MiB).feed(chunk) -> list[bytes]` | Stream decoder: returns every frame completed by a chunk, keeps partial frames. Searches each byte once, so cost grows with the data, not its square. A frame longer than `max_frame_bytes` raises `MllpFrameTooLarge` and the buffer is dropped. |
| `MllpReader(sock, max_frame_bytes=1 MiB).read_frame(timeout) -> bytes` | Next frame from a socket, or `MllpTimeout` (one deadline for the whole frame, even if bytes trickle in), or `MllpConnectionClosed(partial=..., reason=...)` for a close, a reset or any other socket error: the three outcomes S4 found the adapter produces. An oversized reply raises `MllpFrameTooLarge`; treat it as "no usable acknowledgment". |
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
| MSH-11 | `P` | Processing ID (table 0103): `P` production, `T` training, `D` debugging; nothing else is accepted. Written to MSH-11.1. |
| MSH-12 | `2.5.1` | Version ID. |
| MSH-15, MSH-16 | empty | Original acknowledgment mode. S4: the store ignores them. |
| PID-3 | `id^^^authority^MR` | Patient identifier (CX): ID, assigning authority (PID-3.4), type `MR` (PID-3.5). The store lists PID-3 as `patientIds`. |
| PID-5 | `family^given` | Patient name (XPN). Required in 2.5.1. |
| OBR-3 | `control-id^CUFFSIM` | Filler order number. 2.5.1 wants OBR-2 or OBR-3; one reading is one order. |
| OBR-4 | LOINC `85354-9` | Universal service identifier: the blood-pressure panel. |
| OBR-7 | observation time | Observation date/time of the order. |
| OBX-2 | `NM` | Value type: numeric. It decides how OBX-5 must look. |
| OBX-3 | LOINC `8480-6` / `8462-4` | What was measured: systolic / diastolic. |
| OBX-5 | integer | The value. Validation holds NM values to HL7's own rule (below). |
| OBX-6 | `mm[Hg]^mm[Hg]^UCUM` | Unit, coded in UCUM. |
| OBX-11 | `F` | Result status: final (table 0085). Required. |
| OBX-14 | observation time with offset | When it was measured. Always written with its UTC offset so T12 never has to guess the time zone; a naive `datetime` is refused. |

### Caller values cannot add structure

The patient identifier, assigning authority, identifier type, names and endpoint names come from
the caller. Each is set on its own component as an hl7apy datatype object, never as a plain
string. That matters because hl7apy parses a plain string as ER7: assigned to a whole field, `^`
starts a new component; assigned to a component, `&` starts a new subcomponent. A datatype
object is taken as a value, and hl7apy escapes every separator in it on output:

| Caller value | Written as |
| --- | --- |
| `SYN\|1^2&3~4` (identifier) | `SYN\F\1\S\2\T\3\R\4` |
| `O\|BRIEN^X&Y~Z` (family name) | `O\F\BRIEN\S\X\T\Y\R\Z` |

The backslash is refused outright, with an error naming the field. It is HL7's escape character
(MSH-2), and hl7apy leaves a backslash that already forms an escape sequence alone: `A\F\B` would
reach the receiver unchanged and be decoded there as `A|B`. Caller text must also be printable
ASCII, since MSH-18 (character set) is empty.

### NM values: HL7's rule, not Python's

OBX-2 = `NM` (numeric) means OBX-5 may contain only an optional `+` or `-`, ASCII digits and an
optional decimal point (2.5.1 chapter 2A). hl7apy checks NM with Python's `Decimal`, which also
accepts `NaN`, `inf`, `1e3`, `1_000`, surrounding spaces and digits from other scripts (`١٢٠`).
So after hl7apy's check, `validate_oru_r01` applies our own rule, `NM_PATTERN`
(`[+-]?(\d+(\.\d*)?|\.\d+)` matched in full, ASCII only), and reports anything wider.

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
| OBX-5 checked against OBX-2 | no: "varies" fields are skipped (`# TODO: it should check the real rule` in its validator) | checks every OBX-5 repetition with hl7apy's own datatype class for the OBX-2 type |
| NM as HL7 defines it | too lenient: `Decimal` accepts `NaN`, `1e3`, `1_000`, spaces, non-ASCII digits | our `NM_PATTERN`, applied after hl7apy's check |
| Caller text as a value | only when given a datatype object; a plain string is parsed as ER7 | always passes datatype objects |
| A value containing an escape sequence such as `\F\` | passed through unescaped | the builder refuses a backslash |
| Line-feed separated messages | misleading `UnsupportedVersion` error | pre-check with a clear message |
| Empty MSH-9 at STRICT level | "Cannot instantiate an unknown Element" | checks the message structure first (with hl7apy's `get_message_info`) |
| Store's ACK with MSH-9 = `ACK` only | STRICT: refused; TOLERANT: parsed | reads ACKs at TOLERANT; the message code is MSH-9's first component as hl7apy parses it (`MSG_1` from 2.5, `CM_MSG_1` in 2.3) |
| Store's NACK with blank MSH fields and empty MSA-2 | TOLERANT: parsed | no fallback parser needed |
| ACK with no trailing `\r` (HTTPS ingest, S5) | parsed | tested |
| MSH-12 `9.9` (message or ACK), or empty (message) | `UnsupportedVersion`. An ACK with an empty MSH-12 is read with hl7apy's default version | ACK: re-read with hl7apy's `parse_segments` at version 2.5.1. Message: `HL7ParseError` for the mapper to record |
| Unescape `\E\`, `\F\` ... when reading | no | `decode_escapes`, about ten lines |

## Tests

`make test` runs `libs/pulsebridge_hl7v2/tests`. The ACK tests use the raw store replies recorded
in `docs/spikes/s4.md` and `s5.md`; the MLLP reader tests use a loopback TCP socket on
`127.0.0.1` inside the test process. No network or GCP access.
