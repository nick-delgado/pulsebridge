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


# ---- with_patient (T33) -----------------------------------------------------------------------

from dataclasses import replace  # noqa: E402

from pulsebridge_hl7v2 import Patient  # noqa: E402

OTHER = Patient(
    identifier="PB-OTHER-0042",
    assigning_authority="OTHERAUTH",
    family_name="Example",
    given_name="Avery",
)
# The samples whose PID must not or cannot be replaced, and why (Sample.with_patient docstring).
UNCHANGED = {
    "unparseable_no_msh",  # no MSH: no separators
    "unparseable_lf_terminators",  # hl7apy cannot split the segments
    "unparseable_crlf_terminators",
    "unparseable_separator_mismatch",  # hl7apy refuses the header
    "invalid_missing_pid3",  # the PID is the defect
    "unmappable_msh_only",  # no PID
    "unmappable_bare_msh",
}


@pytest.mark.parametrize("sample", ALL, ids=IDS)
def test_with_patient_changes_only_the_pid_segment(sample):
    changed = sample.with_patient(OTHER)
    assert sample.accepts_patient is (sample.name not in UNCHANGED)
    if sample.name in UNCHANGED:
        assert changed is sample
        return
    index = next(i for i, seg in enumerate(sample.segments) if seg.startswith("PID"))
    assert changed.segments[index] == "PID|1||PB-OTHER-0042^^^OTHERAUTH^MR||Example^Avery"
    # Every other segment, the terminators, the framing, the class and the expectations: as is.
    assert changed == replace(sample, segments=changed.segments)
    others = [seg for i, seg in enumerate(changed.segments) if i != index]
    assert others == [seg for i, seg in enumerate(sample.segments) if i != index]
    # On the wire: the bytes before and after the PID are the original bytes exactly, so a
    # broken frame stays broken.
    before, _, after = sample.wire_bytes.partition(sample.segments[index].encode("ascii"))
    assert changed.wire_bytes == before + changed.segments[index].encode("ascii") + after


def test_with_patient_keeps_the_sample_defect_where_a_layer_checks_it():
    # The layers this package can check give the same answer after substitution.
    for sample in ALL:
        changed = sample.with_patient(OTHER)
        assert _validation(changed) == _validation(sample), sample.name
        assert _tolerant_parse(changed) == _tolerant_parse(sample), sample.name
        assert _frame_completes(changed) == _frame_completes(sample), sample.name


def _validation(sample) -> str:
    try:
        validate_oru_r01(sample.payload)
    except (HL7ValidationError, HL7ParseError):
        return "fail"
    return "pass"


def _tolerant_parse(sample) -> str:
    try:
        parse_er7(sample.payload)
    except HL7ParseError:
        return "error"
    return "ok"


def _frame_completes(sample) -> bool:
    return bool(MllpDecoder().feed(sample.wire_bytes))


def test_with_patient_reads_back_as_the_new_patient_with_hl7apy():
    message = parse_er7(load("unmappable_missing_obx14").with_patient(OTHER).payload)
    pid = message.oru_r01_patient_result.oru_r01_patient.pid  # grouped by the ORU_R01 structure
    assert pid.pid_3.cx_1.to_er7() == "PB-OTHER-0042"
    assert pid.pid_3.cx_4.to_er7() == "OTHERAUTH"
    assert pid.pid_5.to_er7() == "Example^Avery"


def test_with_patient_escapes_separators_in_patient_values():
    # A name with HL7 separators in it becomes escape sequences (2.5.1 section 2.7), never new
    # components or fields: the builder's rule.
    odd = replace(OTHER, family_name="O|Brien^&", given_name="Ann~Marie")
    pid = load("unmappable_unknown_obx3_code").with_patient(odd).segments[1]
    assert pid == "PID|1||PB-OTHER-0042^^^OTHERAUTH^MR||O\\F\\Brien\\S\\\\T\\^Ann\\R\\Marie"


@pytest.mark.parametrize(
    "bad",
    [replace(OTHER, identifier=""), replace(OTHER, given_name="A\\F\\B"), "PB-OTHER-0042"],
)
def test_with_patient_refuses_unusable_patient_values_for_every_sample(bad):
    for name in ("unmappable_missing_obx14", "invalid_missing_pid3"):
        with pytest.raises((ValueError, TypeError)):
            load(name).with_patient(bad)


def test_with_patient_leaves_a_pid_written_with_other_separators_alone():
    # MSH declares "|", the PID uses "#": hl7apy would read one long PID-1, and rewriting it
    # would repair the defect. No shipped sample is like this, so one is made here.
    # hl7apy reads this PID's PID-3.1 as "y" and writes the segment back with "|" for the "#".
    sample = load("unmappable_missing_obx14")
    pid = "PID#1##SYN-000123|x|y|SYN-1^^^A^MR"
    odd = replace(sample, segments=(sample.segments[0], pid, *sample.segments[2:]))
    assert not odd.accepts_patient
    assert odd.with_patient(OTHER) is odd


def test_with_patient_leaves_a_sample_with_two_pid_segments_alone():
    # Which PID would be "the" patient? Replacing one would leave a mixed message.
    sample = load("unmappable_missing_obx14")
    two = replace(sample, segments=(*sample.segments[:2], sample.segments[1], *sample.segments[2:]))
    assert not two.accepts_patient
    assert two.with_patient(OTHER) is two


def test_with_patient_leaves_a_sample_whose_segments_do_not_line_up_alone():
    # One manifest entry holding two segments: hl7apy's segment positions are not the
    # manifest's, so no manifest entry may be replaced.
    sample = load("unmappable_missing_obx14")
    joined = replace(sample, segments=("\r".join(sample.segments[:2]),))
    assert not joined.accepts_patient
    assert joined.with_patient(OTHER) is joined


def test_with_patient_leaves_a_header_hl7apy_crashes_on_alone():
    # hl7apy 1.3.5 raises IndexError for a five-character MSH-2 with no MSH-12 (see ack.py).
    sample = load("unmappable_missing_obx14")
    odd = replace(sample, segments=("MSH|^~\\&#|CUFFSIM", *sample.segments[1:]))
    assert not odd.accepts_patient
    assert odd.with_patient(OTHER) is odd


def test_with_patient_writes_the_new_pid_with_the_sample_own_separators():
    # A sample declaring "#" as field and "$" as component separator (MSH-1, MSH-2): the new PID
    # uses them, and a separator inside a name is escaped with them (\F\, \S\).
    sample = load("unmappable_missing_obx14")
    table = str.maketrans({"|": "#", "^": "$"})
    odd = replace(sample, segments=tuple(seg.translate(table) for seg in sample.segments))
    assert odd.segments[0].startswith("MSH#$~\\&#")
    patient = replace(OTHER, family_name="O#Brien", given_name="Ann$Marie")
    changed = odd.with_patient(patient)
    assert changed.segments[1] == "PID#1##PB-OTHER-0042$$$OTHERAUTH$MR##O\\F\\Brien$Ann\\S\\Marie"
    assert changed.segments[2:] == odd.segments[2:]


def test_with_patient_keeps_only_pid_1_3_and_5():
    # Other PID fields (here PID-7 birth date, PID-8 sex) and further PID-3 repetitions are not
    # carried over: the new PID is the builder's (samples README).
    sample = load("unmappable_missing_obx14")
    full = "PID|1||SYN-000123^^^PULSEBRIDGE^MR~OTHER-9^^^X^MR||TESTPATIENT^SYNTHIA||19800412|F"
    rich = replace(sample, segments=(sample.segments[0], full, *sample.segments[2:]))
    assert rich.with_patient(OTHER).segments[1] == (
        "PID|1||PB-OTHER-0042^^^OTHERAUTH^MR||Example^Avery"
    )


def test_with_patient_leaves_a_pid_with_trailing_empty_fields_alone():
    # hl7apy drops trailing empty fields when it writes the PID back, so the bytes differ.
    sample = load("unmappable_missing_obx14")
    pid = "PID|1||SYN-000123^^^PULSEBRIDGE^MR||TESTPATIENT^SYNTHIA|||"
    trailing = replace(sample, segments=(sample.segments[0], pid, *sample.segments[2:]))
    assert not trailing.accepts_patient
    assert trailing.with_patient(OTHER) is trailing
