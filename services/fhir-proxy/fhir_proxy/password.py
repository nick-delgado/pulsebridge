"""The reviewer password, read from the mounted secret file (D9, docs/spikes/s6.md).

Cloud Run mounts the ``reviewer-password`` secret as a file with version ``latest``. A mounted
secret is read from Secret Manager when the file is read, so a new version takes effect without
a redeploy (s6: "the proxy re-reads the mounted file, with a short cache"). The content is kept
for ``cache_seconds``, so rotation takes effect within that time and a burst of requests does not
read the file once each.

The value is never logged, never put into an exception message and never returned.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

# A real password is 16 to a few dozen characters (s6: at least 16; generated, 24 or more).
# Anything longer than this is not a password file.
MAX_PASSWORD_BYTES = 1024


class PasswordUnavailable(Exception):
    """The secret has no usable value: missing file, empty, unreadable or not text.

    The message is a fixed code (``missing``, ``empty``, ``unreadable``), never the content."""


class PasswordFile:
    def __init__(
        self,
        path: str,
        *,
        cache_seconds: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = path
        self._cache_seconds = cache_seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._value: str | None = None
        self._read_at: float | None = None

    def current(self) -> str:
        """The password now in force. Raises :class:`PasswordUnavailable` (never cached, so a
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

    def _read(self) -> str:
        try:
            with open(self._path, "rb") as handle:
                data = handle.read(MAX_PASSWORD_BYTES + 1)
        except FileNotFoundError:
            raise PasswordUnavailable("missing") from None
        except OSError:
            raise PasswordUnavailable("unreadable") from None
        if len(data) > MAX_PASSWORD_BYTES:
            raise PasswordUnavailable("unreadable")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise PasswordUnavailable("unreadable") from None
        # A trailing newline from `echo` or an editor is not part of the password; everything
        # else is, so a password with inner spaces keeps them.
        text = text.rstrip("\r\n")
        if not text:
            raise PasswordUnavailable("empty")
        return text
