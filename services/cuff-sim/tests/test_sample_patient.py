"""T33: "send malformed" samples carry the CONFIGURED patient, so the mapper reaches each
sample's own reason on any deployment, not ``unknown-patient``.

The proof runs the bytes ``CuffSimulator.prepare`` would send through the real ``v2-to-fhir``
mapping module (T12), configured for a patient that is not T07's fixed ``SYN-000123``. Both are
installed in the repository's one virtual environment (``make install``). All values are
synthetic.
"""

from datetime import UTC, datetime

import pytest
from cuff_sim.message import malformed_samples
from cuff_sim.request import SendRequest
from cuff_sim.service import CuffSimulator
from pulsebridge_hl7v2.samples import load
from v2_to_fhir.mapping import MappingConfig, map_message

OTHER = {
    "PATIENT_ID": "PB-OTHER-0042",
    "PATIENT_ASSIGNING_AUTHORITY": "OTHERAUTH",
    "PATIENT_FAMILY_NAME": "Example",
    "PATIENT_GIVEN_NAME": "Avery",
}
# v2-to-fhir configured for the same patient, assigning authority checked too (PID-3.4).
MAPPER = MappingConfig(patient_id="PB-OTHER-0042", patient_assigning_authority="OTHERAUTH")
MESSAGE_NAME = "projects/demo/locations/us-central1/datasets/demo/hl7V2Stores/main/messages/x"
RECORDED = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)

# The four samples the T13 review named: with T07's fixed patient they ended in unknown-patient.
NAMED = (
    "unmappable_unknown_obx3_code",
    "unmappable_missing_obx14",
    "unmappable_non_numeric_obx5",
    "invalid_obx14_not_hl7_date",
)


@pytest.fixture
def simulator(h, client):
    return CuffSimulator(h.make_config(h.closed_port(), **OTHER), client)


def sent_bytes(simulator, name: str, transport: str = "mllp") -> bytes:
    return simulator.prepare(SendRequest(transport=transport, sample_name=name)).payload


def mapped_reason(raw: bytes) -> str:
    result = map_message(raw, message_name=MESSAGE_NAME, config=MAPPER, recorded=RECORDED)
    return result.reason.value


@pytest.mark.parametrize("name", NAMED)
@pytest.mark.parametrize("transport", ["mllp", "https"])
def test_named_samples_reach_their_own_reason_with_another_patient(simulator, name, transport):
    sample = load(name)
    # Without substitution the mapper stops at the patient check: the bug being fixed.
    assert mapped_reason(sample.payload) == "unknown-patient"
    assert mapped_reason(sent_bytes(simulator, name, transport)) == sample.expected.mapping_reason


# Every sample the store stores and the mapper then rejects. The others never reach the mapper:
# the store answers AE (unparseable) or the frame never completes (noack).
STORED_AND_REJECTED = [s for s in malformed_samples() if s.expected.application_outcome == "error"]


def test_the_stored_and_rejected_set_is_what_the_manifest_says():
    assert len(STORED_AND_REJECTED) == 11
    assert set(NAMED) <= {s.name for s in STORED_AND_REJECTED}


@pytest.mark.parametrize("sample", STORED_AND_REJECTED, ids=lambda s: s.name)
def test_every_sample_the_mapper_sees_gets_the_manifest_reason(simulator, sample):
    # The manifest's reason, never unknown-patient.
    transport = sample.transports[0]
    assert mapped_reason(sent_bytes(simulator, sample.name, transport)) == (
        sample.expected.mapping_reason
    )


def test_a_framing_sample_keeps_its_broken_frame_with_the_new_patient(simulator):
    request = SendRequest(transport="mllp", sample_name="noack_missing_end_block")
    outgoing = simulator.prepare(request)
    assert b"PB-OTHER-0042^^^OTHERAUTH^MR||Example^Avery" in outgoing.payload
    assert outgoing.wire_bytes == b"\x0b" + outgoing.payload  # still no end block
    assert outgoing.framing == "missing_end_block"
    # A broken frame never reaches the mapper, but its message is a valid reading for this
    # patient: only the framing is wrong.
    assert mapped_reason(outgoing.payload) == "mapped"
