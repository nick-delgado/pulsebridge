"""Write example resources for the HL7 FHIR validator (task T08).

    python -m pulsebridge_fhir.examples [OUTPUT_DIR]

Every value here is synthetic: a made-up patient, made-up readings, a made-up message name.
The default output directory is ``libs/pulsebridge_fhir/examples``. A test checks the committed
files equal what this module produces, so they cannot drift from the code.
"""

import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from pulsebridge_fhir.audit_event import build_send_attempt_audit_event
from pulsebridge_fhir.bundle import blood_pressure_transaction, patient_setup_transaction
from pulsebridge_fhir.google_health import (
    GoogleDataSource,
    heart_rate_transaction,
    parse_google_utc_offset,
    rollup_source,
    window_from_rollup,
)
from pulsebridge_fhir.inputs import (
    Acknowledged,
    BloodPressureReading,
    DeviceInput,
    NotAcknowledged,
    PatientInput,
    SendAttempt,
    SourceInfo,
)
from pulsebridge_fhir.resources import (
    build_blood_pressure_observation,
    build_device,
    build_heart_rate_observation,
    build_patient,
    build_provenance,
)

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "examples"

# The full record, written once by the setup step (connect script T11 or seeder T09).
SYNTHETIC_PATIENT = PatientInput(
    patient_id="PB-SYN-0001",
    family="Example",
    given=("Avery",),
    gender="female",
    birth_date=date(1980, 4, 12),
    google_health_user_id="synthetic-health-user-0001",
)
# What health-sync knows: only the identifier, from configuration.
SYNTHETIC_PATIENT_REF = PatientInput(patient_id="PB-SYN-0001")
# What v2-to-fhir knows from the cuff message: PID-3 (identifier) and PID-5 (name).
SYNTHETIC_PATIENT_FROM_PID = PatientInput(
    patient_id="PB-SYN-0001", family="Example", given=("Avery",)
)
SYNTHETIC_WATCH = DeviceInput(display_name="Pixel Watch 4")
SYNTHETIC_CUFF = DeviceInput(display_name="PulseBridge cuff simulator")

# Two consecutive five-minute roll-up windows in Google's JSON shape (spike S3), made-up values.
SYNTHETIC_ROLLUP = [
    {
        "startTime": "2026-01-01T14:05:00Z",
        "endTime": "2026-01-01T14:10:00Z",
        "heartRate": {"beatsPerMinuteMin": 64, "beatsPerMinuteAvg": 68.2, "beatsPerMinuteMax": 75},
    },
    {
        "startTime": "2026-01-01T14:10:00Z",
        "endTime": "2026-01-01T14:15:00Z",
        "heartRate": {"beatsPerMinuteMin": 66, "beatsPerMinuteAvg": 71.6, "beatsPerMinuteMax": 82},
    },
]
SYNTHETIC_TZ = parse_google_utc_offset("-18000s")  # UTC-05:00; no DST change in these windows
# As stated by a listed data point (spike S3); the roll-up response itself has no dataSource.
SYNTHETIC_DATA_SOURCE = GoogleDataSource(platform="FITBIT", recording_method="PASSIVELY_MEASURED")
SYNTHETIC_RECORDED = datetime(2026, 1, 1, 14, 20, 3, tzinfo=UTC)
# The start of the overlap the next sync re-requests: the second window is written preliminary.
SYNTHETIC_COMPLETE_BEFORE = datetime(2026, 1, 1, 14, 10, tzinfo=UTC)

SYNTHETIC_BP = BloodPressureReading(
    effective=datetime.fromisoformat("2026-01-01T09:30:00-05:00"),
    systolic=121,
    diastolic=79,
    source_key="PULSEBRIDGE_CUFF/MSG-SYN-0001",
)
SYNTHETIC_BP_SOURCE = SourceInfo(
    record_type="hl7v2-message",
    record_id="hl7V2Stores/example-store/messages/synthetic-message-0001",
    platform="HL7V2_STORE",
    # The ORU^R01 message does not say how the reading was taken, so nothing is asserted.
    recording_method=None,
)

# Cuff send attempts (T31). A made-up store name; the project ID is a placeholder.
SYNTHETIC_HL7V2_STORE = (
    "projects/example-project/locations/us-central1/datasets/example-dataset/"
    "hl7V2Stores/example-store"
)
SYNTHETIC_SEND_ACCEPTED = SendAttempt(
    sent_at=datetime(2026, 1, 1, 14, 30, 0, 125000, tzinfo=UTC),
    transport="https",
    control_id="MSG-SYN-0002",
    result=Acknowledged(
        code="AA",
        # The shape of the store's ACK (spike S5): message type ACK, MSA-2 echoes MSH-10.
        raw_ack=b"MSH|^~\\&|PHR|PULSEBRIDGE|CUFFSIM|PULSEBRIDGE|20260101143000||ACK|ACK-SYN-0002"
        b"|P|2.5.1\rMSA|AA|MSG-SYN-0002",
    ),
    patient_id="PB-SYN-0001",
    agent="cuff-sim",
    destination=SYNTHETIC_HL7V2_STORE,
    message_name=SYNTHETIC_HL7V2_STORE + "/messages/synthetic-message-0002",
)
SYNTHETIC_SEND_REJECTED = SendAttempt(
    sent_at=datetime(2026, 1, 1, 14, 31, 0, tzinfo=UTC),
    transport="mllp",
    # The "no MSH segment" sample has no MSH-10 to report.
    control_id=None,
    result=Acknowledged(
        code="AE",
        # The store's NACK for a message with no MSH (spike S4): empty MSA-2, reason in MSA-3.
        raw_ack=b"MSH|^~\\&|||||20260101143100||ACK|ACK-SYN-0003\rMSA|AE||Invalid MSH header",
        reason="Invalid MSH header",
    ),
    patient_id="PB-SYN-0001",
    agent="cuff-sim",
    destination=SYNTHETIC_HL7V2_STORE,
    sample_name="unparseable_no_msh",
)
SYNTHETIC_SEND_NO_ACK = SendAttempt(
    sent_at=datetime(2026, 1, 1, 14, 32, 0, tzinfo=UTC),
    transport="mllp",
    control_id="MSG-SYN-0004",
    # A frame with no end block: the adapter waits for more bytes and never answers (spike S4).
    result=NotAcknowledged(why="timeout"),
    patient_id="PB-SYN-0001",
    agent="cuff-sim",
    destination=SYNTHETIC_HL7V2_STORE,
    sample_name="noack_missing_end_block",
)
SYNTHETIC_SEND_UNREADABLE_REPLY = SendAttempt(
    sent_at=datetime(2026, 1, 1, 14, 33, 0, tzinfo=UTC),
    transport="mllp",
    control_id="MSG-SYN-0005",
    # A reply the ACK parser refuses: it starts with a UTF-8 byte order mark, not with MSH.
    result=NotAcknowledged(
        why="unreadable-reply",
        reply_prefix=b"\xef\xbb\xbfMSH|^~\\&|||||20260101143300||ACK|ACK-SYN-0005\rMSA|AA|MSG-SYN-0005",
    ),
    patient_id="PB-SYN-0001",
    agent="cuff-sim",
    destination=SYNTHETIC_HL7V2_STORE,
)


def build_examples() -> dict[str, dict]:
    """File name to resource, for every example."""
    patient = build_patient(SYNTHETIC_PATIENT)
    watch = build_device(SYNTHETIC_WATCH, patient=patient)
    cuff = build_device(SYNTHETIC_CUFF, patient=patient)
    window = window_from_rollup(SYNTHETIC_ROLLUP[0])
    heart_rate = build_heart_rate_observation(
        window, patient=patient, device=watch, tz=SYNTHETIC_TZ
    )
    hr_provenance = build_provenance(
        target=heart_rate,
        device=watch,
        source=rollup_source(window, SYNTHETIC_DATA_SOURCE),
        recorded=SYNTHETIC_RECORDED,
        assembler="PulseBridge health-sync",
    )
    blood_pressure = build_blood_pressure_observation(SYNTHETIC_BP, patient=patient, device=cuff)
    bp_provenance = build_provenance(
        target=blood_pressure,
        device=cuff,
        source=SYNTHETIC_BP_SOURCE,
        recorded=SYNTHETIC_RECORDED,
        assembler="PulseBridge v2-to-fhir",
    )
    sends = {
        name: build_send_attempt_audit_event(
            attempt, recorded=attempt.sent_at + timedelta(seconds=seconds)
        ).resource
        for name, attempt, seconds in (
            ("auditevent-send-accepted.json", SYNTHETIC_SEND_ACCEPTED, 1),
            ("auditevent-send-rejected.json", SYNTHETIC_SEND_REJECTED, 1),
            # recorded after the simulator's read timeout
            ("auditevent-send-no-ack.json", SYNTHETIC_SEND_NO_ACK, 5),
            ("auditevent-send-unreadable-reply.json", SYNTHETIC_SEND_UNREADABLE_REPLY, 1),
        )
    }
    return sends | {
        "patient.json": patient.resource,
        "device-watch.json": watch.resource,
        "observation-heart-rate.json": heart_rate.resource,
        "observation-blood-pressure.json": blood_pressure.resource,
        "provenance-heart-rate.json": hr_provenance.resource,
        "provenance-blood-pressure.json": bp_provenance.resource,
        "bundle-patient-setup.json": patient_setup_transaction(
            SYNTHETIC_PATIENT, devices=[SYNTHETIC_WATCH]
        ),
        "bundle-heart-rate-ingest.json": heart_rate_transaction(
            SYNTHETIC_ROLLUP,
            patient=SYNTHETIC_PATIENT_REF,
            device=SYNTHETIC_WATCH,
            data_source=SYNTHETIC_DATA_SOURCE,
            tz=SYNTHETIC_TZ,
            recorded=SYNTHETIC_RECORDED,
            complete_before=SYNTHETIC_COMPLETE_BEFORE,
        ),
        "bundle-blood-pressure-ingest.json": blood_pressure_transaction(
            SYNTHETIC_BP,
            patient=SYNTHETIC_PATIENT_FROM_PID,
            device=SYNTHETIC_CUFF,
            source=SYNTHETIC_BP_SOURCE,
            recorded=SYNTHETIC_RECORDED,
        ),
    }


def render(resource: dict) -> str:
    return json.dumps(resource, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    out_dir = Path(argv[1]) if len(argv) > 1 else DEFAULT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*.json"):
        if stale.name not in build_examples():
            stale.unlink()
            print(f"removed {stale}")
    for name, resource in build_examples().items():
        (out_dir / name).write_text(render(resource), encoding="utf-8")
        print(f"wrote {out_dir / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
