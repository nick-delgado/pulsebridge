"""What the smoke test passes to the ``gcloud`` commands it runs, and what it shows of them.

**The child's environment.** ``gcloud`` needs the caller's own environment (``PATH``, its
configuration directory, the proxy settings, ``CLOUDSDK_*``). The smoke test holds no secret in
the environment (the signing key is read by ``gcloud`` itself and kept in memory), but
``PULSEBRIDGE_REVIEWER_PASSWORD``, the D9 password variable an operator may still have set from
before, is removed from the copy handed to a child all the same: no other program, and no crash
report or debug log of one, should see it.

**What is shown of a failure.** A fixed sentence saying what to check, then gcloud's own
message: its whole stderr with whitespace collapsed and at most :data:`STDERR_LIMIT`
characters (the output is redacted again before it is printed).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

# D9's variable, no longer read; dropped from a child's environment in case it is still set.
LEGACY_PASSWORD_ENV = "PULSEBRIDGE_REVIEWER_PASSWORD"  # noqa: S105 - a variable name
STDERR_LIMIT = 600


def child_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of ``source`` (default: this process's environment) without the legacy
    password variable."""
    env = dict(os.environ if source is None else source)
    env.pop(LEGACY_PASSWORD_ENV, None)
    return env


def gcloud_said(stderr: str | None) -> str:
    """gcloud's stderr on one line, limited to :data:`STDERR_LIMIT` characters."""
    text = " ".join((stderr or "").split())
    if not text:
        return "gcloud printed no message"
    if len(text) > STDERR_LIMIT:
        text = text[:STDERR_LIMIT] + " [...]"
    return f"gcloud said: {text}"
