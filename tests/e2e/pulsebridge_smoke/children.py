"""What the smoke test passes to the ``gcloud`` commands it runs, and what it shows of them.

**The child's environment.** ``gcloud`` needs the caller's own environment (``PATH``, its
configuration directory, the proxy settings, ``CLOUDSDK_*``), but never the reviewer password:
when ``PULSEBRIDGE_REVIEWER_PASSWORD`` is set it is removed from the copy handed to the child,
so no other program, and no crash report or debug log of one, can see it.

**What is shown of a failure.** A fixed sentence saying what to check, then gcloud's own
message: its whole stderr with whitespace collapsed and at most :data:`STDERR_LIMIT`
characters (the output is redacted again before it is printed).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

PASSWORD_ENV = "PULSEBRIDGE_REVIEWER_PASSWORD"  # noqa: S105 - a variable name, not a value
STDERR_LIMIT = 600


def child_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of ``source`` (default: this process's environment) without the password."""
    env = dict(os.environ if source is None else source)
    env.pop(PASSWORD_ENV, None)
    return env


def gcloud_said(stderr: str | None) -> str:
    """gcloud's stderr on one line, limited to :data:`STDERR_LIMIT` characters."""
    text = " ".join((stderr or "").split())
    if not text:
        return "gcloud printed no message"
    if len(text) > STDERR_LIMIT:
        text = text[:STDERR_LIMIT] + " [...]"
    return f"gcloud said: {text}"
