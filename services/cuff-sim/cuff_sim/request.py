"""The request body: two bounded integers, a transport and optionally a sample NAME.

Body (the dashboard's ``CuffReadingRequest``, ``web/src/api/contract.ts``, forwarded unchanged by
``fhir-proxy``, D10)::

    {"systolic": 120, "diastolic": 80, "transport": "mllp", "sendMalformed": null}

- ``systolic`` and ``diastolic``: JSON integers (not ``120.0``, not ``"120"``, not ``true``),
  each 1 to 400 mm[Hg] (T07's builder), systolic above diastolic. Required unless
  ``sendMalformed`` is set; then they are ignored, but must still be valid if present.
- ``transport``: ``"mllp"`` (default when absent) or ``"https"``.
- ``sendMalformed``: ``null``/absent, or the name of a T07 malformed sample that applies to the
  transport (``GET /cuff/samples`` lists them).
- Any other key is refused.

No text from the caller reaches a message: the only strings accepted are a transport from a
fixed pair and a sample name from T07's manifest, and both select fixed content. Error messages
name the field and never repeat what the caller sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cuff_sim.message import MAX_MMHG, MIN_MMHG, TRANSPORTS, find_sample, malformed_samples

FIELDS = ("systolic", "diastolic", "transport", "sendMalformed")


@dataclass(frozen=True)
class SendRequest:
    transport: str
    systolic: int | None = None
    diastolic: int | None = None
    sample_name: str | None = None


@dataclass
class RequestError(ValueError):
    """Refused input. ``status`` is 400 (not a JSON object) or 422 (a field is wrong)."""

    status: int
    issues: list[tuple[str, str]] = field(default_factory=list)  # (field, sentence)

    def __str__(self) -> str:
        return "; ".join(f"{name}: {text}" for name, text in self.issues)


def _pressure(body: dict, name: str, issues: list[tuple[str, str]]) -> int | None:
    value = body.get(name)
    if value is None:
        return None
    # bool is a subclass of int in Python (and true/false are not pressures).
    if isinstance(value, bool) or not isinstance(value, int):
        issues.append((name, "must be a whole number of mm[Hg]"))
        return None
    if not MIN_MMHG <= value <= MAX_MMHG:
        issues.append((name, f"must be from {MIN_MMHG} to {MAX_MMHG} mm[Hg]"))
        return None
    return value


def parse_request(body: object) -> SendRequest:
    if not isinstance(body, dict):
        raise RequestError(400, [("body", "must be a JSON object")])
    issues: list[tuple[str, str]] = []
    if any(k not in FIELDS for k in body):
        issues.append(("body", f"only these fields are accepted: {', '.join(FIELDS)}"))

    transport = body.get("transport", "mllp")
    if transport not in TRANSPORTS:
        issues.append(("transport", 'must be "mllp" or "https"'))
        transport = None

    sample_name = body.get("sendMalformed")
    if sample_name is not None:
        known = {s.name for s in malformed_samples()}
        if not isinstance(sample_name, str) or sample_name not in known:
            issues.append(("sendMalformed", "must be the name of a malformed sample, or null"))
            sample_name = None
        elif transport is not None and find_sample(sample_name, transport) is None:
            issues.append(("sendMalformed", f"this sample cannot be sent over {transport}"))
            sample_name = None

    systolic = _pressure(body, "systolic", issues)
    diastolic = _pressure(body, "diastolic", issues)
    if body.get("sendMalformed") is None:
        for name in ("systolic", "diastolic"):
            if body.get(name) is None:
                issues.append((name, "is required"))
    if systolic is not None and diastolic is not None and systolic <= diastolic:
        issues.append(("systolic", "must be greater than diastolic"))

    if issues:
        raise RequestError(422, issues)
    return SendRequest(
        transport=transport, systolic=systolic, diastolic=diastolic, sample_name=sample_name
    )
