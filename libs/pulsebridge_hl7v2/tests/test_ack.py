"""ACK parsing: AC "returns the acknowledgment code (AA, AE, AR) and the control ID it answers".

The byte strings marked S4/S5 are copied from docs/spikes/s4.md and s5.md: what the real
Cloud Healthcare HL7v2 store sent back (synthetic messages; the ACK control IDs are the store's).
"""

import pytest
from pulsebridge_hl7v2 import AckCode, AckParseError, parse_ack
from pulsebridge_hl7v2.mllp import MllpDecoder

# S4, valid message over MLLP, framing included as received.
S4_AA_FRAMED = (
    b"\x0bMSH|^~\\&|PHR|PULSEBRIDGE|CUFFSIM|PULSEBRIDGE|20261113205330||ACK|"
    b"6c52ee99-ac83-49c0-b976-cd739886d858|P|2.5.1\rMSA|AA|S4-205329-0001\x1c\r"
)
# S4 b1, unparseable message: empty header fields, empty MSA-2, reason in MSA-3.
S4_NACK_B1_FRAMED = (
    b"\x0bMSH|^~\\&|||||20261113205348||ACK|63a5f7b7-0ae4-42a6-b517-eed75c1861a3\r"
    b"MSA|AE||Invalid MSH header\x1c\r"
)
# S4 a6, line-feed terminators; MSA-3 contains the escape sequence \E\ (a backslash).
S4_NACK_A6_FRAMED = (
    b"\x0bMSH|^~\\&|||||20261113205348||ACK|57eb795c-d742-4a29-8bdc-4a8ed3c7f356\r"
    b'MSA|AE||invalid segment terminator, store expects: "\\E\\r"\x1c\r'
)
# S5, HTTPS ingest: the base64-decoded hl7Ack. No framing and no trailing carriage return.
S5_AA_HTTPS = (
    b"MSH|^~\\&|PHR|PULSEBRIDGE|CUFFSIM|PULSEBRIDGE|20261113224607||ACK|"
    b"2478b790-9ee0-4adf-b7de-a897e8fecc6b|P|2.5.1\rMSA|AA|S5-0001"
)
S5_NACK_HTTPS = (
    b"MSH|^~\\&|||||20261113225233||ACK|cf6fbef5-243b-4873-bdb2-dc5bcca37253\r"
    b"MSA|AE||Invalid MSH header"
)
# S5, rejectDuplicateMessage store: a NACK that does carry MSA-2.
S5_DUPLICATE_NACK = (
    b"MSH|^~\\&|PHR|PULSEBRIDGE|CUFFSIM|PULSEBRIDGE|20261113224607||ACK|x1|P|2.5.1\r"
    b"MSA|AE|S5-RD|message already exists"
)


def unframe(data: bytes) -> bytes:
    (payload,) = MllpDecoder().feed(data)
    return payload


def test_store_accept_over_mllp():
    ack = parse_ack(unframe(S4_AA_FRAMED))
    assert ack.code is AckCode.AA
    assert ack.accepted
    assert ack.acknowledged_control_id == "S4-205329-0001"
    assert ack.reason is None
    assert ack.message_type == "ACK"
    assert ack.ack_control_id == "6c52ee99-ac83-49c0-b976-cd739886d858"
    assert ack.version == "2.5.1"


def test_store_nack_with_blank_header_and_empty_msa2():
    ack = parse_ack(unframe(S4_NACK_B1_FRAMED))
    assert ack.code is AckCode.AE
    assert not ack.accepted
    assert ack.acknowledged_control_id is None
    assert ack.reason == "Invalid MSH header"
    assert ack.version is None


def test_nack_reason_has_hl7_escape_sequences_decoded():
    ack = parse_ack(unframe(S4_NACK_A6_FRAMED))
    assert ack.reason == 'invalid segment terminator, store expects: "\\r"'


def test_https_ack_without_trailing_carriage_return():
    ack = parse_ack(S5_AA_HTTPS)
    assert (ack.code, ack.acknowledged_control_id) == (AckCode.AA, "S5-0001")
    nack = parse_ack(S5_NACK_HTTPS)
    assert (nack.code, nack.acknowledged_control_id, nack.reason) == (
        AckCode.AE,
        None,
        "Invalid MSH header",
    )


def test_nack_that_carries_the_control_id():
    ack = parse_ack(S5_DUPLICATE_NACK)
    assert (ack.code, ack.acknowledged_control_id, ack.reason) == (
        AckCode.AE,
        "S5-RD",
        "message already exists",
    )


def test_application_reject_and_ack_with_trigger_event():
    # The store never sends AR (S4), but other receivers do, and "ACK^R01^ACK" is the textbook form.
    ack = parse_ack(
        b"MSH|^~\\&|RCV|FAC|SND|FAC|20261113205330||ACK^R01^ACK|A1|P|2.5.1\r"
        b"MSA|AR|SYN-0001|unsupported message type\r"
    )
    assert (ack.code, ack.acknowledged_control_id, ack.reason) == (
        AckCode.AR,
        "SYN-0001",
        "unsupported message type",
    )
    assert ack.message_type == "ACK^R01^ACK"


def test_ack_with_unknown_version_still_parses():
    # The store echoes MSH-12; for the 9.9 sample hl7apy raises UnsupportedVersion on the reply.
    ack = parse_ack(
        b"MSH|^~\\&|PHR|PB|CUFFSIM|PB|20261113205330||ACK|A2|P|9.9\rMSA|AA|SYN-MAP-0001\r"
    )
    assert (ack.code, ack.acknowledged_control_id, ack.version) == (
        AckCode.AA,
        "SYN-MAP-0001",
        "9.9",
    )


def test_ack_with_custom_field_separator():
    # S4 row b7: a message declaring "#" in MSH-1 gets an ACK that also uses "#".
    ack = parse_ack(b"MSH#^~\\&#PHR#PB#C#PB#20261113205330##ACK#A3#P#2.5.1\rMSA#AA#SYN-7\r")
    assert (ack.code, ack.acknowledged_control_id) == (AckCode.AA, "SYN-7")


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        (b"", "not a readable"),
        (b"THIS IS NOT HL7", "not a readable"),
        (b"MSH|^~\\&|A|B|C|D|20261113205330||ORU^R01|X|P|2.5.1\rMSA|AA|1", "not ACK"),
        (b"MSH|^~\\&|A|B|C|D|20261113205330||ACK|X|P|2.5.1\r", "no MSA"),
        (
            b"MSH|^~\\&|A|B|C|D|20261113205330||ACK|X|P|2.5.1\rMSA|OK|1",
            "not an HL7 acknowledgment code",
        ),
        (
            b"MSH|^~\\&|A|B|C|D|20261113205330||ACK|X|P|2.5.1\rMSA||1",
            "not an HL7 acknowledgment code",
        ),
    ],
)
def test_non_acks_are_refused_with_a_reason(payload, fragment):
    with pytest.raises(AckParseError, match=fragment):
        parse_ack(payload)
