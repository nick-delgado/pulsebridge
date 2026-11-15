"""The code exchange over real HTTP, through the production transport (``requests``), against a
fake token endpoint on 127.0.0.1. Shows the request really goes out as the documented form and
that failures from a real socket are reported without leaking."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from connect_google_health import oauth
from connect_google_health.testing import (
    AUTH_CODE,
    CLIENT_ID,
    CLIENT_SECRET,
    REFRESH_TOKEN,
    SECRET_VALUES,
    leaked,
)
from pulsebridge_gcp import RequestsTransport

CLIENT = oauth.ClientCredentials(CLIENT_ID, CLIENT_SECRET)
REDIRECT = "http://127.0.0.1:50123"
VERIFIER = "w" * 43


class FakeTokenEndpoint:
    def __init__(self, status: int, body: bytes, content_type: str = "application/json"):
        self.received: list[tuple[str, dict, bytes]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                outer.received.append((self.path, dict(self.headers), self.rfile.read(length)))
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/token"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _transport() -> RequestsTransport:
    session = requests.Session()
    session.trust_env = False  # no proxy from the environment: this is 127.0.0.1
    return RequestsTransport(session)


def _exchange(url):
    return oauth.exchange_code(
        _transport(),
        CLIENT,
        oauth.AuthorizationCode(AUTH_CODE),
        code_verifier=VERIFIER,
        redirect_uri=REDIRECT,
        token_url=url,
        timeout=5,
    )


@pytest.fixture
def endpoint(request):
    status, body = request.param
    server = FakeTokenEndpoint(status, body)
    yield server
    server.close()


SUCCESS = json.dumps(
    {"access_token": "synthetic-at", "expires_in": 3599, "refresh_token": REFRESH_TOKEN,
     "token_type": "Bearer", "scope": oauth.DEFAULT_SCOPE}
).encode()  # fmt: skip


@pytest.mark.parametrize("endpoint", [(200, SUCCESS)], indirect=True)
def test_the_form_goes_out_over_http_exactly(endpoint):
    tokens = _exchange(endpoint.url)
    assert tokens.refresh_token == REFRESH_TOKEN
    ((path, headers, body),) = endpoint.received
    assert path == "/token"
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert body == oauth.token_request_body(
        CLIENT, oauth.AuthorizationCode(AUTH_CODE), code_verifier=VERIFIER, redirect_uri=REDIRECT
    )


@pytest.mark.parametrize(
    ("endpoint", "code"),
    [
        ((400, b'{"error": "invalid_grant", "error_description": "' + AUTH_CODE.encode() + b'"}'),
         "invalid_grant"),
        ((401, b'{"error": "invalid_client"}'), "invalid_client"),
        ((200, b"<html>" + CLIENT_SECRET.encode() + b"</html>"), "malformed-response"),
        ((502, b"bad gateway " + CLIENT_SECRET.encode()), "unexpected-status"),
    ],
    indirect=["endpoint"],
)  # fmt: skip
def test_failures_over_http_are_reported_without_leaking(endpoint, code):
    with pytest.raises(oauth.OAuthError) as caught:
        _exchange(endpoint.url)
    assert caught.value.code == code
    assert leaked(str(caught.value), SECRET_VALUES) == []


def test_nothing_listening_is_a_network_error():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens there now
    with pytest.raises(oauth.OAuthError) as caught:
        _exchange(f"http://127.0.0.1:{port}/token")
    assert caught.value.code == "network-error"
    assert leaked(str(caught.value), SECRET_VALUES) == []
