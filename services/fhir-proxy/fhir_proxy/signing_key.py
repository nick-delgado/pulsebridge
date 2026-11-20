"""The session-signing key, read from the mounted secret file (D16).

Cloud Run mounts the ``<prefix>-session-signing-key`` secret as a file with version ``latest``.
A mounted secret is read from Secret Manager when the file is read, so a new version takes
effect without a redeploy. The content is kept for :data:`CACHE_SECONDS`, so a burst of
requests does not read the file once each, and replacing the version ends every session within
that time (every token signed with the old key then fails its signature check).

**What the file holds.** The key is the file's bytes, with at most one trailing newline
removed, and must be at least :data:`MIN_KEY_BYTES` (32) long. That accepts the 32 raw random
bytes T44's script stores, and equally a text encoding of them (base64 or hex, which are longer
and carry the same 256 bits), so the proxy does not depend on which encoding the script chose.
HMAC-SHA256 takes a key of any length (RFC 2104 section 3: keys longer than the 64-byte block
are hashed first), and RFC 7518 section 3.2 asks for a key of at least 256 bits for ``HS256``.

The key is never logged, never put into an exception message and never returned.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

MIN_KEY_BYTES = 32
# Far more than any encoding of a 32-byte key; anything longer is not this secret.
MAX_KEY_BYTES = 1024
# How long the file's content is reused. Not configurable: it is only the rotation delay.
CACHE_SECONDS = 30.0


class KeyUnavailable(Exception):
    """The secret has no usable value. The message is a fixed code (``missing``, ``too-short``,
    ``unreadable``), never the content."""


class SigningKeyFile:
    def __init__(
        self,
        path: str,
        *,
        cache_seconds: float = CACHE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = path
        self._cache_seconds = cache_seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._value: bytes | None = None
        self._read_at: float | None = None

    def current(self) -> bytes:
        """The key now in force. Raises :class:`KeyUnavailable` (a failure is never cached, so a
        fixed secret is picked up on the next request)."""
        with self._lock:
            now = self._monotonic()
            if (
                self._value is not None
                and self._read_at is not None
                and now - self._read_at < self._cache_seconds
            ):
                return self._value
            value = self._read()
            self._value, self._read_at = value, now
            return value

    def _read(self) -> bytes:
        try:
            with open(self._path, "rb") as handle:
                data = handle.read(MAX_KEY_BYTES + 1)
        except FileNotFoundError:
            raise KeyUnavailable("missing") from None
        except OSError:
            raise KeyUnavailable("unreadable") from None
        if len(data) > MAX_KEY_BYTES:
            raise KeyUnavailable("unreadable")
        # One trailing newline from an editor or `echo` is not part of the key. Only when the
        # rest is long enough on its own: 32 raw bytes may end in 0x0a by chance.
        if data.endswith(b"\n") and len(data) > MIN_KEY_BYTES:
            data = data[:-1]
            if data.endswith(b"\r") and len(data) > MIN_KEY_BYTES:
                data = data[:-1]
        if len(data) < MIN_KEY_BYTES:
            raise KeyUnavailable("too-short")
        return data
