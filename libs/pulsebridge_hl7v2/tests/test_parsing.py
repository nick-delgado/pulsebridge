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


def test_empty_version_becomes_typed_error_naming_msh12():
    # S4 row b9: the store accepts an empty MSH-12 with AA; hl7apy cannot pick a version.
    with pytest.raises(HL7ParseError, match="MSH-12"):
        parse_er7(load("unmappable_empty_msh12").payload)


# ---- Only HL7ParseError escapes parse_er7; only HL7ValidationError escapes validation (T33) -----

import random  # noqa: E402

from pulsebridge_hl7v2 import HL7ValidationError, validate_oru_r01  # noqa: E402
from pulsebridge_hl7v2.samples import all_samples  # noqa: E402

# Headers for which hl7apy 1.3.5 raises IndexError (see pulsebridge_hl7v2.parsing).
HL7APY_CRASHES = [
    # A non-ASCII fifth encoding character with no MSH-12 (found by the T33 review's fuzz run).
    (b"MSH|^~\\&\xef\x88\xbf\r", "no MSH-12"),
    # A five-character MSH-2 (2.7's truncation character) in a header with no MSH-12.
    (b"MSH|^~\\&#|CUFFSIM|PB|PHR|PB|20261113205329||ORU^R01|X\rPID|1", "no MSH-12"),
    # Whitespace as an encoding character, in a header that ends after MSH-2.
    (b"MSH|^~\\ \rPID|1", "five distinct delimiter characters"),
]


@pytest.mark.parametrize(("payload", "fragment"), HL7APY_CRASHES)
def test_headers_that_crash_hl7apy_are_parse_errors(payload, fragment):
    with pytest.raises(HL7ParseError, match=fragment) as caught:
        parse_er7(payload)
    # The reason is the check's own sentence, not wrapped as a data-type error.
    assert caught.value.reason.startswith(("MSH-1 and MSH-2", "MSH-2 has five"))
    with pytest.raises(HL7ValidationError, match=fragment):
        validate_oru_r01(payload)


_SEEDS = tuple(s.payload for s in all_samples())
_ALPHABET = b"|^~\\&#\r\n MSHPIDOBX0123456789.\t\x0b\x1c\x00\x80\xff\xef\x88\xbf"


def _mutate(rng: random.Random, data: bytes) -> bytes:
    out = bytearray(data)
    for _ in range(rng.randint(1, 6)):
        op = rng.randrange(6)
        pos = rng.randrange(len(out) + 1)
        if op == 5 and len(out) > 8:
            out[rng.randrange(3, 9)] = rng.choice(_ALPHABET)  # the header's delimiters
        elif op == 0 and out:
            del out[min(pos, len(out) - 1)]
        elif op == 1:
            out.insert(pos, rng.choice(_ALPHABET))
        elif op == 2 and out:
            out[min(pos, len(out) - 1)] = rng.choice(_ALPHABET)
        elif op == 3:
            del out[pos:]
        else:
            out[pos:pos] = bytes(rng.choice(_ALPHABET) for _ in range(rng.randint(1, 4)))
    return bytes(out)


def test_fuzzed_messages_raise_only_the_package_errors():
    """Seeded: the same 3,000 mutations of T07's samples every run. Before T33's fix about 2% of
    such inputs raised IndexError from inside hl7apy."""
    rng = random.Random(20261116)  # noqa: S311 - reproducible test data, not security
    escaped = []
    for i in range(3000):
        data = _mutate(rng, rng.choice(_SEEDS))
        try:
            parse_er7(data)
        except HL7ParseError:
            pass
        except Exception as exc:  # noqa: BLE001 - this is what the test looks for
            escaped.append(("parse_er7", type(exc).__name__, data[:40]))
        if i % 3:
            continue  # validation is slower: every third input
        try:
            validate_oru_r01(data)
        except HL7ValidationError:
            pass
        except Exception as exc:  # noqa: BLE001
            escaped.append(("validate_oru_r01", type(exc).__name__, data[:40]))
    assert escaped == []
