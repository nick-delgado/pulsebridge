"""Parse ER7 bytes into an hl7apy ``Message``, with clear errors where hl7apy's are misleading.

ER7 is HL7v2's pipe-delimited text encoding ("MSH|^~\\&|..."). All the real parsing is done by
hl7apy (decision D3). This module adds only:

- byte decoding and three pre-checks (the message starts with ``MSH``; segments are separated by
  carriage returns; MSH-1 and MSH-2 are five distinct delimiter characters), because hl7apy's
  own error for those cases is misleading or missing: a message with line-feed separators fails
  with ``UnsupportedVersion``, since the whole message becomes one segment and MSH-12 then runs
  on into the next line, and two header shapes make it raise a plain ``IndexError`` (below);
- :func:`message_info`, hl7apy's header reader with its one known crash turned into
  :class:`HL7ParseError`, so :func:`parse_er7` (and the ACK parser and the validator, which use
  these too) raise only this package's errors for any bytes;
- :func:`decode_escapes`, because hl7apy 1.3.5 escapes values when it writes ER7 but has no
  function to turn escape sequences back into text when it reads them.
"""

from __future__ import annotations

import re

from hl7apy import exceptions as hl7apy_exceptions
from hl7apy.consts import VALIDATION_LEVEL
from hl7apy.core import Message
from hl7apy.parser import get_message_info, parse_message

# HL7v2 2.5.1 chapter 2.5.4 / 2.7: segments are terminated by a carriage return (hex 0D).
SEGMENT_TERMINATOR = "\r"

# MSH-1 (field separator) is the character right after "MSH", and MSH-2 (encoding characters)
# starts right after it: component, repetition, escape, subcomponent (2.5.1 sections 2.15.9.1 and
# 2.15.9.2). So the first five delimiters sit at fixed positions 3 to 7 of every header; reading
# them by position is how hl7apy itself finds MSH-1, not ER7 splitting (D3).
_DELIMITERS = slice(3, 8)


class HL7ParseError(ValueError):
    """The bytes could not be read as an HL7v2 message. ``reason`` is a one-line explanation."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def decode_payload(payload: bytes | str) -> str:
    """Return the message text. HL7v2 without MSH-18 (character set) means ASCII; we accept UTF-8,
    which is a superset, and fail clearly on anything else."""
    if isinstance(payload, str):
        return payload
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HL7ParseError(f"message is not valid UTF-8 text: {exc}") from exc


def check_framing_text(text: str) -> None:
    """Pre-checks that hl7apy reports misleadingly. Raises :class:`HL7ParseError`."""
    if not text:
        raise HL7ParseError("message is empty")
    # Every HL7v2 message starts with the MSH (message header) segment; MSH-1 and MSH-2, the
    # characters right after "MSH", declare the separators used by the rest of the message.
    if not text.startswith("MSH"):
        first = text.split(SEGMENT_TERMINATOR, 1)[0][:3]
        raise HL7ParseError(f"message does not start with an MSH segment (starts with {first!r})")
    if "\n" in text:
        raise HL7ParseError(
            "message contains line feeds (\\n); HL7v2 requires each segment to end with a "
            "carriage return (\\r) only, and the HL7v2 store rejects \\n and \\r\\n"
        )
    check_delimiters(text)


def check_delimiters(text: str) -> None:
    """Refuse a header whose delimiters HL7 does not allow, before hl7apy reads it.

    HL7 2.5.1 section 2.5.4: the field separator and the four encoding characters are single,
    distinct characters, and none may be a letter, a digit or whitespace (the segment terminator
    is a carriage return, and ER7 data is text). hl7apy 1.3.5 checks that they are distinct, but
    it accepts whitespace (and non-ASCII) as an encoding character, and when MSH-2 ends the header
    its segment parser strips trailing whitespace, so reading the separators raises
    ``IndexError`` (for example ``MSH|^~\\<space>``).
    """
    delimiters = text[_DELIMITERS]
    if (
        len(delimiters) < 5
        or len(set(delimiters)) < 5
        or any(not ("\x21" <= c <= "\x7e") or c.isalnum() for c in delimiters)
    ):
        raise HL7ParseError(
            "MSH-1 and MSH-2 must be five distinct delimiter characters (printable, not a letter, "
            "digit or space), such as |^~\\&"
        )


def message_info(text: str) -> tuple[dict, str | None, str | None]:
    """hl7apy's header reader: ``(encoding characters, message structure, version)``.

    hl7apy 1.3.5 ``parser._split_msh`` raises ``IndexError`` for a five-character MSH-2 (2.7
    added a fifth, the truncation character) in a header of fewer than twelve fields: it reads
    MSH-12 to decide whether that fifth character is allowed without checking MSH-12 exists
    (``MSH|^~\\&#|...|ACK|A|``, or ``MSH|^~\\&`` followed by a non-ASCII character that the
    delimiter check above has not already refused). That ``IndexError`` becomes
    :class:`HL7ParseError`; hl7apy's own exceptions are left to the caller.
    """
    try:
        return get_message_info(text)
    except IndexError as exc:
        # Nothing else in _split_msh or get_message_info indexes past MSH-2.
        raise HL7ParseError(
            "MSH-2 has five encoding characters, which only HL7 2.7 and later allow, and the "
            "header has no MSH-12 (version) to say so"
        ) from exc


def parse_er7(payload: bytes | str, *, strict: bool = False) -> Message:
    """Parse ER7 text with hl7apy and group segments by the message structure (``find_groups``).

    ``strict=False`` (hl7apy's TOLERANT level) is right for reading what someone else sent, such as
    an ACK or a stored message: it keeps unknown or odd content instead of refusing it. Use
    :func:`pulsebridge_hl7v2.validation.validate_oru_r01` to check a message before sending.
    """
    text = decode_payload(payload)
    check_framing_text(text)
    level = VALIDATION_LEVEL.STRICT if strict else VALIDATION_LEVEL.TOLERANT
    try:
        message_info(text)  # parse_message reads the header the same way: fail here, clearly
        return parse_message(text, validation_level=level, find_groups=True)
    except HL7ParseError:
        raise  # from message_info, already one of ours (it is a ValueError: keep it as it is)
    except hl7apy_exceptions.UnsupportedVersion as exc:
        # MSH-12 (version ID) names a version hl7apy has no definitions for, e.g. "9.9".
        raise HL7ParseError(f"MSH-12 version is not supported by hl7apy: {exc}") from exc
    except hl7apy_exceptions.HL7apyException as exc:
        raise HL7ParseError(f"hl7apy could not parse the message: {exc}") from exc
    except ValueError as exc:
        # At STRICT level hl7apy checks base data types while parsing and raises a plain
        # ValueError, e.g. "2026-11-13 is not an HL7 valid date value" for a DTM field.
        raise HL7ParseError(f"a field value does not match its HL7 data type: {exc}") from exc


# HL7v2 2.5.1 section 2.7.4 escape sequences inside a text field, written with the escape
# character declared in MSH-2 (normally "\"): \F\ field separator, \S\ component separator,
# \T\ subcomponent separator, \R\ repetition separator, \E\ the escape character itself.
# Formatting (\H\, \N\, \.br\ ...) and hex/charset sequences (\X..\, \C..\, \M..\) are left as
# they are: they have no plain-text equivalent.
_ESCAPE_SEQUENCE_KEYS = {"F": "FIELD", "S": "COMPONENT", "T": "SUBCOMPONENT", "R": "REPETITION"}


def decode_escapes(value: str, encoding_chars: dict[str, str]) -> str:
    """Turn HL7v2 escape sequences in a parsed field value back into the characters they stand for.

    ``encoding_chars`` is hl7apy's separator dictionary (``message.encoding_chars``), so a message
    that declares unusual separators in MSH-1/MSH-2 is decoded with its own characters.
    """
    esc = encoding_chars["ESCAPE"]
    pattern = re.compile(f"{re.escape(esc)}([FSTRE]){re.escape(esc)}")

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return esc if key == "E" else encoding_chars[_ESCAPE_SEQUENCE_KEYS[key]]

    return pattern.sub(replace, value)
