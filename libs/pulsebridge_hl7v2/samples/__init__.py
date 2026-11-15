"""Synthetic HL7v2 sample messages, one or more per malformed-message class in D7.

The samples are defined in ``samples.json`` next to this file, one entry per sample with its
segments, its segment terminator, its MLLP framing and the outcome expected at every layer. See
``README.md`` in this directory for why they are JSON rather than raw ``.hl7`` files.

    from pulsebridge_hl7v2.samples import load, by_class
    sample = load("unparseable_no_msh")
    sample.payload      # the exact message bytes (what HTTPS ingest would carry)
    sample.wire_bytes   # the exact bytes to write to the MLLP socket, framing included
    sample.expected.commit_ack          # "AE"
    sample.with_patient(patient)        # the same sample with its PID rebuilt for ``patient``
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import cache
from importlib.resources import files

from hl7apy import exceptions as hl7apy_exceptions
from hl7apy.consts import VALIDATION_LEVEL
from hl7apy.core import Segment
from hl7apy.parser import get_message_info, parse_segments
from pulsebridge_hl7v2.mllp import END_BLOCK, START_BLOCK, frame
from pulsebridge_hl7v2.oru import HL7_VERSION, Patient, check_patient, fill_pid
from pulsebridge_hl7v2.parsing import SEGMENT_TERMINATOR

# D7 classes, in the order a message meets the layers.
CLASSES = (
    "valid",  # accepted everywhere
    "structurally_invalid",  # caught by hl7apy validation before sending
    "unparseable",  # rejected by the HL7v2 store: commit-level AE
    "no_acknowledgment",  # bad MLLP framing: the adapter never replies
    "parses_but_cannot_map",  # stored with AA, then the mapper records an error outcome
)

# How the payload is put on the MLLP wire. Only "standard" is a correct frame.
_FRAMINGS = {
    "standard": frame,
    "missing_end_block": lambda p: START_BLOCK + p,
    "missing_final_cr": lambda p: START_BLOCK + p + END_BLOCK,
}
_TERMINATORS = {"\r", "\n", "\r\n"}
EVIDENCE_KINDS = ("observed in S4", "observed in S5", "inferred:")


@dataclass(frozen=True)
class Expected:
    """The outcome expected at each layer. ``None`` means the layer is never reached.

    - ``hl7apy_validation``: "pass" or "fail" for ``validate_oru_r01``.
    - ``hl7apy_parse``: "ok" or "error" for ``parse_er7`` (tolerant parsing, as the mapper does).
    - ``commit_ack``: MSA-1 from the HL7v2 store ("AA", "AE"), or ``None`` for no reply.
    - ``commit_ack_reason``: MSA-3 as recorded in spike S4/S5 (escape sequences decoded).
    - ``no_ack``: why there is no ACK ("timeout"), when ``commit_ack`` is ``None``.
    - ``application_outcome``: the mapper's result, "accepted" or "error" (T12).
    - ``mapping_reason``: suggested reason code for the label (S5 style); T12 owns the final set.
    """

    hl7apy_validation: str
    hl7apy_parse: str
    commit_ack: str | None
    commit_ack_reason: str | None
    no_ack: str | None
    application_outcome: str | None
    mapping_reason: str | None


@dataclass(frozen=True)
class Sample:
    name: str
    sample_class: str
    description: str
    # Per transport ("mllp", "https"): where the expected store outcome comes from. Each text
    # starts with one of EVIDENCE_KINDS; "inferred:" marks expectations no spike observed.
    evidence: dict[str, str]
    segments: tuple[str, ...]
    segment_terminator: str
    terminate_last_segment: bool
    mllp_framing: str
    transports: tuple[str, ...]  # "mllp", "https": where the sample makes sense to send
    expected: Expected

    @property
    def payload(self) -> bytes:
        """The message bytes, exactly as they should reach the store."""
        text = self.segment_terminator.join(self.segments)
        if self.terminate_last_segment:
            text += self.segment_terminator
        return text.encode("ascii")

    @property
    def wire_bytes(self) -> bytes:
        """The bytes to write to an MLLP socket, with this sample's (possibly broken) framing."""
        return _FRAMINGS[self.mllp_framing](self.payload)

    # ---- Patient substitution (T33) ---------------------------------------------------------

    def _pid_position(self) -> tuple[int, dict[str, str]] | None:
        """Where this sample's PID segment is, and the separators to rewrite it with; ``None``
        when the PID must not or cannot be replaced (see :meth:`with_patient`).

        Everything is read by hl7apy: the separators from MSH-1 and MSH-2, the segments from
        the whole payload, the identifier from PID-3.1.
        """
        text = self.payload.decode("ascii")
        try:
            # hl7apy reads the separators from MSH-1 and MSH-2 (no MSH: ParserError) and splits
            # the segments on carriage returns. With line feeds between segments it finds one
            # segment, or segments named "\nPI": either way none is named PID.
            encoding_chars, _structure, _version = get_message_info(text)
            # Read as 2.5.1 whatever MSH-12 says ("9.9", empty): the PID segment has the same
            # shape in every 2.x version, and the defect of those samples is MSH-12 itself.
            segments = parse_segments(
                text.rstrip(SEGMENT_TERMINATOR),
                version=HL7_VERSION,
                encoding_chars=encoding_chars,
                validation_level=VALIDATION_LEVEL.TOLERANT,
            )
        except (hl7apy_exceptions.HL7apyException, IndexError):
            # HL7apyException: hl7apy refused the header or a segment. IndexError: hl7apy 1.3.5
            # raises it for a five-character MSH-2 with no MSH-12 (see pulsebridge_hl7v2.ack).
            return None
        positions = [i for i, segment in enumerate(segments) if segment.name == "PID"]
        if len(positions) != 1:
            return None  # no PID (MSH only, bare MSH), or more than one: nothing to replace
        index = positions[0]
        pid = segments[index]
        if index >= len(self.segments) or (
            pid.to_er7(encoding_chars=encoding_chars) != self.segments[index]
        ):
            # hl7apy does not read this PID back as the manifest segment at the same position:
            # either it split the message differently (so the positions do not line up), or
            # the PID's fields are not what they seem (for example "PID#1##..." under MSH-1
            # "|"), so the PID carries the defect, which must not be repaired.
            return None
        if not pid.pid_3.cx_1.to_er7():
            # No identifier in PID-3.1 (invalid_missing_pid3): the missing identifier IS the
            # defect, so there is no identifier to replace.
            return None
        return index, encoding_chars

    @property
    def accepts_patient(self) -> bool:
        """Whether :meth:`with_patient` replaces this sample's PID (``False``: it returns the
        sample unchanged)."""
        return self._pid_position() is not None

    def with_patient(self, patient: Patient) -> Sample:
        """This sample with its PID segment rebuilt for ``patient``, built with hl7apy.

        Only the PID segment changes: it is rebuilt by the same code the ORU^R01 builder uses
        (PID-1 set ID, PID-3 identifier with authority and type, PID-5 family and given name)
        and written with the sample's own separators. Every other segment, the segment
        terminators, the MLLP framing and so the sample's deliberate defect are kept byte for
        byte; the name, class and expected outcomes are unchanged.

        The sample is returned **unchanged** when its PID must not or cannot be replaced:

        - it cannot be read into segments by hl7apy: no MSH to declare the separators
          (``unparseable_no_msh``), line feeds between segments
          (``unparseable_lf_terminators``, ``unparseable_crlf_terminators``), or a header hl7apy
          refuses (``unparseable_separator_mismatch``: MSH-1 says ``|`` but the fields use
          ``#``, so MSH-2 runs on into the header and repeats characters);
        - hl7apy does not read its PID back as the same bytes, so the PID's fields are not what
          they seem (a PID written with other separators than MSH declares; no shipped sample);
        - its PID has no PID-3.1 identifier: the defect is the PID (``invalid_missing_pid3``);
        - it has no PID segment (``unmappable_msh_only``, ``unmappable_bare_msh``).

        ``accepts_patient`` says which case applies. ``patient`` is checked as the builder checks
        it (``ValueError`` for text that cannot go into a PID field), whatever the sample.
        """
        if not isinstance(patient, Patient):
            raise TypeError(f"patient must be a Patient, got {type(patient).__name__}")
        check_patient(patient)
        position = self._pid_position()
        if position is None:
            return self
        index, encoding_chars = position
        pid = Segment("PID", version=HL7_VERSION, validation_level=VALIDATION_LEVEL.STRICT)
        fill_pid(pid, patient)
        segments = list(self.segments)
        segments[index] = pid.to_er7(encoding_chars=encoding_chars)
        return replace(self, segments=tuple(segments))


def _sample_from_json(raw: dict) -> Sample:
    sample = Sample(
        name=raw["name"],
        sample_class=raw["class"],
        description=raw["description"],
        evidence=dict(raw["evidence"]),
        segments=tuple(raw["segments"]),
        segment_terminator=raw["segment_terminator"],
        terminate_last_segment=raw["terminate_last_segment"],
        mllp_framing=raw["mllp_framing"],
        transports=tuple(raw["transports"]),
        expected=Expected(**raw["expected"]),
    )
    if sample.sample_class not in CLASSES:
        raise ValueError(f"sample {sample.name}: unknown class {sample.sample_class!r}")
    if sample.mllp_framing not in _FRAMINGS:
        raise ValueError(f"sample {sample.name}: unknown framing {sample.mllp_framing!r}")
    if sample.segment_terminator not in _TERMINATORS:
        raise ValueError(f"sample {sample.name}: unknown terminator")
    if set(sample.evidence) != set(sample.transports):
        raise ValueError(f"sample {sample.name}: evidence must name exactly its transports")
    for transport, text in sample.evidence.items():
        if not text.startswith(EVIDENCE_KINDS):
            raise ValueError(
                f"sample {sample.name}: {transport} evidence must start with one of "
                f"{EVIDENCE_KINDS}"
            )
    return sample


@cache
def all_samples() -> tuple[Sample, ...]:
    """Every sample, in manifest order."""
    data = json.loads(files(__name__).joinpath("samples.json").read_text(encoding="ascii"))
    return tuple(_sample_from_json(raw) for raw in data["samples"])


def names() -> tuple[str, ...]:
    return tuple(s.name for s in all_samples())


def load(name: str) -> Sample:
    """The sample called ``name``. Raises ``KeyError`` with the valid names if unknown."""
    for sample in all_samples():
        if sample.name == name:
            return sample
    raise KeyError(f"no sample named {name!r}; known: {', '.join(names())}")


def by_class(sample_class: str) -> tuple[Sample, ...]:
    """Every sample of one D7 class (see ``CLASSES``)."""
    if sample_class not in CLASSES:
        raise ValueError(f"unknown class {sample_class!r}; known: {', '.join(CLASSES)}")
    return tuple(s for s in all_samples() if s.sample_class == sample_class)


__all__ = [
    "CLASSES",
    "EVIDENCE_KINDS",
    "Expected",
    "Sample",
    "all_samples",
    "by_class",
    "load",
    "names",
]
