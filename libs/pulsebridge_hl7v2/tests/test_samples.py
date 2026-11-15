"""Sample messages: AC "synthetic and in libs/pulsebridge_hl7v2/samples/", and the manifest's
expected outcomes are true for every layer that can be checked without GCP.

The store-level expectations (commit ACK, reason) come from spikes S4 and S5 and are not
re-checked here; ``evidence`` on each sample says where each one comes from.
"""

from pathlib import Path

import pulsebridge_hl7v2.samples as samples_pkg
import pytest
from pulsebridge_hl7v2 import (
    HL7ParseError,
    HL7ValidationError,
    MllpDecoder,
    parse_er7,
    validate_oru_r01,
)
from pulsebridge_hl7v2.samples import CLASSES, EVIDENCE_KINDS, all_samples, by_class, load
from pulsebridge_hl7v2.samples.__main__ import export

ALL = all_samples()
IDS = [s.name for s in ALL]


def test_samples_ship_from_the_package_samples_directory():
    here = Path(samples_pkg.__file__).resolve().parent
    assert here.name == "samples"
    assert here.parent.name == "pulsebridge_hl7v2"  # libs/pulsebridge_hl7v2/samples
    assert (here / "samples.json").is_file()


def test_every_d7_class_has_at_least_one_sample():
    for sample_class in CLASSES:
        assert by_class(sample_class), sample_class


def test_required_cases_from_the_brief_are_present():
    required = {
        "unparseable_no_msh",
        "unparseable_lf_terminators",
        "noack_missing_end_block",
        "unmappable_unknown_obx3_code",
        "unmappable_missing_obx14",
        "unmappable_non_numeric_obx5",
        "unmappable_msh_only",
        "unmappable_unknown_version",
        "unmappable_unknown_msh9",
        "unmappable_empty_msh12",
    }
    assert required <= set(IDS)


def test_names_are_unique():
    assert len(IDS) == len(set(IDS))


def test_unknown_name_lists_the_known_ones():
    with pytest.raises(KeyError, match="valid_bp_reading"):
        load("nope")


@pytest.mark.parametrize("sample", ALL, ids=IDS)
def test_hl7apy_validation_outcome_matches_manifest(sample):
    if sample.expected.hl7apy_validation == "pass":
        validate_oru_r01(sample.payload)
    else:
        with pytest.raises(HL7ValidationError):
            validate_oru_r01(sample.payload)


@pytest.mark.parametrize("sample", ALL, ids=IDS)
def test_hl7apy_tolerant_parse_outcome_matches_manifest(sample):
    if sample.expected.hl7apy_parse == "ok":
        parse_er7(sample.payload)
    else:
        with pytest.raises(HL7ParseError):
            parse_er7(sample.payload)


@pytest.mark.parametrize("sample", ALL, ids=IDS)
def test_mllp_outcome_matches_manifest(sample):
    frames = MllpDecoder().feed(sample.wire_bytes)
    if sample.expected.commit_ack is None:
        assert sample.expected.no_ack == "timeout"
        assert frames == []  # the adapter would wait for end bytes that never come
    else:
        assert frames == [sample.payload]


@pytest.mark.parametrize("sample", ALL, ids=IDS)
def test_segment_terminators_are_exactly_as_declared(sample):
    payload = sample.payload
    if sample.segment_terminator == "\r":
        assert b"\n" not in payload
    if sample.segment_terminator == "\n":
        assert b"\r" not in payload
    if sample.segment_terminator == "\r\n":
        assert payload.count(b"\r\n") == len(sample.segments)


def test_payloads_are_reproducible_bytes():
    # The manifest is JSON so that no git setting or text hook can change a CR to LF.
    assert load("valid_bp_reading").payload.count(b"\r") == 5
    assert load("unparseable_lf_terminators").payload.count(b"\n") == 5
    assert load("noack_missing_end_block").wire_bytes.startswith(b"\x0bMSH|")
    assert not load("noack_missing_end_block").wire_bytes.endswith(b"\x1c\r")
    assert load("noack_missing_final_cr").wire_bytes.endswith(b"\x1c")


@pytest.mark.parametrize("sample", by_class("parses_but_cannot_map"), ids=lambda s: s.name)
def test_manifest_consistency_unmappable_samples_expect_aa_then_mapping_error(sample):
    # Checks the manifest's own consistency only; T12's tests exercise the mapper on these.
    assert sample.expected.commit_ack == "AA"
    assert sample.expected.application_outcome == "error"
    assert sample.expected.mapping_reason


@pytest.mark.parametrize("sample", by_class("unparseable"), ids=lambda s: s.name)
def test_manifest_consistency_unparseable_samples_expect_ae_with_reason(sample):
    # Checks the manifest's own consistency only; the store's replies come from spikes S4/S5.
    assert sample.expected.commit_ack == "AE"
    assert sample.expected.commit_ack_reason
    assert sample.expected.application_outcome is None


def test_unmappable_samples_carry_the_defect_they_claim():
    # Read through hl7apy so a mistaken edit to the manifest would be caught.
    unknown = parse_er7(load("unmappable_unknown_obx3_code").payload)
    order = unknown.oru_r01_patient_result.oru_r01_order_observation
    codes = [g.obx.obx_3.obx_3_1.to_er7() for g in order.oru_r01_observation]
    assert codes == ["99999-9", "8462-4"]

    missing = parse_er7(load("unmappable_missing_obx14").payload)
    order = missing.oru_r01_patient_result.oru_r01_order_observation
    assert [g.obx.obx_14.to_er7() for g in order.oru_r01_observation] == ["", ""]

    text = parse_er7(load("unmappable_non_numeric_obx5").payload)
    order = text.oru_r01_patient_result.oru_r01_order_observation
    assert order.oru_r01_observation[0].obx.obx_5.to_er7() == "HIGH"

    assert parse_er7(load("unmappable_empty_msh9").payload).msh.msh_9.to_er7() == ""
    assert parse_er7(load("unmappable_unknown_msh9").payload).msh.msh_9.to_er7() == (
        "ADT^A01^ADT_A01"
    )
    assert load("unmappable_empty_msh12").segments[0].endswith("|P|")
    assert load("unmappable_msh_only").segments[0].startswith("MSH|")
    assert len(load("unmappable_msh_only").segments) == 1


@pytest.mark.parametrize("sample", ALL, ids=IDS)
def test_samples_use_only_the_synthetic_patient(sample):
    for segment in sample.segments:
        if not segment.startswith("PID"):
            continue
        fields = segment.split(segment[3])  # the character after "PID" is the field separator
        pid_3, pid_5 = fields[3], fields[5]
        assert pid_3 in ("", "SYN-000123^^^PULSEBRIDGE^MR")
        assert pid_5 == "TESTPATIENT^SYNTHIA"


def test_export_writes_exact_bytes(tmp_path):
    export(tmp_path)
    for sample in ALL:
        assert (tmp_path / f"{sample.name}.hl7").read_bytes() == sample.payload
        assert (tmp_path / f"{sample.name}.mllp").read_bytes() == sample.wire_bytes


@pytest.mark.parametrize("sample", ALL, ids=IDS)
def test_evidence_is_given_per_transport_and_marked(sample):
    assert set(sample.evidence) == set(sample.transports)
    for text in sample.evidence.values():
        assert text.startswith(EVIDENCE_KINDS)


@pytest.mark.parametrize(
    ("name", "transport"),
    [
        ("unparseable_crlf_terminators", "mllp"),  # S4 never sent CR LF
        ("unmappable_bare_msh", "mllp"),  # S4 did not record whether row b4 ended in CR
        ("unmappable_unknown_version", "https"),  # S5 did not send version 9.9
        ("unmappable_empty_msh12", "https"),  # S5 did not send an empty MSH-12
        ("valid_bp_reading_offset", "mllp"),
        ("invalid_missing_pid3", "mllp"),
        ("invalid_obx14_not_hl7_date", "mllp"),
    ],
)
def test_expectations_no_spike_observed_are_marked_inferred(name, transport):
    assert load(name).evidence[transport].startswith("inferred:")


@pytest.mark.parametrize(
    "name", ["valid_bp_reading", "unparseable_separator_mismatch", "unparseable_no_msh"]
)
def test_observed_evidence_says_the_exact_bytes_were_not_sent(name):
    for text in load(name).evidence.values():
        assert "exact bytes" in text and "not sent" in text


def test_a_malformed_manifest_entry_is_refused():
    from pulsebridge_hl7v2.samples import _sample_from_json

    raw = {
        "name": "x",
        "class": "valid",
        "description": "",
        "evidence": {"mllp": "seen somewhere"},
        "segments": ["MSH|^~\\&"],
        "segment_terminator": "\r",
        "terminate_last_segment": True,
        "mllp_framing": "standard",
        "transports": ["mllp"],
        "expected": {
            "hl7apy_validation": "fail",
            "hl7apy_parse": "ok",
            "commit_ack": "AA",
            "commit_ack_reason": None,
            "no_ack": None,
            "application_outcome": "error",
            "mapping_reason": None,
        },
    }
    with pytest.raises(ValueError, match="evidence must start with"):
        _sample_from_json(raw)
    with pytest.raises(ValueError, match="exactly its transports"):
        _sample_from_json(dict(raw, transports=["mllp", "https"]))
