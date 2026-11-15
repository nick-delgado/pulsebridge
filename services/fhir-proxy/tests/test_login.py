"""Reviewer login (D9): the password file, the 401/429/503 answers, the rate limit, XFF."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fhir_proxy import session_token
from fhir_proxy.client_address import UNPARSEABLE, client_address
from fhir_proxy.login_limit import LoginLimiter
from fhir_proxy.testing import OTHER_PASSWORD, PASSWORD, build_harness


def _login(harness, password: str, xff: str | None = "198.51.100.7"):  # noqa: ANN001, ANN202
    headers = {"X-Forwarded-For": xff} if xff is not None else {}
    return harness.client.post("/auth/login", json={"password": password}, headers=headers)


# ---- answers ---------------------------------------------------------------------------------


def test_right_password_gives_a_token_and_expiry_but_no_cookie(harness) -> None:
    response = _login(harness, PASSWORD)
    assert response.status_code == 200
    body = response.get_json()
    assert set(body) == {"token", "expires_at"}
    assert body["expires_at"] == (harness.clock.now + timedelta(hours=8)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert "Set-Cookie" not in response.headers
    assert response.headers["Cache-Control"] == "no-store"
    # The token works on a protected route.
    ok = harness.client.get("/fhir/metadata", headers={"Authorization": f"Bearer {body['token']}"})
    assert ok.status_code == 200


def test_wrong_password_gives_401_with_a_challenge(harness) -> None:
    response = _login(harness, "not-the-password")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer")
    assert response.get_json()["issue"][0]["details"]["coding"][0]["code"] == "wrong-password"


def test_trailing_newline_in_the_secret_file_is_not_part_of_the_password(harness) -> None:
    harness.password_file.write_text(PASSWORD + "\r\n")
    assert _login(harness, PASSWORD).status_code == 200
    assert _login(harness, PASSWORD + "\n").status_code == 401


@pytest.mark.parametrize("content", [None, b"", b"\n", b"\xff\xfe", b"x" * 2000])
def test_missing_or_unusable_secret_gives_503(harness, content: bytes | None) -> None:
    if content is None:
        harness.password_file.unlink()
    else:
        harness.password_file.write_bytes(content)
    response = _login(harness, PASSWORD)
    assert response.status_code == 503
    assert response.get_json()["issue"][0]["details"]["coding"][0]["code"] == (
        "login-not-configured"
    )


def test_missing_secret_does_not_count_as_a_failed_login(harness) -> None:
    harness.password_file.unlink()
    for _ in range(10):
        assert _login(harness, PASSWORD).status_code == 503
    harness.password_file.write_text(PASSWORD)
    assert _login(harness, PASSWORD).status_code == 200


def test_rotating_the_password_ends_every_session(harness) -> None:
    token = harness.login()
    assert (
        harness.get("/fhir/metadata", headers={"Authorization": f"Bearer {token}"}).status_code
        == 200
    )
    harness.password_file.write_text(OTHER_PASSWORD)
    old = harness.get("/fhir/metadata", headers={"Authorization": f"Bearer {token}"})
    assert old.status_code == 401
    assert 'error="invalid_token"' in old.headers["WWW-Authenticate"]
    assert _login(harness, PASSWORD).status_code == 401
    assert _login(harness, OTHER_PASSWORD).status_code == 200


def test_rotation_is_seen_after_the_cache_time(tmp_path) -> None:
    h = build_harness(tmp_path, password_cache_seconds=30)
    token = h.login()
    h.password_file.write_text(OTHER_PASSWORD)
    auth = {"Authorization": f"Bearer {token}"}
    assert h.client.get("/fhir/metadata", headers=auth).status_code == 200  # cached
    h.clock.advance(31)
    assert h.client.get("/fhir/metadata", headers=auth).status_code == 401


@pytest.mark.parametrize(
    ("body", "content_type", "status"),
    [
        (b'{"password": "x", "extra": 1}', "application/json", 400),
        (b'{"password": 123}', "application/json", 400),
        (b'{"password": ""}', "application/json", 400),
        (b'["x"]', "application/json", 400),
        (b'{"password": "a", "password": "b"}', "application/json", 400),
        (b'{"password": NaN}', "application/json", 400),
        (b"not json", "application/json", 400),
        (b'{"password": "x"}', "text/plain", 415),
        (b'{"password": "' + b"x" * 2000 + b'"}', "application/json", 413),
    ],
)
def test_login_body_is_strict(harness, body: bytes, content_type: str, status: int) -> None:
    response = harness.client.post("/auth/login", data=body, content_type=content_type)
    assert response.status_code == status
    assert response.headers["Content-Type"] == "application/fhir+json"


def test_login_compares_in_constant_time(harness, monkeypatch) -> None:
    calls = []
    real = session_token.hmac.compare_digest

    def spy(a, b):  # noqa: ANN001, ANN202
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(session_token.hmac, "compare_digest", spy)
    assert _login(harness, "a-wrong-guess").status_code == 401
    assert len(calls) == 1
    assert len(calls[0][0]) == len(calls[0][1]) == 32


# ---- rate limit ------------------------------------------------------------------------------


def test_sixth_failed_login_in_a_minute_gives_429_with_retry_after(harness) -> None:
    for _ in range(5):
        assert _login(harness, "wrong").status_code == 401
    response = _login(harness, "wrong")
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    # Blocked means blocked: even the right password is not checked now.
    assert _login(harness, PASSWORD).status_code == 429
    harness.clock.advance(59)
    blocked = _login(harness, PASSWORD)
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "1"
    harness.clock.advance(1)
    assert _login(harness, PASSWORD).status_code == 200


def test_failures_age_out_of_a_sliding_window(harness) -> None:
    for _ in range(4):
        _login(harness, "wrong")
    harness.clock.advance(30)
    _login(harness, "wrong")  # five failures inside the last minute
    assert _login(harness, "wrong").status_code == 429
    harness.clock.advance(31)  # the first four have aged out
    assert _login(harness, "wrong").status_code == 401


def test_a_success_does_not_reset_the_count(harness) -> None:
    for _ in range(4):
        _login(harness, "wrong")
    assert _login(harness, PASSWORD).status_code == 200  # a reviewer behind the same address
    assert _login(harness, "wrong").status_code == 401  # fifth failure
    assert _login(harness, "wrong").status_code == 429


def test_addresses_are_counted_apart(harness) -> None:
    for _ in range(5):
        _login(harness, "wrong", xff="203.0.113.1")
    assert _login(harness, "wrong", xff="203.0.113.1").status_code == 429
    assert _login(harness, PASSWORD, xff="203.0.113.2").status_code == 200


def test_spoofed_left_entries_do_not_give_a_fresh_count(harness) -> None:
    # The attacker writes a new fake address in front each time; Cloud Run appends the real one.
    for n in range(5):
        _login(harness, "wrong", xff=f"10.0.0.{n}, 203.0.113.9")
    assert _login(harness, "wrong", xff="10.9.9.9, 203.0.113.9").status_code == 429


def test_ipv6_clients_are_counted_per_64(harness) -> None:
    for n in range(5):
        _login(harness, "wrong", xff=f"2001:db8:1:2::{n + 1}")
    assert _login(harness, "wrong", xff="2001:db8:1:2:ffff::1").status_code == 429
    assert _login(harness, "wrong", xff="2001:db8:1:3::1").status_code == 401


def test_total_limit_across_addresses(tmp_path) -> None:
    h = build_harness(tmp_path, login_failures_total=8)
    for n in range(8):
        assert _login(h, "wrong", xff=f"198.51.100.{n}").status_code == 401
    assert _login(h, "wrong", xff="198.51.100.200").status_code == 429


def test_limiter_retry_after_rounds_up() -> None:
    now = [0.0]
    limiter = LoginLimiter(per_address=2, total=100, window_seconds=60, monotonic=lambda: now[0])
    limiter.record_failure("a")
    now[0] = 10.5
    limiter.record_failure("a")
    assert limiter.retry_after("b") is None
    assert limiter.retry_after("a") == 50  # the first failure ages out at 60.0
    now[0] = 59.9
    assert limiter.retry_after("a") == 1
    now[0] = 60.0
    assert limiter.retry_after("a") is None


# ---- X-Forwarded-For -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("xff", "remote", "hops", "expected"),
    [
        # 0 hops: no proxy in front; the header is ignored entirely.
        ("203.0.113.5", "192.0.2.10", 0, "192.0.2.10"),
        # 1 hop: straight to *.run.app; Cloud Run appended the last entry.
        ("203.0.113.5", "169.254.1.1", 1, "203.0.113.5"),
        ("6.6.6.6, 203.0.113.5", "169.254.1.1", 1, "203.0.113.5"),
        ("evil, also evil, 203.0.113.5", "169.254.1.1", 1, "203.0.113.5"),
        # 2 hops: a proxy in front that appends one more entry (e.g. LB: CLIENT_IP, LB_IP).
        ("6.6.6.6, 203.0.113.5, 35.191.0.1", "169.254.1.1", 2, "203.0.113.5"),
        ("203.0.113.5, 35.191.0.1", "169.254.1.1", 2, "203.0.113.5"),
        # Fewer entries than hops: every entry is a trusted one; use the left-most.
        ("203.0.113.5", "169.254.1.1", 2, "203.0.113.5"),
        # Ports and IPv6.
        ("[2001:db8::7]:443", "169.254.1.1", 1, "2001:db8::/64"),
        ("203.0.113.5:8443", "169.254.1.1", 1, "203.0.113.5"),
        ("::ffff:203.0.113.5", "169.254.1.1", 1, "203.0.113.5"),
        # Not an address in the trusted position: one shared bucket.
        ("203.0.113.5, not-an-ip", "169.254.1.1", 1, UNPARSEABLE),
        (",,,", "169.254.1.1", 1, UNPARSEABLE),
        ("1.2.3.4," * 400, "169.254.1.1", 1, UNPARSEABLE),
        # No header at all: the socket's peer.
        (None, "192.0.2.10", 1, "192.0.2.10"),
    ],
)
def test_client_address(xff: str | None, remote: str, hops: int, expected: str) -> None:
    assert client_address(xff, remote, hops) == expected


def test_trusted_hops_setting_reaches_the_limiter(tmp_path) -> None:
    h = build_harness(tmp_path, trusted_proxy_hops=2)
    # With two trusted hops the client is second from the right; the right-most (the front
    # proxy's own address) is the same for everyone and must not be the key.
    for _ in range(5):
        _login(h, "wrong", xff="203.0.113.1, 35.191.0.1")
    assert _login(h, "wrong", xff="203.0.113.77, 35.191.0.1").status_code == 401
    assert _login(h, "wrong", xff="203.0.113.1, 35.191.0.1").status_code == 429
