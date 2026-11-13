"""Write example resources for the HL7 FHIR validator (task T08).

    python -m pulsebridge_fhir.examples [OUTPUT_DIR]

Every value here is synthetic: a made-up patient, made-up readings, a made-up message name.
The default output directory is ``libs/pulsebridge_fhir/examples``. A test checks the committed
files equal what this module produces, so they cannot drift from the code.
"""

import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from pulsebridge_fhir.bundle import build_transaction_bundle
from pulsebridge_fhir.google_health import (
    GoogleDataSource,
    heart_rate_transaction,
    parse_google_utc_offset,
    rollup_source,
    window_from_rollup,
)
from pulsebridge_fhir.inputs import BloodPressureReading, DeviceInput, PatientInput, SourceInfo
from pulsebridge_fhir.resources import (
    build_blood_pressure_observation,
    build_device,
    build_heart_rate_observation,
    build_patient,
    build_provenance,
)

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "examples"

SYNTHETIC_PATIENT = PatientInput(
    patient_id="PB-SYN-0001",
    family="Example",
    given=("Avery",),
    gender="female",
    birth_date=date(1980, 4, 12),
    google_health_user_id="synthetic-health-user-0001",
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
SYNTHETIC_TZ = parse_google_utc_offset("-18000s")  # UTC-05:00
SYNTHETIC_RECORDED = datetime(2026, 1, 1, 14, 20, 3, tzinfo=UTC)

SYNTHETIC_BP = BloodPressureReading(
    effective=datetime.fromisoformat("2026-01-01T09:30:00-05:00"),
    systolic=121,
    diastolic=79,
    source_key="PULSEBRIDGE_CUFF/MSG-SYN-0001",
)
SYNTHETIC_BP_SOURCE = SourceInfo(
    platform="HL7V2_STORE",
    recording_method="ACTIVELY_MEASURED",
    record_type="hl7v2-message",
    record_id="hl7V2Stores/example-store/messages/synthetic-message-0001",
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
        source=rollup_source(window, GoogleDataSource()),
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
    return {
        "patient.json": patient.resource,
        "device-watch.json": watch.resource,
        "observation-heart-rate.json": heart_rate.resource,
        "observation-blood-pressure.json": blood_pressure.resource,
        "provenance-heart-rate.json": hr_provenance.resource,
        "bundle-heart-rate-transaction.json": heart_rate_transaction(
            SYNTHETIC_ROLLUP,
            patient=SYNTHETIC_PATIENT,
            device=SYNTHETIC_WATCH,
            data_source=GoogleDataSource(),
            tz=SYNTHETIC_TZ,
            recorded=SYNTHETIC_RECORDED,
        ),
        "bundle-blood-pressure-transaction.json": build_transaction_bundle(
            [patient, cuff, blood_pressure, bp_provenance]
        ),
    }


def render(resource: dict) -> str:
    return json.dumps(resource, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    out_dir = Path(argv[1]) if len(argv) > 1 else DEFAULT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, resource in build_examples().items():
        (out_dir / name).write_text(render(resource), encoding="utf-8")
        print(f"wrote {out_dir / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
