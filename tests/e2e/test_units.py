"""Unit tests of the pieces under the smoke command: redaction, the front door's address, the
HTTP transport over a real socket on 127.0.0.1 (no redirects), the HL7v2 reader and the FHIR
rules. Synthetic values only."""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from pulsebridge_hl7v2.oru import BloodPressureReading, Patient, build_bp_oru_r01, to_er7_bytes
from pulsebridge_smoke import fhir
from pulsebridge_smoke.hl7 import parse_dtm, read_sent_reading
from pulsebridge_smoke.http import (
    ApiClient,
    Response,
    UrllibTransport,
    check_base_url,
    problem_reason,
)
from pulsebridge_smoke.redact import Redactor

# ---- redaction ---------------------------------------------------------------------------------


def test_registered_secrets_bearer_tokens_and_jwts_are_redacted() -> None:
    redact = Redactor()
    redact.add("synthetic-password-0001")
    text = redact(
        "pw synthetic-password-0001, Authorization: Bearer abc.DEF-123, "
        "token eyJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJ4In0.c2lnbmF0dXJl"
    )
    assert "synthetic-password-0001" not in text
    assert "abc.DEF-123" not in text
    assert "eyJ" not in text
    assert text.count("[redacted]") == 3


def test_a_secret_containing_another_is_replaced_whole() -> None:
    redact = Redactor()
    redact.add("abcd")
    redact.add("abcdefgh")
    assert redact("x abcdefgh y") == "x [redacted] y"


def test_a_very_short_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="shorter than 4"):
        Redactor().add("ab")


# ---- the base URL ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["https://dash.example.test/api", "https://dash.example.test/api/", "http://127.0.0.1:8080"],
)
def test_good_base_urls(url: str) -> None:
    assert not check_base_url(url).endswith("/")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://dash.example.test/api",
        "ftp://dash.example.test",
        "https://dash.example.test/api?x=1",
        "https://user@dash.example.test/api",
        "https://dash.example.test/api#top",
    ],
)
def test_bad_base_urls(url: str) -> None:
    with pytest.raises(ValueError):
        check_base_url(url)


# ---- the transport, over a real socket ----------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list[tuple[str, str, str | None]] = []

    def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
        _Handler.seen.append((self.command, self.path, self.headers.get("Authorization")))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.end_headers()
            return
        status = 200 if self.path == "/ok" else 404
        body = b'{"resourceType": "OperationOutcome", "issue": [{"code": "not-found"}]}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:  # keep the test output clean
        pass


@pytest.fixture
def server() -> Iterator[str]:
    _Handler.seen = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_transport_answers_and_errors_come_back_as_responses(server: str) -> None:
    transport = UrllibTransport(server)
    ok = transport.send("GET", "/ok", headers={}, body=None, timeout=5)
    missing = transport.send("GET", "/missing", headers={}, body=None, timeout=5)
    assert ok.status == 200
    assert missing.status == 404
    assert problem_reason(missing) == "not-found"


def test_a_redirect_is_not_followed_and_the_token_goes_nowhere_else(server: str) -> None:
    client = ApiClient(UrllibTransport(server), Redactor())
    client._token = "synthetic-session-token-0001"  # noqa: S105, SLF001 - synthetic
    answer = client.call("GET", "/redirect")
    assert answer.status == 302
    assert [path for _, path, _ in _Handler.seen] == ["/redirect"]
    assert _Handler.seen[0][2] == "Bearer synthetic-session-token-0001"


def test_problem_reason_reads_the_proxys_fixed_code() -> None:
    body = (
        b'{"resourceType": "OperationOutcome", "issue": [{"code": "processing", "details": '
        b'{"coding": [{"system": "urn:pulsebridge:fhir-proxy:reason", "code": "share-busy"}]}}]}'
    )
    assert problem_reason(Response(503, body)) == "share-busy"
    envelope = b'{"operationOutcome": ' + body + b', "steps": []}'
    assert problem_reason(Response(502, envelope)) == "share-busy"
    assert problem_reason(Response(500, b"<html>")) == "-"


# ---- the HL7v2 reader ---------------------------------------------------------------------------


def _message(systolic: int = 123, diastolic: int = 77) -> str:
    built = build_bp_oru_r01(
        BloodPressureReading(systolic, diastolic, datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
        Patient("SYN-UNIT-0001", "PULSEBRIDGE", "Synthetic", "Demo"),
        control_id="CTRL0001",
    )
    return to_er7_bytes(built).decode()


def test_the_sent_reading_is_read_with_hl7apy() -> None:
    reading = read_sent_reading(_message())
    assert reading.message_type.startswith("ORU^R01")
    assert reading.control_id == "CTRL0001"
    assert reading.patient_id == "SYN-UNIT-0001"
    assert reading.values == {"8480-6": "123", "8462-4": "77"}
    assert reading.units == {"8480-6": "mm[Hg]", "8462-4": "mm[Hg]"}
    assert reading.observed_at["8480-6"] == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_an_unreadable_message_raises_value_error() -> None:
    with pytest.raises(ValueError):
        read_sent_reading("PID|1||SYN-1\r")


def test_dtm_needs_seconds_and_an_offset() -> None:
    assert parse_dtm("20260102030405+0000") == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert parse_dtm("202601020304") is None
    assert parse_dtm("20260102030405") is None


# ---- the FHIR rules -----------------------------------------------------------------------------


def _observation() -> dict:
    def quantity(value: int) -> dict:
        return {"value": value, "unit": "mmHg", "system": fhir.UCUM, "code": "mm[Hg]"}

    return {
        "resourceType": "Observation",
        "id": "obs-1",
        "status": "final",
        "category": [{"coding": [{"system": fhir.OBSERVATION_CATEGORY, "code": "vital-signs"}]}],
        "code": {"coding": [{"system": fhir.LOINC, "code": "85354-9"}]},
        "subject": {"reference": "Patient/p1"},
        "device": {"reference": "Device/d1"},
        "effectiveDateTime": "2026-01-02T03:04:05+00:00",
        "component": [
            {
                "code": {"coding": [{"system": fhir.LOINC, "code": "8480-6"}]},
                "valueQuantity": quantity(123),
            },
            {
                "code": {"coding": [{"system": fhir.LOINC, "code": "8462-4"}]},
                "valueQuantity": quantity(77),
            },
        ],
    }


AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_a_correct_observation_has_no_problems() -> None:
    checks = fhir.store_observation_checks(
        _observation(), systolic=123, diastolic=77, observed_at=AT
    )
    assert [problem for _, problem in checks] == [None] * len(checks)


def test_the_same_instant_in_another_zone_is_equal() -> None:
    observation = _observation()
    observation["effectiveDateTime"] = "2026-01-01T22:04:05-05:00"
    assert fhir.time_problem(observation, AT) is None


def test_a_boolean_or_string_value_is_not_a_number() -> None:
    observation = _observation()
    observation["component"][0]["valueQuantity"]["value"] = "123"
    assert fhir.values_problem(observation, 123, 77) is not None


def test_a_repeated_component_is_refused() -> None:
    observation = _observation()
    observation["component"].append(observation["component"][0])
    assert fhir.values_problem(observation, 123, 77) is not None


def test_a_time_without_a_zone_is_refused() -> None:
    observation = _observation()
    observation["effectiveDateTime"] = "2026-01-02T03:04:05"
    assert "not a zoned time" in (fhir.time_problem(observation, AT) or "")


def test_provenance_must_target_the_observation_and_name_the_message() -> None:
    provenance = {
        "resourceType": "Provenance",
        "target": [{"reference": "Observation/obs-1"}],
        "recorded": "2026-01-02T03:04:06+00:00",
        "entity": [
            {"role": "source", "what": {"identifier": {"value": "hl7V2Stores/s/messages/m1"}}}
        ],
    }
    full_name = "projects/p/locations/l/datasets/d/hl7V2Stores/s/messages/m1"
    assert fhir.provenance_problem(provenance, "obs-1", full_name) is None
    assert fhir.provenance_problem(provenance, "obs-2", full_name) is not None
    assert fhir.provenance_problem(provenance, "obs-1", full_name.replace("m1", "m2")) is not None
    assert fhir.provenance_problem(None, "obs-1", full_name) is not None


def test_an_opaque_session_token_is_redacted_once_issued() -> None:
    """The token pattern catches JWTs; a token of another shape is redacted because the client
    registers it at login. A server quoting it in an error must not reach the output."""
    from pulsebridge_smoke.cli import main
    from pulsebridge_smoke.http import TransportError

    token = "opaque-session-value-0001"  # noqa: S105 - synthetic

    class Fake:
        def send(self, method, path, **_):  # noqa: ANN001, ANN003, ANN201
            if path == "/auth/login":
                return Response(200, ('{"token": "%s"}' % token).encode())  # noqa: UP031
            raise TransportError(f"{method} {path}: upstream said {token}")

    lines: list[str] = []
    code = main(
        [],
        env={
            "PULSEBRIDGE_BASE_URL": "https://dash.example.test/api",
            "PULSEBRIDGE_REVIEWER_PASSWORD": "synthetic-password-0001",
        },
        transport=Fake(),
        out=lines.append,
    )
    assert code == 1
    text = "\n".join(lines)
    assert "upstream said [redacted]" in text
    assert token not in text
