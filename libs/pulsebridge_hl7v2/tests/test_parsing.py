"""parse_er7 and decode_escapes: the typed errors and the unescaping hl7apy lacks."""

import pytest
from pulsebridge_hl7v2 import HL7ParseError, decode_escapes, parse_er7
from pulsebridge_hl7v2.samples import load

STANDARD = {"FIELD": "|", "COMPONENT": "^", "REPETITION": "~", "ESCAPE": "\\", "SUBCOMPONENT": "&"}


def test_unknown_version_becomes_typed_error_naming_msh12():
    # The store accepts MSH-12 "9.9" (S4 row b8); hl7apy raises UnsupportedVersion (S5).
    with pytest.raises(HL7ParseError) as caught:
        parse_er7(load("unmappable_unknown_version").payload)
    assert caught.value.reason.startswith("MSH-12 version is not supported by hl7apy")


def test_barely_hl7_messages_the_store_accepts_still_parse():
    for name in ("unmappable_bare_msh", "unmappable_msh_only", "unmappable_empty_msh9"):
        assert parse_er7(load(name).payload).msh is not None


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        (b"", "empty"),
        (b"PID|1||X", "does not start with an MSH segment"),
        (b"MSH|^~\\&|A\nPID|1", "line feeds"),
        (b"MSH|^~\\&|\xff\xfe", "not valid UTF-8"),
    ],
)
def test_unreadable_input_gives_typed_error(payload, fragment):
    with pytest.raises(HL7ParseError, match=fragment):
        parse_er7(payload)


def test_decode_escapes_standard_sequences():
    assert decode_escapes("O\\F\\B\\S\\C\\T\\D\\R\\E\\E\\", STANDARD) == "O|B^C&D~E\\"


def test_decode_escapes_uses_the_message_own_separators():
    custom = dict(STANDARD, FIELD="#", ESCAPE="!")
    assert decode_escapes("a!F!b!E!c\\F\\", custom) == "a#b!c\\F\\"


def test_decode_escapes_leaves_formatting_sequences_alone():
    assert decode_escapes("line\\.br\\two \\H\\x\\N\\", STANDARD) == "line\\.br\\two \\H\\x\\N\\"
