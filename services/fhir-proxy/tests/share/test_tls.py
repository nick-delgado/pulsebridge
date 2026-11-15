"""The real HTTPS transport against the fake OpenEMR on 127.0.0.1, with certificates generated
here: TLS trusts only the document's certificate, with IP checking on; no redirect is followed;
answers are size- and time-capped."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest
from fhir_proxy.share.document import pinned_context
from fhir_proxy.share.testing import (
    HttpsFakeServer,
    build_share_env,
    make_certificate,
)
from fhir_proxy.share.transport import MAX_RESPONSE_BYTES, Answer, HttpsTransport

SHARE_SOURCE = Path(__file__).resolve().parents[2] / "fhir_proxy" / "share"


@pytest.fixture
def tls(tmp_path, client_key, server_cert):  # noqa: ANN001, ANN201
    """The proxy talking real TLS to a fake OpenEMR serving ``server_cert`` on 127.0.0.1."""
    server = HttpsFakeServer(None, server_cert[0], server_cert[1])
    with server:
        site = f"https://127.0.0.1:{server.port}"
        env = build_share_env(
            tmp_path,
            client_key,
            server_cert,
            site=site,
            transport=HttpsTransport("127.0.0.1", server.port),
            timeout=2.0,
        )
        server.fake = env.openemr
        yield env


def failed_tls(env, response, reason: str) -> None:  # noqa: ANN001
    assert response.status_code == 502
    body = response.get_json()
    assert (
        body["operationOutcome"]["issue"][0]["details"]["coding"][0]["code"]
        == "openemr-unreachable"
    )
    (step,) = body["steps"]
    assert step["step"] == "token" and step["response"]["status"] == 0
    assert reason in step["label"]
    assert env.openemr.requests == []  # the handshake failed: not one request reached OpenEMR


def test_a_whole_share_over_pinned_tls(tls) -> None:
    response = tls.share()
    assert response.status_code == 200, response.get_data(as_text=True)
    assert [s["result"] for s in response.get_json()["steps"]][-1] == "ok"
    assert tls.harness.get("/status").get_json()["openemr"] == {
        "reachable": True,
        "checkedAt": "2026-01-02T12:00:00Z",
        "detail": None,
    }


def test_another_certificate_is_refused(tls) -> None:
    other, _ = make_certificate(["127.0.0.1"])  # valid for the IP, but not OpenEMR's
    tls.write_doc(server_certificate_pem=other)
    failed_tls(tls, tls.share(), "tls-verification-failed")
    assert tls.harness.get("/status").get_json()["openemr"]["detail"] == "tls-verification-failed"


def test_a_certificate_for_another_ip_is_refused(tmp_path, client_key) -> None:
    cert, key = make_certificate(["127.0.0.2"])  # OpenEMR's own certificate, another address
    server = HttpsFakeServer(None, cert, key)
    with server:
        env = build_share_env(
            tmp_path,
            client_key,
            (cert, key),
            site=f"https://127.0.0.1:{server.port}",
            transport=HttpsTransport("127.0.0.1", server.port),
            timeout=2.0,
        )
        server.fake = env.openemr
        failed_tls(env, env.share(), "tls-verification-failed")


def test_the_context_trusts_only_the_one_certificate(server_cert) -> None:
    import ssl

    context = pinned_context(server_cert[0])
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    assert len(context.get_ca_certs()) == 1  # no public authority
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_verification_is_never_switched_off_in_the_share_code() -> None:
    forbidden = re.compile(
        r"CERT_NONE|check_hostname\s*=\s*False|verify\s*=\s*False|_create_unverified_context"
    )
    for source in SHARE_SOURCE.glob("*.py"):
        assert not forbidden.search(source.read_text()), source.name


def test_a_redirect_is_not_followed_over_the_wire(tls) -> None:
    tls.openemr.override(
        "GET", r"/Patient$", Answer(302, {"location": "https://127.0.0.1:1/x"}, b"")
    )
    response = tls.share()
    assert response.status_code == 502
    assert response.get_json()["steps"][-1]["response"]["status"] == 302
    assert len(tls.openemr.calls("GET")) == 1


def test_an_oversized_answer_is_not_read(tls) -> None:
    assert MAX_RESPONSE_BYTES == 1024 * 1024
    big = b'{"x": "' + b"a" * (2 * 1024 * 1024) + b'"}'
    tls.openemr.override(
        "GET", r"/Patient$", Answer(200, {"content-type": "application/json"}, big)
    )
    response = tls.share()
    assert response.status_code == 502
    assert "answer too large" in response.get_json()["steps"][-1]["label"]


def test_a_slow_answer_times_out_within_the_call_limit(tls) -> None:
    def slow(method, path, query, headers, body):  # noqa: ANN001, ANN202
        time.sleep(3)  # longer than the 2 s call limit
        return Answer(200, {}, b"{}")

    tls.openemr.override("GET", r"/Patient$", slow)
    began = time.monotonic()
    response = tls.share()
    assert time.monotonic() - began < 3
    assert response.status_code == 504
    assert response.get_json()["steps"][-1]["response"]["status"] == 0


def test_nothing_listening_is_unreachable(tmp_path, client_key, server_cert) -> None:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = build_share_env(
        tmp_path,
        client_key,
        server_cert,
        site=f"https://127.0.0.1:{port}",
        transport=HttpsTransport("127.0.0.1", port),
        timeout=2.0,
    )
    failed_tls(env, env.share(), "unreachable")
    assert env.harness.get("/status").get_json()["openemr"]["detail"] == "unavailable"


def test_a_call_has_one_deadline_not_one_per_blocking_step(tls) -> None:
    from fhir_proxy.share.testing import SlowAnswer

    # Each wait alone is under the 2 s limit; together they are over it. With a per-operation
    # timeout this would take about 3 s and succeed; with one deadline it stops at about 2 s.
    slow = SlowAnswer(
        200,
        {"content-type": "application/json"},
        json.dumps({"resourceType": "Bundle", "type": "searchset"}).encode(),
        header_delay=1.5,
        chunk_delay=1.5,
    )
    tls.openemr.override("GET", r"/Patient$", slow)
    began = time.monotonic()
    response = tls.share()
    elapsed = time.monotonic() - began
    assert response.status_code == 504
    assert elapsed < 2.7, elapsed
