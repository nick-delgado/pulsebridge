"""Typed results returned by :class:`pulsebridge_gcp.HealthcareClient`."""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field

from pulsebridge_gcp.errors import MalformedResponseError

# Documented label limits for Cloud Healthcare resources (spike S5 found they are not enforced
# today, so we enforce them ourselves): at most 64 labels; keys 1 to 63 bytes, starting with a
# lowercase letter, then lowercase letters, digits, "_" or "-"; values up to 63 bytes of
# lowercase letters, digits, "_" or "-". An empty value removes that key when patched.
MAX_LABELS = 64
LABEL_KEY = re.compile(r"[a-z][a-z0-9_-]{0,62}", re.ASCII)
LABEL_VALUE = re.compile(r"[a-z0-9_-]{0,63}", re.ASCII)


def check_labels(labels: dict[str, str]) -> None:
    """Raise ``ValueError`` unless ``labels`` keeps to the documented label limits."""
    if not isinstance(labels, dict):
        raise ValueError("labels must be a dict of str to str")
    if len(labels) > MAX_LABELS:
        raise ValueError(f"at most {MAX_LABELS} labels are allowed, got {len(labels)}")
    for key, value in labels.items():
        if not isinstance(key, str) or not LABEL_KEY.fullmatch(key):
            raise ValueError(f"label key {key!r} is not 1-63 lowercase letters, digits, _ or -")
        if not isinstance(value, str) or not LABEL_VALUE.fullmatch(value):
            raise ValueError(
                f"label {key!r}: value {value!r} is not 0-63 lowercase letters, digits, _ or -"
            )


def decode_base64_field(operation: str, name: str, value: object) -> bytes:
    """Decode a base64 ``bytes`` field of an API response, or raise MalformedResponseError."""
    if not isinstance(value, str):
        raise MalformedResponseError(operation, 200, server_message=f"{name} is not a string")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MalformedResponseError(
            operation, 200, server_message=f"{name} is not valid base64"
        ) from exc


@dataclass(frozen=True)
class PatientId:
    """One ``patientIds`` entry the store reads from PID-3.

    ``value`` is PID-3.1 and ``type`` is PID-3.5 (identifier type code)."""

    value: str
    type: str | None


@dataclass(frozen=True)
class Hl7v2Message:
    """A stored HL7v2 message (REST resource ``Message``), as far as the requested view shows it.

    - ``name``: full resource name, ``projects/.../hl7V2Stores/<store>/messages/<id>``.
    - ``data``: the message bytes exactly as sent (decoded from base64); ``None`` in the ``BASIC``
      and ``PARSED_ONLY`` views.
    - ``message_type`` (MSH-9.1 only, e.g. ``ORU``), ``send_facility`` (MSH-4), ``send_time``
      (MSH-7, as the store read it), ``create_time`` (the store's clock), ``patient_ids``
      (every PID-3 repetition): metadata the store extracted; absent fields are ``None``.
    - ``labels``: ``{}`` when the message has none.
    - ``raw``: the JSON object as returned, for anything not modelled here.
    """

    name: str
    data: bytes | None = None
    message_type: str | None = None
    send_facility: str | None = None
    send_time: str | None = None
    create_time: str | None = None
    patient_ids: tuple[PatientId, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, operation: str, obj: object) -> Hl7v2Message:
        if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
            raise MalformedResponseError(operation, 200, server_message="no message name")
        data = None
        if "data" in obj:
            data = decode_base64_field(operation, "data", obj["data"])
        labels = obj.get("labels") or {}
        if not isinstance(labels, dict):
            raise MalformedResponseError(operation, 200, server_message="labels is not an object")
        patient_ids = tuple(
            PatientId(value=p.get("value", ""), type=p.get("type"))
            for p in obj.get("patientIds", [])
            if isinstance(p, dict)
        )
        return cls(
            name=obj["name"],
            data=data,
            message_type=obj.get("messageType"),
            send_facility=obj.get("sendFacility"),
            send_time=obj.get("sendTime"),
            create_time=obj.get("createTime"),
            patient_ids=patient_ids,
            labels={str(k): str(v) for k, v in labels.items()},
            raw=obj,
        )


@dataclass(frozen=True)
class MessagePage:
    """One page of ``messages.list``. ``next_page_token`` is ``None`` on the last page."""

    messages: tuple[Hl7v2Message, ...]
    next_page_token: str | None


@dataclass(frozen=True)
class IngestResult:
    """A message the HL7v2 store accepted through ``messages:ingest``.

    ``hl7_ack`` is the commit-level ACK in ER7 (decoded from base64; no MLLP frame and no
    trailing carriage return, spike S5). A rejected message raises
    :class:`pulsebridge_gcp.ClientError` with ``hl7_nack`` set instead.
    """

    hl7_ack: bytes
    message: Hl7v2Message
