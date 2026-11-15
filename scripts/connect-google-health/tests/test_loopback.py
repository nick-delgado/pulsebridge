"""The one-shot loopback listener, driven by real HTTP requests on 127.0.0.1."""

import html
import http.client
import socket
import threading
import time

import pytest
from connect_google_health.loopback import SUCCESS_TEXT, LoopbackListener
from connect_google_health.oauth import OAuthError
from connect_google_health.testing import AUTH_CODE, leaked

STATE = "synthetic-state-value-abcdefghijklmnopqrstuvwxyz"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth?client_id=x&state=" + STATE


def _get(port, path, host=None, method="GET"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host or f"127.0.0.1:{port}")
        conn.endheaders()
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode()
    finally:
        conn.close()


def _serve(listener, timeout=10):
    """Run wait() in a thread; returns (thread, result dict)."""
    result = {}

    def target():
        try:
            result["code"] = listener.wait(timeout)
        except OAuthError as exc:
            result["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


def _listener():
    listener = LoopbackListener(expected_state=STATE)
    listener.authorization_url = AUTH_URL
    return listener


def _refused(port):
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()


def test_binds_127_0_0_1_only_on_a_random_port():
    with _listener() as listener:
        assert listener.address[0] == "127.0.0.1"
        assert listener.port > 0
        assert listener.start_url == f"http://127.0.0.1:{listener.port}/start"
        # Not on the machine's other addresses (when it has one).
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1: no packet is sent for UDP connect
            other = probe.getsockname()[0]
        except OSError:
            other = None
        finally:
            probe.close()
        if other and not other.startswith("127."):
            with pytest.raises(OSError):
                socket.create_connection((other, listener.port), timeout=2).close()


def test_start_redirects_to_google_without_putting_the_address_in_the_page():
    with _listener() as listener:
        thread, result = _serve(listener)
        status, headers, body = _get(listener.port, "/start")
        assert status == 302
        assert headers["Location"] == AUTH_URL
        assert "client_id" not in body
        _get(listener.port, f"/?state={STATE}&code={AUTH_CODE}")
        thread.join(5)
        assert result["code"].value == AUTH_CODE


def test_answers_one_redirect_then_stops_listening(capfd):
    with _listener() as listener:
        port = listener.port
        thread, result = _serve(listener)
        status, headers, body = _get(port, f"/?state={STATE}&code={AUTH_CODE}&scope=s")
        thread.join(5)
        assert not thread.is_alive()
        assert status == 200
        assert html.escape(SUCCESS_TEXT) in body
        assert headers["Cache-Control"] == "no-store"
        assert headers["Referrer-Policy"] == "no-referrer"
        # The page holds neither the code nor the state.
        assert AUTH_CODE not in body and STATE not in body
        assert result["code"].value == AUTH_CODE
        # A second request finds nothing listening.
        _refused(port)
    # http.server's request log (which would hold the code) was not written anywhere.
    captured = capfd.readouterr()
    assert AUTH_CODE not in captured.out + captured.err
    assert "GET /" not in captured.err


@pytest.mark.parametrize(
    ("path", "host", "method", "status"),
    [
        ("/favicon.ico", None, "GET", 404),
        ("/", None, "GET", 404),  # no query: not a redirect
        ("/start?x=1", None, "GET", 404),
        (f"/callback?state={STATE}&code=x", None, "GET", 404),  # wrong path
        (f"/?state={STATE}&code=x", "evil.example:80", "GET", 400),  # DNS rebinding
        (f"/?state={STATE}&code=x", "localhost", "GET", 400),
        (f"/?state={STATE}&code=x", None, "POST", 405),
    ],
)
def test_other_requests_are_refused_and_it_keeps_waiting(path, host, method, status):
    with _listener() as listener:
        thread, result = _serve(listener)
        got, _h, body = _get(listener.port, path, host, method)
        assert got == status
        assert STATE not in body
        assert thread.is_alive() and not result
        _get(listener.port, f"/?state={STATE}&code={AUTH_CODE}")
        thread.join(5)
        assert result["code"].value == AUTH_CODE


@pytest.mark.parametrize(
    "query",
    [
        f"state=forged&code={AUTH_CODE}",
        f"code={AUTH_CODE}",  # no state
        f"state={STATE}&state={STATE}&code={AUTH_CODE}",  # repeated
        "error=access_denied",  # an error without this run's state is not acted on
        "&".join(f"f{i}=x" for i in range(30)) + f"&state={STATE}",  # too many fields
    ],
)
def test_a_request_without_this_runs_state_is_ignored_and_the_wait_goes_on(query):
    """Any web page can make the browser send a request to 127.0.0.1; it must not end the
    connect. Only a redirect with this run's state counts."""
    with _listener() as listener:
        thread, result = _serve(listener)
        status, _h, body = _get(listener.port, "/?" + query)
        assert status == 400
        assert leaked(body, (AUTH_CODE, STATE, "forged")) == []
        assert thread.is_alive() and not result
        _get(listener.port, f"/?state={STATE}&code={AUTH_CODE}")
        thread.join(5)
        assert result["code"].value == AUTH_CODE


def test_this_runs_state_with_no_code_ends_the_wait_with_missing_code():
    with _listener() as listener:
        thread, result = _serve(listener)
        status, _h, body = _get(listener.port, f"/?state={STATE}")
        thread.join(5)
        assert status == 400
        assert "missing-code" in body
        assert result["error"].code == "missing-code"
        _refused(listener.port)


def test_start_is_single_use():
    with _listener() as listener:
        thread, result = _serve(listener)
        assert _get(listener.port, "/start")[0] == 302
        status, headers, body = _get(listener.port, "/start")
        assert status == 410
        assert "Location" not in headers and STATE not in body
        _get(listener.port, f"/?state={STATE}&code={AUTH_CODE}")
        thread.join(5)


def test_wait_returns_promptly_after_the_page_is_sent():
    with _listener() as listener:
        thread, result = _serve(listener)
        time.sleep(0.2)  # the listener is idle and serving
        sent = time.monotonic()
        status, _h, _b = _get(listener.port, f"/?state={STATE}&code={AUTH_CODE}")
        thread.join(5)
        assert status == 200  # the browser got its page
        assert time.monotonic() - sent < 0.5
        assert result["code"].value == AUTH_CODE


def test_error_access_denied_ends_the_wait():
    with _listener() as listener:
        thread, result = _serve(listener)
        status, _h, body = _get(listener.port, f"/?state={STATE}&error=access_denied")
        thread.join(5)
        assert status == 400
        assert result["error"].code == "access_denied"


def test_timeout():
    with _listener() as listener:
        with pytest.raises(OAuthError) as caught:
            listener.wait(1)
        assert caught.value.code == "timeout"
        _refused(listener.port)


def test_a_silent_connection_does_not_block_the_redirect():
    with _listener() as listener:
        thread, result = _serve(listener, timeout=20)
        idle = socket.create_connection(("127.0.0.1", listener.port))  # sends nothing
        try:
            _get(listener.port, f"/?state={STATE}&code={AUTH_CODE}")
            thread.join(10)
        finally:
            idle.close()
        assert result["code"].value == AUTH_CODE


def test_a_busy_port_raises_oserror():
    with _listener() as first, pytest.raises(OSError):
        LoopbackListener(expected_state=STATE, port=first.port)


def test_a_second_redirect_racing_the_first_changes_nothing():
    """Two redirects can arrive in parallel threads before wait() closes the socket. The
    second must not replace the first's outcome."""
    with _listener() as listener:
        _serve_one = threading.Thread(target=listener._server.handle_request, daemon=True)
        listener._server.timeout = 5
        _serve_one.start()
        first = _get(listener.port, f"/?state={STATE}&code={AUTH_CODE}")
        _serve_one.join(5)
        second_thread = threading.Thread(target=listener._server.handle_request, daemon=True)
        second_thread.start()
        second = _get(listener.port, f"/?state={STATE}&code=other")
        second_thread.join(5)
        assert first[0] == 200
        assert second[0] == 409
        assert listener._outcome.code.value == AUTH_CODE
        assert listener._outcome.error is None


def test_the_page_is_fully_sent_before_wait_returns():
    """The script may exit right after wait() (an error, say); the browser must already have
    its page, not a reset connection."""
    with _listener() as listener:
        base = listener._server.RequestHandlerClass
        sent = threading.Event()

        class SlowPage(base):
            def _send(self, status, body, extra=None):
                time.sleep(0.3)
                super()._send(status, body, extra)
                sent.set()

        listener._server.RequestHandlerClass = SlowPage
        thread, result = _serve(listener)
        client = threading.Thread(
            target=_get, args=(listener.port, f"/?state={STATE}&code={AUTH_CODE}"), daemon=True
        )
        client.start()
        thread.join(5)
        assert result["code"].value == AUTH_CODE
        assert sent.is_set(), "wait() returned before the page was sent"
        client.join(5)
