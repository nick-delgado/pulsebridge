"""Plain-Python inputs to the builders.

Callers (``health-sync``, ``v2-to-fhir``, the seeder, the connect script, the share to OpenEMR)
convert whatever they received (a Google Health JSON window, an HL7v2 segment, configuration)
into these dataclasses. Each one validates itself when constructed, so a builder never sees a
half-valid input and every problem surfaces as a ``FhirInputError`` naming the field.

Text fields are normalised to Unicode NFC on the way in, so the same name typed or encoded two
ways builds the same resource and the same key.
"""

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime

from pulsebridge_fhir import codes
from pulsebridge_fhir.errors import FhirInputError

# Google documents heart rate as 1 to 300 beats per minute (spike S3); the same range is applied to
# every heart-rate value, so a sensor glitch or a unit mix-up (for example Hz) is rejected.
HEART_RATE_RANGE = (1, 300)
# Plausibility bounds for a cuff reading in mm[Hg]. Wider than the simulator's input bounds (T13),
# narrow enough to catch a kPa value (about 16) or a swapped field.
SYSTOLIC_RANGE = (50, 300)
DIASTOLIC_RANGE = (20, 200)

# FHIR administrative-gender (required binding on Patient.gender).
GENDERS = ("male", "female", "other", "unknown")

# Observation.status values that make sense for a measurement we write (R4 observation-status).
OBSERVATION_STATUSES = ("preliminary", "final", "amended", "corrected")

_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _nfc(obj: object, name: str) -> None:
    """Normalise a str field (or a tuple of str) of a frozen dataclass to NFC, in place."""
    value = getattr(obj, name)
    if isinstance(value, str):
        object.__setattr__(obj, name, unicodedata.normalize("NFC", value))
    elif isinstance(value, tuple) and all(isinstance(v, str) for v in value):
        object.__setattr__(obj, name, tuple(unicodedata.normalize("NFC", v) for v in value))


def _require_text(where: str, name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FhirInputError(f"{where}: {name} is missing or empty")
    if value != value.strip():
        raise FhirInputError(f"{where}: {name} has leading or trailing whitespace")
    return value


def _require_aware(where: str, name: str, value: object) -> datetime:
    if value is None:
        raise FhirInputError(f"{where}: {name} is missing")
    if not isinstance(value, datetime):
        raise FhirInputError(f"{where}: {name} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        # A FHIR dateTime with a time part must carry a time-zone offset (R4 datatypes.html).
        raise FhirInputError(f"{where}: {name} has no time zone; FHIR needs an offset")
    return value


def _require_number(where: str, name: str, value: object, low: float, high: float) -> int | float:
    # bool is a subclass of int in Python; True must not pass as 1 beat per minute.
    if value is None:
        raise FhirInputError(f"{where}: {name} is missing")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FhirInputError(f"{where}: {name} must be a number, got {type(value).__name__}")
    # Only floats can be NaN or infinite. Calling math.isfinite on a huge int (a JSON 10**400)
    # would raise OverflowError, so ints go straight to the range check, which handles any size.
    if isinstance(value, float) and not math.isfinite(value):
        raise FhirInputError(f"{where}: {name} must be a finite number")
    if not low <= value <= high:
        # str() of an int over 4300 digits raises ValueError in Python 3.11+, so huge ints are
        # described rather than printed.
        huge = isinstance(value, int) and abs(value) >= 10**20
        shown = "a number of 20 or more digits" if huge else value
        raise FhirInputError(f"{where}: {name} ({shown}) is outside {low} to {high}")
    return value


@dataclass(frozen=True)
class PatientInput:
    """The person the data is about.

    Only ``patient_id`` is always required. It is PulseBridge's identifier for the person and
    becomes the ``PT``-typed identifier: the key every write matches on, and what OpenEMR searches
    by. It must stay the same for the life of the deployment, and comes from configuration (or
    PID-3 on the HL7v2 path), never from code.

    Everything else is optional here, because the ingest paths only know part of it: the cuff
    message carries PID-3 and PID-5 (identifier and name) but no birth date or sex. The full set
    (official name, gender, birth date) is enforced where it is needed, by
    :func:`pulsebridge_fhir.check_complete_patient`: when the setup step writes the Patient record,
    and before a share to OpenEMR (which rejects a Patient without them, spike S1).
    """

    patient_id: str
    family: str | None = None
    given: tuple[str, ...] = ()
    gender: str | None = None
    birth_date: date | None = None
    google_health_user_id: str | None = None

    def __post_init__(self) -> None:
        where = "patient"
        for name in ("patient_id", "family", "given", "google_health_user_id"):
            _nfc(self, name)
        _require_text(where, "patient_id", self.patient_id)
        if self.family is not None:
            _require_text(where, "family", self.family)
        if isinstance(self.given, str) or not isinstance(self.given, tuple):
            raise FhirInputError(f"{where}: given must be a tuple of names, e.g. ('Avery',)")
        for name in self.given:
            _require_text(where, "given name", name)
        if self.gender is not None and self.gender not in GENDERS:
            raise FhirInputError(
                f"{where}: gender {self.gender!r} is not one of {', '.join(GENDERS)}"
            )
        if self.birth_date is not None and (
            not isinstance(self.birth_date, date) or isinstance(self.birth_date, datetime)
        ):
            raise FhirInputError(f"{where}: birth_date must be a date")
        if self.google_health_user_id is not None:
            _require_text(where, "google_health_user_id", self.google_health_user_id)


@dataclass(frozen=True)
class DeviceInput:
    """The device that took the measurement.

    Google supplies only ``dataSource.device.displayName`` (for example ``"Pixel Watch 4"``) and no
    serial number, so the Device identifier is derived from the patient and this name (spike S3).
    On the HL7v2 path the name comes from the sending application.
    """

    display_name: str
    manufacturer: str | None = None

    def __post_init__(self) -> None:
        _nfc(self, "display_name")
        _nfc(self, "manufacturer")
        _require_text("device", "display_name", self.display_name)
        if self.manufacturer is not None:
            _require_text("device", "manufacturer", self.manufacturer)


@dataclass(frozen=True)
class HeartRateWindow:
    """One heart-rate roll-up window: minimum, average and maximum beats per minute.

    ``start`` and ``end`` are the window bounds as instants (Google sends UTC, in whole seconds).
    Sub-second bounds are rejected: the key is built from the start in whole seconds, so allowing
    fractions would let two different windows share a key. Build one from the Google Health API
    JSON with :func:`pulsebridge_fhir.google_health.window_from_rollup`.
    """

    start: datetime
    end: datetime
    bpm_min: int | float
    bpm_avg: int | float
    bpm_max: int | float

    def __post_init__(self) -> None:
        where = "heart-rate window"
        for name in ("start", "end"):
            value = _require_aware(where, name, getattr(self, name))
            if value.microsecond:
                raise FhirInputError(f"{where}: {name} ({value}) must be a whole second")
        if self.end <= self.start:
            raise FhirInputError(f"{where}: end ({self.end}) is not after start ({self.start})")
        low, high = HEART_RATE_RANGE
        _require_number(where, "minimum", self.bpm_min, low, high)
        _require_number(where, "average", self.bpm_avg, low, high)
        _require_number(where, "maximum", self.bpm_max, low, high)
        if not self.bpm_min <= self.bpm_avg <= self.bpm_max:
            raise FhirInputError(
                f"{where}: expected minimum <= average <= maximum, got "
                f"{self.bpm_min} / {self.bpm_avg} / {self.bpm_max}"
            )

    @property
    def start_utc(self) -> str:
        """Window start in UTC, ``YYYY-MM-DDTHH:MM:SSZ``: the time part of the idempotency key."""
        return self.start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def end_utc(self) -> str:
        return self.end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class BloodPressureReading:
    """One cuff reading: systolic and diastolic pressure at one time.

    ``source_key`` identifies the reading at its source and is the idempotency key, so a
    redelivered message maps to the same Observation. On the HL7v2 path use the sending
    application and the message control ID (MSH-3 and MSH-10). ``unit`` must be the UCUM code
    ``mm[Hg]``; ``v2-to-fhir`` passes OBX-6 here so a wrong unit is rejected, not relabelled.
    """

    effective: datetime
    systolic: int | float
    diastolic: int | float
    source_key: str
    unit: str = codes.UCUM_MM_HG

    def __post_init__(self) -> None:
        where = "blood-pressure reading"
        _nfc(self, "source_key")
        _require_aware(where, "effective", self.effective)
        _require_text(where, "source_key", self.source_key)
        if self.unit != codes.UCUM_MM_HG:
            raise FhirInputError(
                f"{where}: unit {self.unit!r} is not the UCUM code {codes.UCUM_MM_HG!r}"
            )
        _require_number(where, "systolic", self.systolic, *SYSTOLIC_RANGE)
        _require_number(where, "diastolic", self.diastolic, *DIASTOLIC_RANGE)
        if self.systolic <= self.diastolic:
            raise FhirInputError(
                f"{where}: systolic ({self.systolic}) must be greater than "
                f"diastolic ({self.diastolic})"
            )


@dataclass(frozen=True, kw_only=True)
class SourceInfo:
    """Where a reading came from, recorded in its Provenance.

    - ``record_type`` and ``record_id`` (required): what the source record is and its id, for
      example ``"hl7v2-message"`` and the HL7v2 store message name, or
      ``"google-health-heart-rate-rollup"`` and the window bounds. They let the trace panel find
      the original.
    - ``platform``: the upstream platform, for example Google's ``dataSource.platform``
      (``"FITBIT"``) or ``"HL7V2_STORE"`` for the cuff path. Upper-case token, or ``None`` when the
      caller does not know it; then the Provenance names no platform rather than guess one.
    - ``recording_method``: for example Google's ``dataSource.recordingMethod``
      (``"PASSIVELY_MEASURED"``). Upper-case token, or ``None`` when unknown.

    Keyword-only, so a platform cannot be passed where a record id was meant.
    """

    record_type: str
    record_id: str
    platform: str | None
    recording_method: str | None

    def __post_init__(self) -> None:
        where = "source"
        for name in ("platform", "recording_method"):
            value = getattr(self, name)
            if value is None:
                continue
            _require_text(where, name, value)
            if not _TOKEN.match(value):
                raise FhirInputError(f"{where}: {name} {value!r} must be an upper-case token")
        _require_text(where, "record_type", self.record_type)
        if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", self.record_type):
            raise FhirInputError(
                f"{where}: record_type {self.record_type!r} must be lower-case words joined by '-'"
            )
        _require_text(where, "record_id", self.record_id)


# ----------------------------------------------------------------------------------------------
# One cuff send attempt (task T31), recorded as an AuditEvent
# ----------------------------------------------------------------------------------------------

TRANSPORTS = ("mllp", "https")
# MSA-1 acknowledgment codes (HL7 table 0008): original mode AA / AE / AR, enhanced-mode commit
# level CA / CE / CR. The HL7v2 store only ever returned AA and AE (spike S4).
ACCEPT_CODES = ("AA", "CA")
REJECT_CODES = ("AE", "AR", "CE", "CR")
ACK_CODES = ACCEPT_CODES + REJECT_CODES
# Why no acknowledgment arrived (spikes S4 and S5):
# - timeout: nothing came back before the read timeout (bad MLLP framing: the adapter waits);
# - connection-closed: the peer closed with no data (MLLP: the adapter's API call failed; HTTPS:
#   the connection dropped);
# - api-error: HTTPS only, an HTTP error with no hl7Nack in it (403, 404, 5xx, a 400 that is not
#   an HL7 reply). It always carries the HTTP status.
NO_ACK_REASONS = ("timeout", "connection-closed", "api-error")

# The longest reason kept from MSA-3. MSA-3 is an ST of 80 characters in v2.5.1, so a conforming
# reason is never cut; the store's longest reasons in S4/S5 are under 60. The raw ACK keeps the
# full text either way.
REASON_MAX = 200
# The ACK is two short segments (MSH, MSA); the S4/S5 ACKs are about 150 bytes.
RAW_ACK_MAX = 4096

# MSH-10 message control ID. T07 builds 1 to 20 of these characters; a little more length is
# allowed for other senders. No HL7 separators, no spaces.
_CONTROL_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# A T07 sample message name, e.g. unparseable_no_msh.
_SAMPLE_NAME = re.compile(r"^[a-z0-9]+([_-][a-z0-9]+)*$")
# A PulseBridge service name, as Cloud Run names services: lower case, digits and hyphens.
_SERVICE_NAME = re.compile(r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")
# A Cloud Healthcare HL7v2 store resource name. Project IDs are 6 to 30 characters, start with a
# letter and do not end with a hyphen; dataset and store IDs are letters, digits, '_', '.', '-'.
_HL7V2_STORE_NAME = re.compile(
    r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/locations/[a-z0-9-]{1,63}"
    r"/datasets/[A-Za-z0-9_.-]{1,256}/hl7V2Stores/[A-Za-z0-9_.-]{1,256}$"
)
# The id after /messages/ is the URL-safe base64 of the SHA-256 of the message (spike S5).
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}={0,2}$")


def sanitise_reason(value: object) -> str | None:
    """MSA-3 text from the receiver, made safe to store and show.

    The text comes from the HL7v2 store, not from a person, but it is still outside input. It is
    **cleaned, not rejected**, because rejecting it would lose the audit record of a real attempt
    (the raw ACK keeps the original bytes):

    1. normalised to Unicode NFC;
    2. every control, format, private-use, surrogate or unassigned character (Unicode category
       ``C*``) and every line or paragraph separator becomes U+FFFD (the replacement character),
       so a CR, a tab or a bidirectional override cannot break a log line or disguise the text,
       and the reader can see that something was replaced;
    3. leading and trailing whitespace is removed;
    4. text longer than ``REASON_MAX`` is cut to ``REASON_MAX - 1`` characters plus an ellipsis
       (U+2026).

    Empty after cleaning means no reason (``None``). Applying it twice gives the same result, so a
    reason read back from a stored AuditEvent validates to itself.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise FhirInputError(f"acknowledgment: reason must be text, got {type(value).__name__}")
    text = unicodedata.normalize("NFC", value)
    text = "".join(
        "\ufffd"  # U+FFFD REPLACEMENT CHARACTER
        if unicodedata.category(ch)[0] == "C" or unicodedata.category(ch) in ("Zl", "Zp")
        else ch
        for ch in text
    ).strip()
    if len(text) > REASON_MAX:
        text = unicodedata.normalize("NFC", text[: REASON_MAX - 1].rstrip() + "\u2026")
    return text or None


def _require_whole_milliseconds(where: str, name: str, value: object) -> datetime:
    """An aware datetime with no sub-millisecond part.

    Milliseconds, not whole seconds as for heart-rate windows: a resend can follow the first
    attempt within the same second, and the attempt's key includes the send time. A FHIR instant
    can carry the milliseconds exactly, so the stored time and the key never disagree.
    """
    value = _require_aware(where, name, value)
    if value.microsecond % 1000:
        raise FhirInputError(f"{where}: {name} ({value}) must be a whole millisecond")
    return value


@dataclass(frozen=True, kw_only=True)
class Acknowledged:
    """The receiver answered with an ACK (MLLP frame, or ``hl7Ack`` / ``hl7Nack`` over HTTPS).

    - ``code``: MSA-1 as parsed by the T07 ACK parser (this library does not parse HL7v2).
    - ``raw_ack``: the ACK message bytes without the MLLP frame (no ``0x0B`` start byte, no
      ``0x1C 0x0D`` end); over HTTPS, the base64-decoded ``hl7Ack`` or ``hl7Nack``. It must start
      with ``MSH``.
    - ``reason``: MSA-3, cleaned by :func:`sanitise_reason`. Optional: MSA-3 may be empty.
    """

    code: str
    raw_ack: bytes
    reason: str | None = None

    def __post_init__(self) -> None:
        where = "acknowledgment"
        if self.code not in ACK_CODES:
            raise FhirInputError(
                f"{where}: code {self.code!r} is not one of {', '.join(ACK_CODES)}"
            )
        # A str subclass such as T07's AckCode (a StrEnum) is stored as the plain code.
        object.__setattr__(self, "code", str.__str__(self.code))
        if not isinstance(self.raw_ack, bytes):
            raise FhirInputError(
                f"{where}: raw_ack must be bytes, got {type(self.raw_ack).__name__}"
            )
        if not 1 <= len(self.raw_ack) <= RAW_ACK_MAX:
            raise FhirInputError(f"{where}: raw_ack must be 1 to {RAW_ACK_MAX} bytes")
        if b"\x0b" in self.raw_ack or b"\x1c" in self.raw_ack:
            raise FhirInputError(f"{where}: raw_ack must not include the MLLP frame bytes")
        if not self.raw_ack.startswith(b"MSH"):
            raise FhirInputError(f"{where}: raw_ack does not start with an MSH segment")
        object.__setattr__(self, "reason", sanitise_reason(self.reason))

    @property
    def accepted(self) -> bool:
        return self.code in ACCEPT_CODES


@dataclass(frozen=True, kw_only=True)
class NotAcknowledged:
    """No ACK came back.

    - ``why``: one of ``NO_ACK_REASONS``.
    - ``http_status``: the HTTP status, required for ``api-error`` and allowed only there.
    """

    why: str
    http_status: int | None = None

    def __post_init__(self) -> None:
        where = "no acknowledgment"
        if self.why not in NO_ACK_REASONS:
            raise FhirInputError(
                f"{where}: why {self.why!r} is not one of {', '.join(NO_ACK_REASONS)}"
            )
        if self.why == "api-error":
            if self.http_status is None:
                raise FhirInputError(f"{where}: api-error needs the http_status")
            _require_number(where, "http_status", self.http_status, 400, 599)
            if not isinstance(self.http_status, int):
                raise FhirInputError(f"{where}: http_status must be a whole number")
        elif self.http_status is not None:
            raise FhirInputError(
                f"{where}: http_status is only for api-error; {self.why} got no HTTP response"
            )


@dataclass(frozen=True, kw_only=True)
class SendAttempt:
    """One attempt by the cuff simulator (T13) to send a message to the HL7v2 store.

    Exactly one ``result``: :class:`Acknowledged` or :class:`NotAcknowledged`, never a mix.

    - ``sent_at``: when the first byte was sent; time-zone aware, whole milliseconds.
    - ``transport``: ``mllp`` or ``https``.
    - ``control_id``: MSH-10 of the message sent. ``None`` only for a malformed sample that has no
      readable MSH-10 (for example ``unparseable_no_msh``).
    - ``patient_id``: PulseBridge's patient identifier (PID-3), the value T06 writes in
      ``Patient.identifier``. Validated by the same rule as :class:`PatientInput`.
    - ``agent``: the sending service's name, e.g. ``cuff-sim`` (configuration).
    - ``destination``: the HL7v2 store's resource name
      ``projects/<p>/locations/<l>/datasets/<d>/hl7V2Stores/<s>`` (configuration).
    - ``message_name``: the stored message's resource name from the ingest response; only with an
      accepting ACK (a rejected message is not stored), and it must be in ``destination``.
    - ``sample_name``: the T07 malformed sample sent when "send malformed" was used.

    There is deliberately no field for the message itself: an accepted message is in the HL7v2
    store, and a malformed one is a fixed synthetic sample identified by name.
    """

    sent_at: datetime
    transport: str
    control_id: str | None
    result: Acknowledged | NotAcknowledged
    patient_id: str
    agent: str
    destination: str
    message_name: str | None = None
    sample_name: str | None = None

    def __post_init__(self) -> None:
        where = "send attempt"
        _nfc(self, "patient_id")
        _require_whole_milliseconds(where, "sent_at", self.sent_at)
        if self.transport not in TRANSPORTS:
            raise FhirInputError(
                f"{where}: transport {self.transport!r} is not one of {', '.join(TRANSPORTS)}"
            )
        if self.sample_name is not None and (
            not isinstance(self.sample_name, str)
            or len(self.sample_name) > 64
            or not _SAMPLE_NAME.match(self.sample_name)
        ):
            raise FhirInputError(
                f"{where}: sample_name {self.sample_name!r} must be a sample name such as "
                "unparseable_no_msh"
            )
        if self.control_id is None:
            if self.sample_name is None:
                raise FhirInputError(
                    f"{where}: control_id is missing; only a malformed sample may lack one"
                )
        elif not isinstance(self.control_id, str) or not _CONTROL_ID.match(self.control_id):
            raise FhirInputError(
                f"{where}: control_id {self.control_id!r} must be 1 to 64 letters, digits, "
                "'.', '_' or '-'"
            )
        if not isinstance(self.result, Acknowledged | NotAcknowledged):
            raise FhirInputError(
                f"{where}: result must be Acknowledged or NotAcknowledged, "
                f"got {type(self.result).__name__}"
            )
        if (
            isinstance(self.result, NotAcknowledged)
            and self.result.why == "api-error"
            and self.transport != "https"
        ):
            raise FhirInputError(f"{where}: api-error happens only over https")
        # The same rule T06 applies to the identifier it writes, so the reference always matches.
        PatientInput(self.patient_id)
        if not isinstance(self.agent, str) or not _SERVICE_NAME.match(self.agent):
            raise FhirInputError(
                f"{where}: agent {self.agent!r} must be a service name such as cuff-sim"
            )
        if not isinstance(self.destination, str) or not _HL7V2_STORE_NAME.match(self.destination):
            raise FhirInputError(
                f"{where}: destination must be an HL7v2 store name, "
                "projects/<p>/locations/<l>/datasets/<d>/hl7V2Stores/<s>"
            )
        if self.message_name is not None:
            if not (isinstance(self.result, Acknowledged) and self.result.accepted):
                raise FhirInputError(
                    f"{where}: message_name is only for an accepted message; "
                    "a rejected or unacknowledged message is not stored"
                )
            prefix = self.destination + "/messages/"
            if (
                not isinstance(self.message_name, str)
                or not self.message_name.startswith(prefix)
                or not _MESSAGE_ID.match(self.message_name[len(prefix) :])
            ):
                raise FhirInputError(
                    f"{where}: message_name must be <destination>/messages/<id> in the same store"
                )

    @property
    def sent_at_utc(self) -> str:
        """``YYYY-MM-DDTHH:MM:SS.mmmZ``: the time part of the attempt's key."""
        return self.sent_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + (
            f"{self.sent_at.microsecond // 1000:03d}Z"
        )
