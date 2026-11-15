"""Cuff forwarding (D10): body unchanged, the proxy's identity, cuff-sim's answers passed on."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fhir_proxy.testing import CUFF_URL
from fhir_proxy.upstream import UpstreamError, UpstreamTimeout, requests_sender

RESULT = {
    "attemptId": "3507bd7c-a683-5683-af50-1a30c1f7bee7",
    "sentAt": "2026-01-01T14:30:30.250Z",
    "transport": "mllp",
    "result": "accepted",
}
OUTCOME_422 = {
    "resourceType": "OperationOutcome",
    "issue": [
        {
            "severity": "error",
            "code": "invalid",
            "diagnostics": "is required",
            "expression": ["systolic"],
        }
    ],
}


def _post(harness, body: bytes, content_type: str = "application/json"):  # noqa: ANN001, ANN202
    return harness.client.post(
        "/cuff/readings", data=body, content_type=content_type, headers=harness.auth()
    )


def test_body_is_forwarded_byte_for_byte_with_the_proxys_identity(harness) -> None:
    harness.sender.answer("/cuff/readings", 200, RESULT)
    # Odd spacing, key order and an unknown field: none of it is touched (cuff-sim refuses
    # unknown fields itself, so the proxy must not strip them and hide the refusal).
    body = b'{ "diastolic":80,"systolic" :120, "transport":"mllp","sendMalformed":null,"x":1}'
    response = _post(harness, body)
    assert response.status_code == 200
    assert response.get_json() == RESULT
    (call,) = harness.sender.calls
    assert call["method"] == "POST"
    assert call["url"] == f"{CUFF_URL}/cuff/readings"
    assert call["body"] == body
    assert call["headers"]["Content-Type"] == "application/json"
    assert (
        call["headers"]["Authorization"] == "Bearer fake-identity-token-for-cuff-sim.example.test"
    )
    assert harness.tokens.audiences == [CUFF_URL]
    assert call["timeout"] >= 90
    # The reviewer's session token never leaves the proxy.
    assert harness.auth()["Authorization"] not in json.dumps(call["headers"])


@pytest.mark.parametrize("status", [400, 413, 422, 429, 500])
def test_cuff_sims_refusals_are_passed_on_unchanged(harness, status: int) -> None:
    raw = json.dumps(OUTCOME_422, separators=(",", ":")).encode()
    headers = {"retry_after": "17"} if status == 429 else {}
    harness.sender.answer(
        "/cuff/readings", status, raw, content_type="application/fhir+json", **headers
    )
    response = _post(harness, b'{"diastolic": 80}')
    assert response.status_code == status
    assert response.get_data() == raw
    assert response.headers["Content-Type"] == "application/fhir+json"
    if status == 429:
        assert response.headers["Retry-After"] == "17"
    else:
        assert "Retry-After" not in response.headers


@pytest.mark.parametrize("retry_after", ["soon", "-1", "1.5", "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_only_a_delay_seconds_retry_after_is_passed_on(harness, retry_after: str) -> None:
    harness.sender.answer("/cuff/readings", 429, OUTCOME_422, retry_after=retry_after)
    response = _post(harness, b"{}")
    assert response.status_code == 429
    assert "Retry-After" not in response.headers


@pytest.mark.parametrize("status", [401, 403, 404, 502, 503, 302])
def test_other_cuff_sim_statuses_are_a_502(harness, status: int) -> None:
    harness.sender.answer(
        "/cuff/readings", status, b"<html>Google Frontend detail</html>", content_type="text/html"
    )
    response = _post(harness, b"{}")
    assert response.status_code == 502
    text = response.get_data(as_text=True)
    assert "Google Frontend" not in text and "cuff-sim.example.test" not in text
    assert response.get_json()["issue"][0]["details"]["coding"][0]["code"] == "cuff-sim-unreachable"


def test_a_200_that_is_not_json_is_a_502(harness) -> None:
    harness.sender.answer("/cuff/readings", 200, b"<html>", content_type="text/html")
    assert _post(harness, b"{}").status_code == 502


def test_cloud_run_cutting_cuff_sim_off_is_a_504(harness) -> None:
    harness.sender.answer(
        "/cuff/readings", 504, b"upstream request timeout", content_type="text/plain"
    )
    response = _post(harness, b"{}")
    assert response.status_code == 504
    assert response.get_json()["issue"][0]["details"]["coding"][0]["code"] == "cuff-sim-timeout"


def test_unreachable_is_502_and_timeout_is_504(harness) -> None:
    harness.sender.routes["/cuff/readings"] = UpstreamError("unreachable")
    assert _post(harness, b"{}").status_code == 502
    harness.sender.routes["/cuff/readings"] = UpstreamTimeout()
    assert _post(harness, b"{}").status_code == 504


def test_identity_token_failure_is_a_502_and_nothing_is_sent(harness) -> None:
    class Broken:
        def token(self, audience: str) -> str:
            raise UpstreamError("identity-token-unavailable")

    harness.tokens.token = Broken().token  # type: ignore[method-assign]
    harness.sender.answer("/cuff/readings", 200, RESULT)
    assert _post(harness, b"{}").status_code == 502
    assert harness.sender.calls == []


def test_body_over_16_kib_is_refused_before_forwarding(harness) -> None:
    response = _post(harness, b"{" + b" " * (16 * 1024) + b"}")
    assert response.status_code == 413
    issue = response.get_json()["issue"][0]
    assert issue["code"] == "too-long" and issue["expression"] == ["body"]
    assert harness.sender.calls == []


def test_a_non_json_content_type_is_not_forwarded_as_json(harness) -> None:
    harness.sender.answer("/cuff/readings", 400, OUTCOME_422)
    _post(harness, b'{"systolic": 1}', content_type="text/plain")
    (call,) = harness.sender.calls
    assert "Content-Type" not in call["headers"]
    assert call["body"] == b'{"systolic": 1}'


def test_cuff_forwarding_needs_a_logged_in_caller(harness) -> None:
    harness.sender.answer("/cuff/readings", 200, RESULT)
    response = harness.client.post("/cuff/readings", data=b"{}", content_type="application/json")
    assert response.status_code == 401
    assert harness.sender.calls == []


def test_sample_list_is_forwarded(harness) -> None:
    samples = [
        {"name": "unparseable_no_msh", "label": "x", "class": "unparseable", "transports": ["mllp"]}
    ]
    harness.sender.answer("/cuff/samples", 200, samples)
    response = harness.get("/cuff/samples")
    assert response.status_code == 200 and response.get_json() == samples
    (call,) = harness.sender.calls
    assert call["method"] == "GET" and call["body"] is None
    assert call["headers"]["Authorization"].startswith("Bearer fake-identity-token")


# ---- the real HTTP sender, against a server on 127.0.0.1 ----------------------------------------


class _Handler(BaseHTTPRequestHandler):
    mode = "ok"
    received: list[bytes] = []

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        _Handler.received.append(self.rfile.read(length))
        if _Handler.mode == "slow":
            time.sleep(1.5)
        if _Handler.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.end_headers()
            return
        body = b"x" * (300 * 1024) if _Handler.mode == "huge" else b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def local_server():  # noqa: ANN201
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Handler.received = []
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_requests_sender_sends_bytes_and_reads_the_answer(local_server: str) -> None:
    _Handler.mode = "ok"
    status, headers, body = requests_sender()(
        "POST", f"{local_server}/cuff/readings", headers={"A": "b"}, body=b"\x00raw", timeout=5
    )
    assert (
        status == 200 and body == b'{"ok": true}' and headers["content-type"] == "application/json"
    )
    assert _Handler.received == [b"\x00raw"]


def test_requests_sender_times_out(local_server: str) -> None:
    _Handler.mode = "slow"
    with pytest.raises(UpstreamTimeout):
        requests_sender()("POST", f"{local_server}/x", headers={}, body=b"{}", timeout=0.5)


def test_requests_sender_does_not_follow_redirects(local_server: str) -> None:
    _Handler.mode = "redirect"
    status, _, _ = requests_sender()("POST", f"{local_server}/x", headers={}, body=b"{}", timeout=5)
    assert status == 302


def test_requests_sender_refuses_a_huge_answer(local_server: str) -> None:
    _Handler.mode = "huge"
    with pytest.raises(UpstreamError, match="answer-too-large"):
        requests_sender()("POST", f"{local_server}/x", headers={}, body=b"{}", timeout=5)


def test_requests_sender_unreachable() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(UpstreamError, match="unreachable"):
        requests_sender()("POST", f"http://127.0.0.1:{port}/x", headers={}, body=b"{}", timeout=2)
