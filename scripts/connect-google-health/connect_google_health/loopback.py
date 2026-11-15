"""A one-shot HTTP listener on 127.0.0.1 that receives Google's redirect.

Google's desktop-app guide: "start an HTTP listener on a random available port" on the loopback
IP, and when the response arrives "respond by displaying an HTML page that instructs the user to
close the browser and return to your app".

What it answers:

- ``GET /start``: a ``302`` to Google's authorization address. The browser is opened at this
  local address, not at Google's, so the OAuth client ID (which is in Google's address) is never
  printed to the terminal, even when the user has to open the link by hand.
- ``GET /?…``: **the redirect**. Its query is checked by :func:`oauth.parse_redirect_query`
  (``state`` first), the browser gets a short page that holds no code, token or state, and the
  listener stops. It answers exactly one redirect: the next connection is refused.
- Anything else (``/favicon.ico``, ``/`` with no query, another method, a ``Host`` that is not
  ``127.0.0.1:<port>``): ``404`` / ``400`` and it keeps waiting. The ``Host`` check stops a web
  page using DNS rebinding to talk to the listener under its own name.

It binds ``127.0.0.1`` only, never ``0.0.0.0``, so other machines cannot reach it.
``BaseHTTPRequestHandler`` normally writes every request line to stderr, which would print the
authorization code; that logging is switched off.
"""

from __future__ import annotations

import html
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from connect_google_health.oauth import AuthorizationCode, OAuthError, parse_redirect_query

LOOPBACK_HOST = "127.0.0.1"
# Each connection gets its own thread, so one that never sends a request (a browser's
# speculative pre-connect) cannot hold up the redirect; it is dropped after this long.
_CONNECTION_TIMEOUT_SECONDS = 5.0

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>PulseBridge</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 36rem; margin: 3rem auto;">
<h1>{heading}</h1><p>{text}</p></body></html>
"""

SUCCESS_TEXT = "Google's answer was received. You can close this tab and return to the terminal."


def page(heading: str, text: str) -> bytes:
    return _PAGE.format(heading=html.escape(heading), text=html.escape(text)).encode("utf-8")


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True  # an idle connection's thread never keeps the script alive
    block_on_close = False

    def handle_error(self, request: object, client_address: object) -> None:
        # socketserver prints a traceback to stderr when a handler fails (a browser closing the
        # connection early, say). Nothing it would print is needed, so nothing is printed.
        return


@dataclass
class _Outcome:
    code: AuthorizationCode | None = None
    error: OAuthError | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def decided(self) -> bool:
        return self.code is not None or self.error is not None


class LoopbackListener:
    """Bound and listening as soon as it is constructed, so the browser cannot arrive first."""

    def __init__(
        self,
        *,
        expected_state: str,
        port: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._expected_state = expected_state
        # Google's authorization address, which /start redirects to. It holds the redirect
        # address, which holds this listener's port, so the caller sets it once bound.
        self.authorization_url = ""
        self._clock = clock
        self._outcome = _Outcome()
        self._server = _QuietServer((LOOPBACK_HOST, port), self._handler_class())
        self.port: int = self._server.server_address[1]
        self.address = self._server.server_address

    @property
    def start_url(self) -> str:
        return f"http://{LOOPBACK_HOST}:{self.port}/start"

    def close(self) -> None:
        self._server.server_close()

    def __enter__(self) -> LoopbackListener:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def wait(self, timeout_seconds: float) -> AuthorizationCode:
        """Serve requests until one redirect arrives or the time is up, then stop listening.
        Raises :class:`OAuthError` for a refused redirect or the timeout."""
        deadline = self._clock() + timeout_seconds
        try:
            while not self._outcome.decided:
                left = deadline - self._clock()
                if left <= 0:
                    raise OAuthError(
                        "timeout",
                        f"No redirect arrived within {int(timeout_seconds)} seconds. Run the "
                        "script again (or with --paste if the browser is on another machine).",
                    )
                self._server.timeout = min(left, 1.0)
                self._server.handle_request()
        finally:
            self.close()
        if self._outcome.error is not None:
            raise self._outcome.error
        if self._outcome.code is None:  # pragma: no cover - the loop only ends when decided
            raise OAuthError("no-code", "No authorization code was received.")
        return self._outcome.code

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        listener = self

        class Handler(BaseHTTPRequestHandler):
            timeout = _CONNECTION_TIMEOUT_SECONDS
            server_version = "PulseBridge-connect"
            sys_version = ""

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                # The request line holds the authorization code: never write it anywhere.
                return

            def log_error(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def _send(self, status: int, body: bytes, extra: dict[str, str] | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                # The page is about a one-time code: never cache it, never send this address on.
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                for name, value in (extra or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - the http.server naming convention
                if self.headers.get("Host") != f"{LOOPBACK_HOST}:{listener.port}":
                    self._send(400, page("Wrong address", "Open the link the script printed."))
                    return
                parts = urlsplit(self.path)
                if parts.path == "/start" and not parts.query and listener.authorization_url:
                    self._send(
                        302,
                        page("Redirecting", "Continuing to Google."),
                        {"Location": listener.authorization_url},
                    )
                    return
                if parts.path != "/" or not parts.query:
                    self._send(404, page("Not found", "Return to the terminal."))
                    return
                outcome = listener._outcome
                with outcome.lock:
                    # Only the first redirect counts; a second one (a reload, another tab)
                    # changes nothing.
                    if outcome.decided:
                        self._send(409, page("Already answered", "Return to the terminal."))
                        return
                    try:
                        outcome.code = parse_redirect_query(
                            parts.query, expected_state=listener._expected_state
                        )
                        failure = None
                    except OAuthError as exc:
                        outcome.error = failure = exc
                if failure is not None:
                    # Only the fixed code, never the received text.
                    self._send(
                        400,
                        page(
                            "Not connected",
                            f"PulseBridge could not use this answer ({failure.code}). "
                            "See the terminal for what to do.",
                        ),
                    )
                    return
                self._send(200, page("Connected", SUCCESS_TEXT))

            def _refuse(self) -> None:
                self._send(405, page("Not allowed", "Return to the terminal."), {"Allow": "GET"})

            do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = _refuse  # noqa: N815

        return Handler
