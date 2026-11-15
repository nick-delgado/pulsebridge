"""A one-shot HTTP listener on 127.0.0.1 that receives Google's redirect.

Google's desktop-app guide: "start an HTTP listener on a random available port" on the loopback
IP, and when the response arrives "respond by displaying an HTML page that instructs the user to
close the browser and return to your app".

What it answers:

- ``GET /start``, once: a ``302`` to Google's authorization address. The browser is opened at
  this local address, not at Google's, so the OAuth client ID (which is in Google's address) is
  never printed to the terminal, even when the user has to open the link by hand. A second
  ``/start`` gets ``410``: the address carries this run's ``state``, so it is handed out once.
- ``GET /?state=<this run's state>&…``: **the redirect**. Its ``error`` or ``code`` is checked by
  :func:`oauth.parse_redirect_query`, the browser gets a short page that holds no code, token or
  state, and then the wait ends. Exactly one redirect counts; a later one gets ``409``.
- ``GET /?…`` **without this run's state** (missing, wrong, repeated or malformed): ``400``, and
  the wait goes on. Any web page the user has open can make the browser send a request to
  127.0.0.1, so such a request must neither end the connect nor be acted on.
- Anything else (``/favicon.ico``, ``/`` with no query, another method, a ``Host`` that is not
  ``127.0.0.1:<port>``): ``404`` / ``405`` / ``400`` and the wait goes on. The ``Host`` check stops
  a web page using DNS rebinding to talk to the listener under its own name.

It binds ``127.0.0.1`` only, never ``0.0.0.0``, so other machines cannot reach it. Each
connection has its own thread, so a browser's silent pre-connect cannot hold up the redirect.
``BaseHTTPRequestHandler`` normally writes every request line to stderr, which would print the
authorization code; that logging, and socketserver's traceback printing, are switched off.
"""

from __future__ import annotations

import html
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from connect_google_health.oauth import (
    AuthorizationCode,
    OAuthError,
    parse_redirect_query,
    redirect_state_matches,
)

LOOPBACK_HOST = "127.0.0.1"
# A connection that never sends a request is dropped after this long.
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
    """What the one counted redirect said. ``claimed`` is set (under the lock) by the handler
    that takes the redirect; ``done`` once its page has been sent and the result stored."""

    code: AuthorizationCode | None = None
    error: OAuthError | None = None
    claimed: bool = False
    start_served: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    done: threading.Event = field(default_factory=threading.Event)


class LoopbackListener:
    """Bound and listening as soon as it is constructed, so the browser cannot arrive first."""

    def __init__(self, *, expected_state: str, port: int = 0) -> None:
        self._expected_state = expected_state
        self._outcome = _Outcome()
        self._server = _QuietServer((LOOPBACK_HOST, port), self._handler_class())
        self.port: int = self._server.server_address[1]
        self.address = self._server.server_address
        # Google's authorization address, which /start redirects to. It holds the redirect
        # address, which holds this listener's port, so the caller sets it once bound.
        self.authorization_url = ""
        self._thread: threading.Thread | None = None

    @property
    def start_url(self) -> str:
        return f"http://{LOOPBACK_HOST}:{self.port}/start"

    def close(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self._server.server_close()

    def __enter__(self) -> LoopbackListener:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def wait(self, timeout_seconds: float) -> AuthorizationCode:
        """Serve until the redirect has been answered or the time is up, then stop listening.
        Raises :class:`OAuthError` for a refused redirect or the timeout."""
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()
        try:
            if not self._outcome.done.wait(timeout_seconds):
                raise OAuthError(
                    "timeout",
                    f"No redirect arrived within {int(timeout_seconds)} seconds. Run the "
                    "script again (or with --paste if the browser is on another machine).",
                )
        finally:
            self.close()
        if self._outcome.error is not None:
            raise self._outcome.error
        if self._outcome.code is None:  # pragma: no cover - done is set only with one of them
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
                outcome = listener._outcome
                if parts.path == "/start" and not parts.query and listener.authorization_url:
                    with outcome.lock:
                        first = not outcome.start_served
                        outcome.start_served = True
                    if not first:
                        self._send(410, page("Link used", "Run the script again for a new one."))
                        return
                    self._send(
                        302,
                        page("Redirecting", "Continuing to Google."),
                        {"Location": listener.authorization_url},
                    )
                    return
                if parts.path != "/" or not parts.query:
                    self._send(404, page("Not found", "Return to the terminal."))
                    return
                if not redirect_state_matches(parts.query, expected_state=listener._expected_state):
                    # Not this run's redirect: answer, ignore, keep waiting.
                    self._send(400, page("Not this request", "Return to the terminal."))
                    return
                with outcome.lock:
                    if outcome.claimed:
                        self._send(409, page("Already answered", "Return to the terminal."))
                        return
                    outcome.claimed = True
                code = failure = None
                try:
                    code = parse_redirect_query(
                        parts.query, expected_state=listener._expected_state
                    )
                except OAuthError as exc:
                    failure = exc
                # The page goes out first, so the browser has it before the script moves on and
                # the listener closes.
                try:
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
                    else:
                        self._send(200, page("Connected", SUCCESS_TEXT))
                finally:
                    outcome.code, outcome.error = code, failure
                    outcome.done.set()

            def _refuse(self) -> None:
                self._send(405, page("Not allowed", "Return to the terminal."), {"Allow": "GET"})

            do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = _refuse  # noqa: N815

        return Handler
