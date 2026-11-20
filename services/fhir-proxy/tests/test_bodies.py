"""Request bodies are read completely or refused (review M2): never forwarded truncated.

Under gunicorn a chunked request has no Content-Length and ``wsgi.input_terminated`` is set;
Werkzeug's own limit then returns the first 16 KiB of a larger body without an error.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fhir_proxy.testing import ORIGIN, SIGNING_KEY, START, EchoSender, build_harness
from werkzeug.test import EnvironBuilder

LIMIT = 16 * 1024


def _gunicorn_style(app, path: str, body: bytes, headers: dict) -> tuple[int, bytes]:  # noqa: ANN001
    """Call the app with the environ gunicorn builds for a chunked body."""
    environ = EnvironBuilder(path=path, method="POST", headers=headers).get_environ()
    environ.pop("CONTENT_LENGTH", None)
    environ["wsgi.input"] = io.BytesIO(body)
    environ["wsgi.input_terminated"] = True
    environ["CONTENT_TYPE"] = "application/json"
    status: list[str] = []
    chunks = app(environ, lambda s, h, *a: status.append(s))
    return int(status[0].split()[0]), b"".join(chunks)


@pytest.fixture
def echo(tmp_path):  # noqa: ANN001, ANN201
    return build_harness(tmp_path, sender=EchoSender())


@pytest.mark.parametrize(("size", "status"), [(LIMIT, 200), (LIMIT + 1, 413), (40000, 413)])
def test_cuff_body_without_content_length(echo, size: int, status: int) -> None:
    body = b"{" + b" " * (size - 2) + b"}"
    code, answer = _gunicorn_style(echo.app, "/cuff/readings", body, echo.auth())
    assert code == status
    if status == 200:
        assert json.loads(answer)["received"] == size  # all of it, not a 16 KiB prefix
    else:
        assert json.loads(answer)["issue"][0]["code"] == "too-long"
        assert echo.sender.calls == []  # nothing was forwarded


def test_session_body_without_content_length(echo) -> None:
    body = json.dumps({"recaptcha_token": "x" * 20000}).encode()
    code, _ = _gunicorn_style(echo.app, "/auth/session", body, {"Origin": ORIGIN})
    assert code == 413
    assert echo.fraud.assessments == 0


@pytest.mark.parametrize(("size", "status"), [(LIMIT, 200), (LIMIT + 1, 413)])
def test_cuff_body_with_content_length(echo, size: int, status: int) -> None:
    body = b"{" + b" " * (size - 2) + b"}"
    response = echo.client.post(
        "/cuff/readings", data=body, content_type="application/json", headers=echo.auth()
    )
    assert response.status_code == status


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_a_real_gunicorn_with_a_chunked_body(tmp_path: Path) -> None:
    port = _free_port()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "FHIR_PROXY_TEST_DIR": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    gunicorn = Path(sys.executable).with_name("gunicorn")
    process = subprocess.Popen(
        [
            str(gunicorn),
            "--bind",
            f"127.0.0.1:{port}",
            "--workers",
            "1",
            "--threads",
            "2",
            "--no-control-socket",
            "--graceful-timeout",
            "1",
            "fhir_proxy.testing:gunicorn_test_app()",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        # The server's fake Fraud Defense lives in its own process, so the session token is
        # signed here with the same synthetic key and the server's fixed clock (START).
        from fhir_proxy.session_token import TokenSigner

        signer = TokenSigner(
            issuer="pulsebridge-fhir-proxy", ttl_seconds=86400, clock=lambda: START
        )
        token = signer.issue(SIGNING_KEY).token
        results = {}
        for size in (LIMIT, LIMIT + 1, 40000):
            body = b"{" + b" " * (size - 2) + b"}"
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            pieces = iter([body[i : i + 1000] for i in range(0, len(body), 1000)])
            conn.request(
                "POST",
                "/cuff/readings",
                body=pieces,
                encode_chunked=True,
                headers={
                    "Content-Type": "application/json",
                    "Cookie": f"__session={token}",
                    "Origin": ORIGIN,
                },
            )
            response = conn.getresponse()
            results[size] = (response.status, json.loads(response.read()))
        assert results[LIMIT][0] == 200 and results[LIMIT][1]["received"] == LIMIT
        assert results[LIMIT + 1][0] == 413
        assert results[40000][0] == 413
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
