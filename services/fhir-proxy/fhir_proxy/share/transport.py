"""One HTTPS request to OpenEMR, and nothing more.

Built on the standard library's ``http.client`` so every safety property is visible here:

- **TLS pinned to OpenEMR's own certificate** (``document.pinned_context``): no public authority
  is trusted for OpenEMR, hostname (IP) checking is on, verification is never switched off.
- **The address comes from configuration only** (``OPENEMR_SITE_ADDRESS``); a caller can
  never name a host, a port or a scheme. Paths are built by this package from fixed parts.
- **No redirect is followed**: ``http.client`` never follows one, and a 3xx is handed back as
  an ordinary status (the share then treats it as unexpected). A redirect could carry the
  access token to another host.
- **Bounded**: the caller's timeout (already capped by the request budget) is ONE deadline for
  the whole call: the connection, the TLS handshake, sending, the status line and every read
  each get only the time left; an answer over ``MAX_RESPONSE_BYTES`` is
  refused rather than read.
- Nothing about the request or the answer is logged here.
"""

from __future__ import annotations

import http.client
import ssl
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

MAX_RESPONSE_BYTES = 1024 * 1024
USER_AGENT = "pulsebridge-fhir-proxy"


class OpenEmrError(Exception):
    """OpenEMR could not be used for this request. ``reason`` is a fixed code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class OpenEmrTimeout(OpenEmrError):
    """No complete answer in time (it may have been received and acted on)."""

    def __init__(self) -> None:
        super().__init__("timeout")


class OpenEmrUnreachable(OpenEmrError):
    """No connection, a TLS failure, or the connection broke (``reason`` says which)."""


@dataclass(frozen=True)
class Answer:
    status: int
    headers: dict[str, str]
    body: bytes


class AnswerTooLarge(OpenEmrError):
    def __init__(self, status: int) -> None:
        super().__init__("answer-too-large")
        self.status = status


class Transport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        query: Sequence[tuple[str, str]] | None,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
        context: ssl.SSLContext,
    ) -> Answer:
        """One request to ``path`` on the configured OpenEMR; raises :class:`OpenEmrError`."""
        ...


class HttpsTransport:
    """The production transport: ``http.client`` over the pinned TLS context."""

    def __init__(
        self, host: str, port: int, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        self._host = host
        self._port = port
        self._monotonic = monotonic

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Sequence[tuple[str, str]] | None,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
        context: ssl.SSLContext,
    ) -> Answer:
        if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
            raise ValueError("an OpenEMR path is a plain path")
        target = path + (f"?{urlencode(list(query))}" if query else "")
        ends = self._monotonic() + timeout

        def left() -> float:
            # ONE deadline for the whole call: each blocking step (connect and TLS handshake,
            # send, waiting for the status line, every read) gets only what is left of it, so
            # a call can never take a multiple of its limit.
            remaining = ends - self._monotonic()
            if remaining <= 0:
                raise OpenEmrTimeout()
            return remaining

        connection = http.client.HTTPSConnection(
            self._host, self._port, timeout=left(), context=context
        )
        try:
            connection.connect()
            # Kept here: http.client hands the socket to the response (and clears
            # connection.sock) when the server will close the connection.
            sock = connection.sock
            sock.settimeout(left())  # type: ignore[union-attr]
            connection.request(
                method, target, body=body, headers={"User-Agent": USER_AGENT, **headers}
            )
            sock.settimeout(left())  # type: ignore[union-attr]
            response = connection.getresponse()
            chunks: list[bytes] = []
            size = 0
            while not response.isclosed():  # closed once the whole body has been read
                sock.settimeout(left())  # type: ignore[union-attr]
                chunk = response.read1(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise AnswerTooLarge(response.status)
                chunks.append(chunk)
            return Answer(
                response.status,
                {k.lower(): v for k, v in response.getheaders()},
                b"".join(chunks),
            )
        except TimeoutError:
            raise OpenEmrTimeout() from None
        except ssl.SSLCertVerificationError:
            # Not OpenEMR's certificate, or not issued for this address: never retried around.
            raise OpenEmrUnreachable("tls-verification-failed") from None
        except ssl.SSLError:
            raise OpenEmrUnreachable("tls-failed") from None
        except (ConnectionError, OSError):
            raise OpenEmrUnreachable("unreachable") from None
        except http.client.HTTPException:
            raise OpenEmrUnreachable("broken-answer") from None
        finally:
            connection.close()
