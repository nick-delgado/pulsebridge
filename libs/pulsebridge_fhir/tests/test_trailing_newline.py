"""Validation patterns must match the whole value (review of T31, minor 2).

``re.match(r"^...$", value)`` also accepts ``value + "\\n"``, because ``$`` matches before a final
newline. Every pattern in the package now uses ``fullmatch``. These cases were accepted before.
"""

import pytest
from pulsebridge_fhir import FhirInputError, SourceInfo, Write, request_for
from pulsebridge_fhir.examples import SYNTHETIC_ROLLUP
from pulsebridge_fhir.google_health import parse_google_utc_offset, window_from_rollup


def test_provenance_and_audit_event_id_with_a_newline_is_refused():
    # Before, this gave the URL "Provenance/abc\n".
    for rt in ("Provenance", "AuditEvent"):
        with pytest.raises(FhirInputError, match="is not a valid FHIR id"):
            request_for({"resourceType": rt, "id": "abc\n"}, Write.UPDATE)
    assert request_for({"resourceType": "Provenance", "id": "abc"}, Write.UPDATE) == {
        "method": "PUT",
        "url": "Provenance/abc",
    }


def test_google_offset_with_a_newline_is_refused():
    with pytest.raises(FhirInputError):
        parse_google_utc_offset("-18000s\n")
    assert parse_google_utc_offset("-18000s").utcoffset(None).total_seconds() == -18000


def test_google_decimal_string_with_a_newline_is_refused():
    point = dict(SYNTHETIC_ROLLUP[0])
    point["heartRate"] = dict(point["heartRate"], beatsPerMinuteAvg="68.2\n")
    with pytest.raises(FhirInputError):
        window_from_rollup(point)
    point["heartRate"]["beatsPerMinuteAvg"] = "68.2"
    assert window_from_rollup(point).bpm_avg == 68.2


def test_google_time_with_a_newline_is_refused():
    point = dict(SYNTHETIC_ROLLUP[0], startTime="2026-01-01T14:05:00Z\n")
    with pytest.raises(FhirInputError):
        window_from_rollup(point)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"record_type": "hl7v2-message\n", "platform": None},
        {"record_type": "hl7v2-message", "platform": "FITBIT\n"},
    ],
)
def test_source_info_with_a_newline_is_refused(kwargs):
    with pytest.raises(FhirInputError):
        SourceInfo(record_id="x", recording_method=None, **kwargs)
