"""Synthetic HL7v2 sample messages, one or more per malformed-message class in D7.

The samples are defined in ``samples.json`` next to this file, one entry per sample with its
segments, its segment terminator, its MLLP framing and the outcome expected at every layer. See
``README.md`` in this directory for why they are JSON rather than raw ``.hl7`` files.

    from pulsebridge_hl7v2.samples import load, by_class
    sample = load("unparseable_no_msh")
    sample.payload      # the exact message bytes (what HTTPS ingest would carry)
    sample.wire_bytes   # the exact bytes to write to the MLLP socket, framing included
    sample.expected.commit_ack          # "AE"
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from importlib.resources import files

from pulsebridge_hl7v2.mllp import END_BLOCK, START_BLOCK, frame

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
