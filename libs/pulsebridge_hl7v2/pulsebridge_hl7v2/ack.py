"""Read an HL7v2 acknowledgment (ACK) into a small result type, using hl7apy.

An ACK is the reply a receiver sends for each message. Its MSA segment ("message acknowledgment")
carries three fields we need:

- MSA-1 acknowledgment code (HL7 table 0008): AA accept, AE error, AR reject in original mode;
  CA, CE, CR are the commit-level equivalents in enhanced mode.
- MSA-2 message control ID: the MSH-10 of the message being answered.
- MSA-3 text message: a human-readable reason, used on a NACK. (MSA-3 is kept "for backward
  compatibility" in 2.5.1, but it is what the Cloud Healthcare HL7v2 store fills in.)

Shapes observed from the real HL7v2 store (spikes S4 and S5), all of which this parser accepts:

- MSH-9 is just ``ACK``, not ``ACK^R01``. hl7apy's STRICT level cannot map that to a message
  structure, so the ACK is read at TOLERANT level.
- On a NACK for an unparseable message MSH-3 to MSH-6 and MSH-12 are empty and MSA-2 is empty,
  because the store could not read the original header. hl7apy then uses its default version.
- Over MLLP the bytes arrive framed (the frame is removed by :mod:`pulsebridge_hl7v2.mllp`); over
  HTTPS ingest they are base64 in JSON with no trailing carriage return. This parser takes the
  plain ACK bytes either way; unwrapping JSON and base64 belongs to the caller.
- A reply to a message with an unknown MSH-12 (for example ``9.9``) repeats that version, which
  hl7apy refuses with ``UnsupportedVersion``. We then ask hl7apy to read the same segments as
  2.5.1 (``parse_segments`` with an explicit version); MSH and MSA have not changed shape in a way
  that matters for these three fields.

**Only ``AckParseError`` escapes, for any input bytes.** hl7apy 1.3.5 has two header defects that
raise plain Python errors instead of its own exceptions; both were found by a seeded fuzz test
(``tests/test_ack.py``) and each is prevented here, not caught blindly:

- ``hl7apy.parser._split_msh``: when MSH-2 holds five characters (HL7 2.7 added a fifth, the
  truncation character) it compares MSH-12 with ``"2.7"`` without checking that the header has
  twelve fields, so a 2.7-style header cut short (``MSH|^~\\&#|PHR|...|ACK|A|``) raises
  ``IndexError``. :func:`_message_info` calls that reader first and turns exactly this
  ``IndexError`` into an ``AckParseError``. The case first reported, ``MSH^~MSH|`` (field
  separator ``^``, so "MSH-2" is ``~MSH|``), took the same path; it is now refused one step
  earlier, because ``M`` is not a delimiter character (next point).
- ``hl7apy.core.Message._get_encoding_chars``: the header reader accepts a space or other
  whitespace as an encoding character, but the field parser then strips it, so MSH-2 has three
  characters and reading the separators raises ``IndexError``. :func:`_check_delimiters` refuses
  such a header first, by the rule HL7 2.5.1 section 2.5.4 gives for delimiters (no letters,
  digits or whitespace).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from hl7apy import exceptions as hl7apy_exceptions
from hl7apy.consts import VALIDATION_LEVEL
from hl7apy.core import Message
from hl7apy.parser import get_message_info, parse_message, parse_segments

from .parsing import HL7ParseError, check_framing_text, decode_escapes, decode_payload

# Used only when an ACK names a version hl7apy does not know (see module docstring).
FALLBACK_VERSION = "2.5.1"

# MSH-1 (field separator) is the character right after "MSH", and MSH-2 (encoding characters)
# starts right after it: component, repetition, escape, subcomponent (2.5.1 sections 2.15.9.1 and
# 2.15.9.2). So the first five delimiters sit at fixed positions 3 to 7 of every header; reading
# them by position is how hl7apy itself finds MSH-1, not ER7 splitting.
_DELIMITERS = slice(3, 8)


class AckCode(StrEnum):
    """MSA-1, HL7 table 0008."""

    AA = "AA"  # original mode: application accept
    AE = "AE"  # original mode: application error
    AR = "AR"  # original mode: application reject
    CA = "CA"  # enhanced mode: commit accept
    CE = "CE"  # enhanced mode: commit error
    CR = "CR"  # enhanced mode: commit reject


class AckParseError(ValueError):
    """The bytes are not a readable HL7v2 ACK."""


@dataclass(frozen=True)
class Ack:
    """What an ACK says.

    ``acknowledged_control_id`` is MSA-2, or ``None`` when empty: on a NACK for an unparseable
    message the store cannot know the control ID, so callers must not require it to match.
    ``reason`` is MSA-3 with HL7 escape sequences decoded, or ``None``.
    """

    code: AckCode
    acknowledged_control_id: str | None
    reason: str | None
    message_type: str  # MSH-9 as sent, e.g. "ACK"
    ack_control_id: str | None  # MSH-10 of the ACK itself
    version: str | None  # MSH-12 of the ACK, if any
    raw: bytes

    @property
    def accepted(self) -> bool:
        """True for AA and CA."""
        return self.code in (AckCode.AA, AckCode.CA)


def _check_delimiters(text: str) -> None:
    """Refuse a header whose delimiters HL7 does not allow, before hl7apy reads it.

    HL7 2.5.1 section 2.5.4: the field separator and the four encoding characters are single,
    distinct characters, and none may be a letter, a digit or whitespace (the segment terminator
    is a carriage return, and ER7 data is text). hl7apy checks that they are distinct, but it
    accepts whitespace in one place and strips it in another (see the module docstring).
    """
    delimiters = text[_DELIMITERS]
    if (
        len(delimiters) < 5
        or len(set(delimiters)) < 5
        or any(not ("\x21" <= c <= "\x7e") or c.isalnum() for c in delimiters)
    ):
        raise AckParseError(
            "MSH-1 and MSH-2 must be five distinct delimiter characters (printable, not a letter, "
            "digit or space), such as |^~\\&"
        )


def _message_info(text: str) -> tuple[dict, str | None, str | None]:
    """hl7apy's header reader (encoding characters, structure, version), with its one known crash
    turned into ``AckParseError``. Called before ``parse_message``, which calls it again."""
    try:
        return get_message_info(text)
    except IndexError as exc:
        # hl7apy 1.3.5 parser._split_msh, a five-character MSH-2 in a header of fewer than twelve
        # fields: it reads MSH-12 to decide whether 2.7's fifth (truncation) character is allowed
        # without checking MSH-12 exists. Nothing else in that function indexes past MSH-2.
        raise AckParseError(
            "MSH-2 has five encoding characters, which only HL7 2.7 and later allow, and the "
            "header has no MSH-12 (version) to say so"
        ) from exc


def _parse_tolerant(text: str) -> Message:
    _check_delimiters(text)
    encoding_chars, _structure, _version = _message_info(text)
    try:
        return parse_message(text, validation_level=VALIDATION_LEVEL.TOLERANT, find_groups=False)
    except hl7apy_exceptions.UnsupportedVersion:
        message = Message(version=FALLBACK_VERSION, validation_level=VALIDATION_LEVEL.TOLERANT)
        message.children = parse_segments(
            text,
            version=FALLBACK_VERSION,
            encoding_chars=encoding_chars,
            validation_level=VALIDATION_LEVEL.TOLERANT,
        )
        return message


def _value(field) -> str | None:
    text = field.to_er7() if field else ""
    return text or None


def parse_ack(payload: bytes | str) -> Ack:
    """Parse ACK bytes (no MLLP frame; trailing carriage return optional). Raises AckParseError."""
    raw = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    try:
        text = decode_payload(payload)
        check_framing_text(text)
        message = _parse_tolerant(text)
    except (HL7ParseError, hl7apy_exceptions.HL7apyException) as exc:
        raise AckParseError(f"not a readable HL7v2 message: {exc}") from exc

    encoding_chars = message.encoding_chars
    msh_9 = message.msh.msh_9
    message_type = _value(msh_9) or ""
    # MSH-9.1, the message code, read through hl7apy's element tree. Its component is named MSG_1
    # from 2.5 on and CM_MSG_1 in 2.3, so we take the first component rather than a fixed name.
    message_code = msh_9.children[0].to_er7() if msh_9 and msh_9.children else ""
    if message_code != "ACK":
        raise AckParseError(f"MSH-9 (message type) is {message_type!r}, not ACK")
    if not message.msa:
        raise AckParseError("ACK has no MSA segment")
    msa = message.msa

    code_text = _value(msa.msa_1) or ""
    try:
        code = AckCode(code_text)
    except ValueError as exc:
        raise AckParseError(f"MSA-1 {code_text!r} is not an HL7 acknowledgment code") from exc

    reason = _value(msa.msa_3)
    return Ack(
        code=code,
        acknowledged_control_id=_value(msa.msa_2),
        reason=decode_escapes(reason, encoding_chars) if reason else None,
        message_type=message_type,
        ack_control_id=_value(message.msh.msh_10),
        version=_value(message.msh.msh_12),
        raw=raw,
    )
