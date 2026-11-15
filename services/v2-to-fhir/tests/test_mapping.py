"""The v2-to-FHIR mapping, checked against literal expectations.

Two kinds of input:
- T07's shared samples, loaded by name through ``pulsebridge_hl7v2.samples`` (never copied);
- small variants written out below, each changing one field of an otherwise valid message.

Every expected value (codes, units, times, references, reason codes) is written out literally.
All data is synthetic.
"""

import logging
from datetime import UTC, datetime

import pytest
from pulsebridge_fhir import FhirInputError
from pulsebridge_hl7v2.samples import all_samples, by_class, load
from v2_to_fhir import mapping
from v2_to_fhir.mapping import MappingConfig, map_message
from v2_to_fhir.reasons import Outcome, Reason

NAME = "projects/demo-project/locations/us-central1/datasets/demo/hl7V2Stores/main/messages/m-1="
CONFIG = MappingConfig(patient_id="SYN-000123")
RECORDED = datetime(2026, 11, 13, 21, 0, 5, tzinfo=UTC)


def run(raw: bytes | str, config: MappingConfig = CONFIG):
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return map_message(raw, message_name=NAME, config=config, recorded=RECORDED)


# ---- message builder for variants -------------------------------------------------------------

MSH = (
    "MSH|^~\\&|CUFFSIM|PULSEBRIDGE|PHR|PULSEBRIDGE|20261113205329+0000||ORU^R01^ORU_R01"
    "|SYN-T12-0001|P|2.5.1"
)
PID = "PID|1||SYN-000123^^^PULSEBRIDGE^MR||TESTPATIENT^SYNTHIA"
OBR = (
    "OBR|1||SYN-T12-0001^CUFFSIM|85354-9^Blood pressure panel with all children optional^LN"
    "|||20261113205329+0000"
)
WHEN = "20261113205329+0000"


def obx(
    n: int,
    code: str,
    value: str,
    *,
    unit: str = "mm[Hg]^mm[Hg]^UCUM",
    when: str = WHEN,
    value_type: str = "NM",
    status: str = "F",
    system: str = "LN",
) -> str:
    # OBX|1|2 value type|3 code^text^system|4|5 value|6 units|7|8|9|10|11 status|12|13|14 time
    return f"OBX|{n}|{value_type}|{code}^Reading^{system}||{value}|{unit}|||||{status}|||{when}"


SYS = obx(1, "8480-6", "120")
DIA = obx(2, "8462-4", "80")


def message(*, msh=MSH, pid=PID, obr=OBR, observations=(SYS, DIA)) -> str:
    segments = [msh] + ([pid] if pid else []) + [obr] + list(observations)
    return "\r".join(segments) + "\r"


def entries(result) -> dict:
    """Bundle entries by resource type (each type occurs once in an ingest Bundle)."""
    assert result.outcome is Outcome.ACCEPTED, result.reason
    return {e["resource"]["resourceType"]: e for e in result.bundle["entry"]}


def observation(result) -> dict:
    return entries(result)["Observation"]["resource"]


def components(result) -> dict:
    """component code -> valueQuantity."""
    return {
        c["code"]["coding"][0]["code"]: c["valueQuantity"] for c in observation(result)["component"]
    }


# ---- T07 samples, every one, by name ----------------------------------------------------------

SAMPLE_OUTCOMES = [
    ("valid_bp_reading", "accepted", "mapped"),
    ("valid_bp_reading_offset", "accepted", "mapped"),
    ("invalid_missing_pid3", "error", "missing-patient-identifier"),
    ("invalid_obx14_not_hl7_date", "error", "invalid-obx-14"),
    # Unparseable: the HL7v2 store rejects these with AE, so they never reach the mapper. If they
    # ever did, they must still end as an error, not an exception.
    ("unparseable_no_msh", "error", "unparseable-by-mapper"),
    ("unparseable_lf_terminators", "error", "unparseable-by-mapper"),
    ("unparseable_crlf_terminators", "error", "unparseable-by-mapper"),
    ("unparseable_separator_mismatch", "error", "unparseable-by-mapper"),
    # No acknowledgment: only the MLLP framing is broken; the message itself is a valid reading.
    ("noack_missing_end_block", "accepted", "mapped"),
    ("noack_missing_final_cr", "accepted", "mapped"),
    ("unmappable_unknown_obx3_code", "error", "unknown-obx-3-code"),
    ("unmappable_missing_obx14", "error", "missing-obx-14"),
    ("unmappable_non_numeric_obx5", "error", "non-numeric-obx-5"),
    ("unmappable_msh_only", "error", "no-observations"),
    ("unmappable_empty_msh9", "error", "unsupported-message-type"),
    ("unmappable_bare_msh", "error", "unsupported-message-type"),
    # T07 suggested unparseable-by-mapper; hl7apy's UnsupportedVersion lets us say exactly why.
    ("unmappable_unknown_version", "error", "unsupported-version"),
    ("unmappable_unknown_msh9", "error", "unsupported-message-type"),
    ("unmappable_empty_msh12", "error", "unsupported-version"),
]


@pytest.mark.parametrize(("name", "outcome", "reason"), SAMPLE_OUTCOMES)
def test_every_t07_sample_gets_its_outcome_and_reason(name, outcome, reason):
    result = run(load(name).payload)
    assert (result.outcome.value, result.reason.value) == (outcome, reason)
    assert (result.bundle is not None) == (outcome == "accepted")


def test_the_table_covers_every_sample_and_every_class():
    assert {name for name, _, _ in SAMPLE_OUTCOMES} == {s.name for s in all_samples()}
    assert by_class("parses_but_cannot_map")  # at least one per malformed class exists
    assert by_class("unparseable") and by_class("structurally_invalid")


@pytest.mark.parametrize("sample", [s.name for s in all_samples()])
def test_mapper_agrees_with_the_t07_manifest_for_every_sample(sample):
    # The manifest and the mapper cannot drift apart: an "error" sample's mapping_reason is
    # the mapper's reason; an "accepted" one has none (the mapper says "mapped"); a sample the
    # store rejects (outcome null) is not expected to reach the mapper.
    expected = load(sample).expected
    result = run(load(sample).payload)
    if expected.application_outcome == "error":
        assert (result.outcome.value, result.reason.value) == ("error", expected.mapping_reason)
    elif expected.application_outcome == "accepted":
        assert expected.mapping_reason is None
        assert result.reason is Reason.MAPPED
    else:
        assert expected.application_outcome is None and expected.mapping_reason is None


# ---- the valid sample, element by element -----------------------------------------------------


def test_valid_sample_observation():
    result = run(load("valid_bp_reading").payload)
    obs = observation(result)
    # Observation.code: the blood-pressure panel, LOINC 85354-9 (the R4 bp profile fixes it).
    assert obs["code"]["coding"] == [
        {
            "system": "http://loinc.org",
            "code": "85354-9",
            "display": "Blood pressure panel with all children optional",
        }
    ]
    assert obs["status"] == "final"  # OBX-11 F
    assert obs["category"][0]["coding"][0]["code"] == "vital-signs"
    assert obs["category"][0]["coding"][0]["system"] == (
        "http://terminology.hl7.org/CodeSystem/observation-category"
    )
    # OBX-14 20261113205329+0000, offset kept.
    assert obs["effectiveDateTime"] == "2026-11-13T20:53:29+00:00"
    # OBX-3 -> component.code, OBX-5 -> value, OBX-6 -> UCUM unit.
    assert obs["component"] == [
        {
            "code": {
                "coding": [
                    {
                        "system": "http://loinc.org",
                        "code": "8480-6",
                        "display": "Systolic blood pressure",
                    }
                ],
                "text": "Systolic",
            },
            "valueQuantity": {
                "value": 120,
                "unit": "mmHg",
                "system": "http://unitsofmeasure.org",
                "code": "mm[Hg]",
            },
        },
        {
            "code": {
                "coding": [
                    {
                        "system": "http://loinc.org",
                        "code": "8462-4",
                        "display": "Diastolic blood pressure",
                    }
                ],
                "text": "Diastolic",
            },
            "valueQuantity": {
                "value": 80,
                "unit": "mmHg",
                "system": "http://unitsofmeasure.org",
                "code": "mm[Hg]",
            },
        },
    ]
    assert "valueQuantity" not in obs  # a panel has components, no top-level value


def test_valid_sample_patient_and_device_are_conditional_creates_and_referenced():
    by_type = entries(run(load("valid_bp_reading").payload))
    patient, device, obs = by_type["Patient"], by_type["Device"], by_type["Observation"]

    # PID-3.1 -> Patient.identifier; PID-5 -> Patient.name. POST + ifNoneExist: create only if
    # absent (D11), so the full record written by the setup step is never overwritten.
    assert patient["resource"]["identifier"][0]["system"] == "urn:pulsebridge:patient"
    assert patient["resource"]["identifier"][0]["value"] == "SYN-000123"
    assert patient["resource"]["name"] == [
        {"use": "official", "family": "TESTPATIENT", "given": ["SYNTHIA"]}
    ]
    assert patient["request"] == {
        "method": "POST",
        "url": "Patient",
        "ifNoneExist": "identifier=urn:pulsebridge:patient|SYN-000123",
    }
    # MSH-3 at MSH-4 -> Device.deviceName.
    assert device["resource"]["deviceName"] == [
        {"name": "CUFFSIM (PULSEBRIDGE)", "type": "user-friendly-name"}
    ]
    assert device["request"]["method"] == "POST"
    assert device["request"]["ifNoneExist"].startswith("identifier=urn:pulsebridge:device|")
    # The Observation points at both through their bundle-local fullUrls.
    assert obs["resource"]["subject"] == {"reference": patient["fullUrl"]}
    assert obs["resource"]["device"] == {"reference": device["fullUrl"]}
    assert device["resource"]["patient"] == {"reference": patient["fullUrl"]}


def test_valid_sample_observation_is_a_conditional_update_on_a_pinned_key():
    obs = entries(run(load("valid_bp_reading").payload))["Observation"]
    # Pinned: UUIDv5 (namespace uuid5(URL, "urn:pulsebridge")) of the JSON list
    # ["Observation", "SYN-000123", "85354-9", '["CUFFSIM","PULSEBRIDGE","SYN-VALID-0001"]'],
    # computed by hand from T06's documented recipe, not copied from the output. If it changes,
    # every stored Observation becomes unreachable and redeliveries create duplicates.
    key = "a704437f-1123-5b1d-9fed-3d05be2c2e36"
    assert obs["resource"]["identifier"] == [
        {"use": "official", "system": "urn:pulsebridge:observation", "value": key}
    ]
    assert obs["request"] == {
        "method": "PUT",
        "url": f"Observation?identifier=urn:pulsebridge:observation|{key}",
    }


def test_valid_sample_provenance_points_back_to_the_stored_message():
    by_type = entries(run(load("valid_bp_reading").payload))
    prov = by_type["Provenance"]["resource"]
    assert prov["target"] == [{"reference": by_type["Observation"]["fullUrl"]}]
    assert prov["recorded"] == "2026-11-13T21:00:05+00:00"
    assert prov["entity"] == [
        {
            "role": "source",
            "what": {
                "identifier": {
                    "system": "urn:pulsebridge:source-record:hl7v2-message",
                    "value": NAME,
                },
                "display": f"hl7v2-message {NAME}",
            },
        }
    ]
    custodian = [a for a in prov["agent"] if a["type"]["coding"][0]["code"] == "custodian"]
    assert custodian[0]["who"]["identifier"]["value"] == "HL7V2_STORE"
    assert by_type["Provenance"]["request"]["method"] == "PUT"


def test_offset_sample_keeps_its_own_offset():
    result = run(load("valid_bp_reading_offset").payload)
    assert observation(result)["effectiveDateTime"] == "2026-11-13T15:53:29-05:00"
    assert components(result)["8480-6"]["value"] == 135
    assert components(result)["8462-4"]["value"] == 85


def test_control_id_is_reported_for_logs():
    assert run(load("valid_bp_reading").payload).control_id == "SYN-VALID-0001"
    assert run(load("unmappable_missing_obx14").payload).control_id == "SYN-MAP-0001"


# ---- idempotency ------------------------------------------------------------------------------


def observation_key(raw: str) -> str:
    return observation(run(raw))["identifier"][0]["value"]


def test_resend_with_a_new_msh7_maps_to_the_same_observation():
    # Spike S5: the store deduplicates on bytes, so a resend with a new MSH-7 is a new stored
    # message, and a new notification, with the same control ID.
    resent = MSH.replace("20261113205329+0000", "20261113210000+0000")
    assert observation_key(message(msh=resent)) == observation_key(message())


def test_a_different_control_id_or_sender_is_a_different_observation():
    base = observation_key(message())
    assert observation_key(message(msh=MSH.replace("SYN-T12-0001", "SYN-T12-0002"))) != base
    assert observation_key(message(msh=MSH.replace("|PULSEBRIDGE|PHR", "|OTHERFAC|PHR"))) != base


def test_mapping_the_same_bytes_twice_gives_the_same_bundle():
    assert run(message()).bundle == run(message()).bundle


# ---- MSH --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("msh", "reason"),
    [
        (MSH.replace("ORU^R01^ORU_R01", "ORU^R30^ORU_R30"), "unsupported-message-type"),
        (MSH.replace("ORU^R01^ORU_R01", "ORM^O01"), "unsupported-message-type"),
        (MSH.replace("|2.5.1", "|2.4"), "unsupported-version"),
        (MSH.replace("|2.5.1", "|2.6"), "unsupported-version"),
        (MSH.replace("|SYN-T12-0001|", "||"), "missing-control-id"),
        (MSH.replace("|CUFFSIM|", "||"), "missing-sending-application"),
        (MSH.replace("|CUFFSIM|", '|""|'), "missing-sending-application"),  # HL7 null
        (MSH.replace("|SYN-T12-0001|", '|""|'), "missing-control-id"),
        (MSH.replace("SYN-T12-0001", "C" * 200), "control-id-too-long"),
    ],
)
def test_header_problems(msh, reason):
    result = run(message(msh=msh))
    assert (result.outcome, result.reason.value) == (Outcome.ERROR, reason)
    assert result.bundle is None


@pytest.mark.parametrize("facility", ["", '""'])
def test_empty_or_null_msh4_names_the_device_by_application_only(facility):
    result = run(message(msh=MSH.replace("|CUFFSIM|PULSEBRIDGE|", f"|CUFFSIM|{facility}|")))
    assert entries(result)["Device"]["resource"]["deviceName"][0]["name"] == "CUFFSIM"


def test_control_id_of_199_characters_is_accepted():
    result = run(message(msh=MSH.replace("SYN-T12-0001", "C" * 199)))
    assert result.outcome is Outcome.ACCEPTED
    assert result.control_id == "C" * 199


def test_device_name_is_escape_decoded_but_the_key_uses_the_raw_text():
    escaped = MSH.replace("|CUFFSIM|PULSEBRIDGE|", "|CU\\F\\FF|PULSE\\T\\BRIDGE|")
    result = run(message(msh=escaped))
    assert entries(result)["Device"]["resource"]["deviceName"][0]["name"] == "CU|FF (PULSE&BRIDGE)"
    # The key is built from the field text as sent, escapes and all, as a JSON list.
    header = mapping._read_header(mapping._parse(message(msh=escaped).encode()))
    assert header.source_key == '["CU\\\\F\\\\FF","PULSE\\\\T\\\\BRIDGE","SYN-T12-0001"]'


@pytest.mark.parametrize("bad", ["\x00", "\x01", "\x1b", "\x7f"])
def test_control_characters_in_sending_application_are_refused(bad):
    result = run(message(msh=MSH.replace("|CUFFSIM|", f"|CUFF{bad}SIM|")))
    assert result.reason is Reason.INVALID_SENDING_APPLICATION


def test_key_parts_cannot_collide_when_msh1_declares_another_separator():
    # With MSH-1 "#", a literal "|" is ordinary text inside a field. MSH-3 "A|B" + MSH-4 "C" and
    # MSH-3 "A" + MSH-4 "B|C" are different senders and must give different keys.
    def with_hash_separator(application: str, facility: str) -> str:
        msh = (
            f"MSH#^~\\&#{application}#{facility}#PHR#PULSEBRIDGE#20261113205329+0000##"
            "ORU^R01^ORU_R01#SYN-T12-0001#P#2.5.1"
        )
        rest = [segment.replace("|", "#") for segment in (PID, OBR, SYS, DIA)]
        return "\r".join([msh, *rest]) + "\r"

    first = run(with_hash_separator("A|B", "C"))
    second = run(with_hash_separator("A", "B|C"))
    assert first.outcome is second.outcome is Outcome.ACCEPTED
    assert observation(first)["identifier"] != observation(second)["identifier"]


# ---- PID --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pid", "reason"),
    [
        ("PID|1||SYN-999999^^^PULSEBRIDGE^MR||TESTPATIENT^SYNTHIA", "unknown-patient"),
        ("PID|1||^^^PULSEBRIDGE^MR||TESTPATIENT^SYNTHIA", "missing-patient-identifier"),
        ('PID|1||""^^^PULSEBRIDGE^MR||TESTPATIENT^SYNTHIA', "missing-patient-identifier"),
        ("PID|1||SYN-000123^^^PULSEBRIDGE^MR||TEST\x00PATIENT^SYNTHIA", "invalid-patient-name"),
        ("PID|1||SYN-000123^^^PULSEBRIDGE^MR||TESTPATIENT^SYN\x1bTHIA", "invalid-patient-name"),
        (None, "missing-patient-identifier"),
        (f"{PID}\rPID|2||SYN-000123", "multiple-patients"),
    ],
)
def test_patient_problems(pid, reason):
    result = run(message(pid=pid))
    assert (result.outcome, result.reason.value) == (Outcome.ERROR, reason)


def test_the_configured_id_may_be_any_pid3_repetition():
    pid = "PID|1||OTHER-1^^^ELSEWHERE^PI~SYN-000123^^^PULSEBRIDGE^MR||TESTPATIENT^SYNTHIA"
    patient = entries(run(message(pid=pid)))["Patient"]["resource"]
    assert [i["value"] for i in patient["identifier"]] == ["SYN-000123"]  # PID-3.1 only


def test_assigning_authority_when_configured():
    strict = MappingConfig(patient_id="SYN-000123", patient_assigning_authority="PULSEBRIDGE")
    assert run(message(), strict).outcome is Outcome.ACCEPTED
    other = "PID|1||SYN-000123^^^OTHERSYS^MR||TESTPATIENT^SYNTHIA"
    assert run(message(pid=other), strict).reason is Reason.UNKNOWN_PATIENT
    assert run(message(pid=other), CONFIG).outcome is Outcome.ACCEPTED  # not configured: ignored


@pytest.mark.parametrize(
    ("pid5", "expected_name"),
    [
        ("", None),
        ('""', None),  # the HL7 null for the whole field
        ('""^""', None),  # and for each part
        ('TESTPATIENT^""', [{"use": "official", "family": "TESTPATIENT"}]),
        ('""^SYNTHIA', [{"use": "official", "given": ["SYNTHIA"]}]),
        ("TESTPATIENT", [{"use": "official", "family": "TESTPATIENT"}]),
        ("^SYNTHIA", [{"use": "official", "given": ["SYNTHIA"]}]),
        ("O\\T\\BRIEN^SYNTHIA", [{"use": "official", "family": "O&BRIEN", "given": ["SYNTHIA"]}]),
    ],
)
def test_an_empty_or_partial_name_does_not_fail_the_message(pid5, expected_name):
    pid = f"PID|1||SYN-000123^^^PULSEBRIDGE^MR||{pid5}"
    patient = entries(run(message(pid=pid)))["Patient"]["resource"]
    assert patient.get("name") == expected_name


# ---- OBX: codes and structure ------------------------------------------------------------------


# ---- OBR: the order -----------------------------------------------------------------------


OBR_2 = OBR.replace("OBR|1|", "OBR|2|")


@pytest.mark.parametrize(
    ("segments", "reason"),
    [
        ([MSH, PID, SYS, DIA], "missing-obr"),
        ([MSH, PID, SYS, OBR, DIA], "missing-obr"),  # an OBX before its OBR
        ([MSH, PID, SYS, DIA, OBR], "missing-obr"),  # OBR after its OBX
        ([MSH, SYS, DIA], "missing-patient-identifier"),  # OBX only: the PID check comes first
        ([MSH, PID, OBR, SYS, OBR_2, DIA], "multiple-orders"),
        ([MSH, PID, OBR, SYS, DIA, OBR_2], "multiple-orders"),
        ([MSH, PID, OBR, OBR_2, SYS, DIA], "multiple-orders"),  # two OBR in a row
        ([MSH, PID, SYS, OBR, OBR_2, DIA], "multiple-orders"),
        ([MSH, SYS, PID, OBR, DIA], "multiple-orders"),  # an OBX before the PID: its own order
        ([MSH, PID, OBR, SYS, "ZZZ|1", DIA], "multiple-orders"),  # a Z segment splits the order
        ([MSH, PID, OBR.replace("85354-9", "55284-4"), SYS, DIA], "unknown-obr-4-code"),
        ([MSH, PID, OBR.replace("^LN|", "^L|"), SYS, DIA], "unknown-obr-4-code"),
        (
            [MSH, PID, "OBR|1||SYN-T12-0001^CUFFSIM||||20261113205329+0000", SYS, DIA],
            "unknown-obr-4-code",
        ),
    ],
)
def test_order_structure_problems(segments, reason):
    result = run("\r".join(segments) + "\r")
    assert (result.outcome, result.reason.value) == (Outcome.ERROR, reason)
    assert result.bundle is None


def test_every_valid_t07_sample_still_has_the_order_structure_we_require():
    for sample in by_class("valid"):
        assert run(sample.payload).outcome is Outcome.ACCEPTED, sample.name


def test_swapped_obx_order_is_read_by_code_not_position():
    result = run(message(observations=(obx(1, "8462-4", "80"), obx(2, "8480-6", "120"))))
    assert components(result)["8480-6"]["value"] == 120
    assert components(result)["8462-4"]["value"] == 80


@pytest.mark.parametrize(
    ("observations", "reason"),
    [
        ((SYS, obx(2, "8480-6", "121"), DIA), "duplicate-obx-3-code"),
        ((SYS, DIA, obx(3, "8867-4", "72", unit="/min^/min^UCUM")), "unknown-obx-3-code"),
        ((SYS, obx(2, "8462-4", "80", system="L")), "unknown-obx-3-code"),
        ((DIA,), "missing-systolic"),
        ((SYS,), "missing-diastolic"),
        ((obx(1, "8480-6", "80"), obx(2, "8462-4", "120")), "systolic-not-above-diastolic"),
        ((obx(1, "8480-6", "80"), DIA), "systolic-not-above-diastolic"),
        ((obx(1, "8480-6", "301"), DIA), "out-of-range"),
        ((SYS, obx(2, "8462-4", "19")), "out-of-range"),
    ],
)
def test_observation_set_problems(observations, reason):
    result = run(message(observations=observations))
    assert (result.outcome, result.reason.value) == (Outcome.ERROR, reason)
    assert result.bundle is None


def test_range_boundaries_are_accepted():
    result = run(message(observations=(obx(1, "8480-6", "300"), obx(2, "8462-4", "20"))))
    assert components(result)["8480-6"]["value"] == 300
    assert components(result)["8462-4"]["value"] == 20


# ---- OBX: one field at a time -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("systolic", "reason"),
    [
        (obx(1, "8480-6", "120", value_type="ST"), "wrong-value-type"),
        (obx(1, "8480-6", ""), "missing-obx-5"),
        (obx(1, "8480-6", "120~121"), "repeated-obx-5"),
        (obx(1, "8480-6", "HIGH"), "non-numeric-obx-5"),
        (obx(1, "8480-6", "1e2"), "non-numeric-obx-5"),
        (obx(1, "8480-6", "12 0"), "non-numeric-obx-5"),
        (obx(1, "8480-6", "١٢٠"), "non-numeric-obx-5"),  # Arabic-Indic digits
        (obx(1, "8480-6", "120", unit=""), "missing-unit"),
        (obx(1, "8480-6", "120", unit="mmHg^mmHg^UCUM"), "wrong-unit"),
        (obx(1, "8480-6", "120", unit="kPa^kPa^UCUM"), "wrong-unit"),
        (obx(1, "8480-6", "120", unit="mm[Hg]^mm[Hg]^L"), "wrong-unit"),
        (obx(1, "8480-6", "120", status="X"), "unsupported-result-status"),
        (obx(1, "8480-6", "120", status=""), "unsupported-result-status"),
        (obx(1, "8480-6", "120", status='""'), "unsupported-result-status"),
        (obx(1, "8480-6", "120", status="Q"), "unsupported-result-status"),
        (
            obx(1, "8480-6", "120", when="20261113205329+0000~20261113205330+0000"),
            "repeated-obx-14",
        ),
        (obx(1, "8480-6", '""'), "missing-obx-5"),
        (obx(1, "8480-6", "120", unit='""'), "missing-unit"),
    ],
)
def test_systolic_obx_field_problems(systolic, reason):
    result = run(message(observations=(systolic, DIA)))
    assert (result.outcome, result.reason.value) == (Outcome.ERROR, reason)


@pytest.mark.parametrize(
    ("text", "value"), [("+120", 120), ("0120", 120), ("120.", 120), ("120.5", 120.5)]
)
def test_hl7_nm_values(text, value):
    result = run(message(observations=(obx(1, "8480-6", text), DIA)))
    assert components(result)["8480-6"]["value"] == value
    assert type(components(result)["8480-6"]["value"]) is type(value)


def test_unit_without_a_coding_system_is_accepted():
    result = run(message(observations=(obx(1, "8480-6", "120", unit="mm[Hg]"), DIA)))
    assert components(result)["8480-6"]["code"] == "mm[Hg]"


@pytest.mark.parametrize(("hl7", "fhir"), [("F", "final"), ("P", "preliminary")])
def test_result_status_maps_to_observation_status(hl7, fhir):
    observations = (obx(1, "8480-6", "120", status=hl7), obx(2, "8462-4", "80", status=hl7))
    result = run(message(observations=observations))
    assert observation(result)["status"] == fhir


@pytest.mark.parametrize("hl7", ["C", "D", "W", "X", "I", "R", "S", "Z"])
def test_corrections_deletions_and_other_statuses_are_refused(hl7):
    # C (corrected), D (delete) and W (wrong) refer to an earlier result. A correction comes as
    # a new message with a new MSH-10, so writing it would add a second Observation and leave the
    # original "final" one in place. Refused, on both OBX, before anything is written.
    observations = (obx(1, "8480-6", "120", status=hl7), obx(2, "8462-4", "80", status=hl7))
    result = run(message(observations=observations))
    assert (result.outcome, result.reason.value) == (Outcome.ERROR, "unsupported-result-status")
    assert result.bundle is None


def test_mixed_result_status_is_refused():
    observations = (obx(1, "8480-6", "120", status="F"), obx(2, "8462-4", "80", status="P"))
    assert run(message(observations=observations)).reason is Reason.MIXED_RESULT_STATUS


# ---- OBX-14 and time zones --------------------------------------------------------------------


def both(when: str) -> tuple[str, str]:
    return (obx(1, "8480-6", "120", when=when), obx(2, "8462-4", "80", when=when))


@pytest.mark.parametrize(
    ("when", "effective"),
    [
        ("20261113205329+0000", "2026-11-13T20:53:29+00:00"),
        ("20261113155329-0500", "2026-11-13T15:53:29-05:00"),
        ("20261114022329+0530", "2026-11-14T02:23:29+05:30"),  # half-hour offset
        ("202611131553-0500", "2026-11-13T15:53:00-05:00"),  # minute precision
        # Fractional seconds: four HL7 digits, truncated to milliseconds by the FHIR builder.
        ("20261113155329.1239-0500", "2026-11-13T15:53:29.123-05:00"),
    ],
)
def test_obx14_with_offset_becomes_effective_datetime_with_that_offset(when, effective):
    assert observation(run(message(observations=both(when))))["effectiveDateTime"] == effective


@pytest.mark.parametrize(
    ("when", "reason"),
    [
        ("20261113155329", "missing-time-zone"),  # no offset: the sender's zone is unknown
        ("202611131553", "missing-time-zone"),
        ("20261113-0500", "invalid-obx-14"),  # day precision is not a reading time
        ("2026-11-13 20:53", "invalid-obx-14"),
        ("20261131155329-0500", "invalid-obx-14"),  # 31 November
        ("20261113155329+1500", "invalid-obx-14"),  # beyond +14:00
        ("20261113155329-05", "invalid-obx-14"),
        ("٢٠٢٦١١١٣١٥٥٣٢٩-0500", "invalid-obx-14"),  # non-ASCII digits
    ],
)
def test_obx14_problems(when, reason):
    result = run(message(observations=both(when)))
    assert (result.outcome, result.reason.value) == (Outcome.ERROR, reason)


def test_missing_obx14_is_an_error_even_with_obr7_present():
    # No OBR-7 fallback (see mapping._read_observation_time); OBR here carries OBR-7.
    result = run(message(observations=both("")))
    assert "|||20261113205329+0000" in OBR
    assert result.reason is Reason.MISSING_OBX_14


def test_one_missing_obx14_is_enough_to_refuse():
    result = run(message(observations=(SYS, obx(2, "8462-4", "80", when=""))))
    assert result.reason is Reason.MISSING_OBX_14


def test_the_same_instant_in_two_offsets_is_one_time():
    observations = (
        obx(1, "8480-6", "120", when="20261113155329-0500"),
        obx(2, "8462-4", "80", when="20261113205329+0000"),
    )
    result = run(message(observations=observations))
    assert observation(result)["effectiveDateTime"] == "2026-11-13T15:53:29-05:00"


def test_different_instants_are_refused():
    observations = (SYS, obx(2, "8462-4", "80", when="20261113205330+0000"))
    assert run(message(observations=observations)).reason is Reason.MISMATCHED_OBX_14


# ---- never raises -----------------------------------------------------------------------------


@pytest.mark.parametrize("sample", [s.name for s in all_samples()])
def test_no_prefix_of_any_sample_raises(sample):
    payload = load(sample).payload
    for end in range(len(payload) + 1):
        result = run(payload[:end])
        assert isinstance(result.outcome, Outcome)


@pytest.mark.parametrize(
    "raw",
    [b"", b"\xff\xfe\x00", b"MSH", b"MSH|", "MSH|^~\\&|é".encode(), b"\r\r\r", b"MSH|^~\\&\r" * 50],
)
def test_odd_bytes_end_as_errors(raw):
    result = run(raw)
    assert result.outcome is Outcome.ERROR


def test_an_unexpected_exception_becomes_mapping_failed_without_logging_content(
    monkeypatch, caplog
):
    def explode(*_args):
        raise RuntimeError("SYN-000123 TESTPATIENT")

    monkeypatch.setattr(mapping, "_read_patient", explode)
    with caplog.at_level(logging.ERROR):
        result = run(load("valid_bp_reading").payload)
    assert (result.outcome, result.reason) == (Outcome.ERROR, Reason.MAPPING_FAILED)
    assert result.control_id == "SYN-VALID-0001"
    assert "RuntimeError" in caplog.text
    assert "SYN-000123" not in caplog.text
    assert "TESTPATIENT" not in caplog.text


@pytest.mark.parametrize(
    "failure", [IndexError("list index"), AttributeError("x"), RecursionError()]
)
def test_any_hl7apy_failure_while_parsing_is_unparseable(monkeypatch, failure):
    # pulsebridge_hl7v2.parse_er7 turns hl7apy's own exceptions into HL7ParseError; anything
    # else hl7apy might raise on odd bytes must still be "unparseable", not a mapper bug.
    def parse(_raw):
        raise failure

    monkeypatch.setattr(mapping, "parse_er7", parse)
    assert run(load("valid_bp_reading").payload).reason is Reason.UNPARSEABLE_BY_MAPPER


def test_a_fhir_builder_refusal_becomes_fhir_input_rejected(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise FhirInputError("patient: family has leading or trailing whitespace")

    monkeypatch.setattr(mapping, "blood_pressure_transaction", refuse)
    result = run(load("valid_bp_reading").payload)
    assert (result.outcome, result.reason) == (Outcome.ERROR, Reason.FHIR_INPUT_REJECTED)
