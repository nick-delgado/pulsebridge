"""Reading a request body completely, or refusing it.

Werkzeug's own limit (``MAX_CONTENT_LENGTH``) is not used, because it does not protect a body
without ``Content-Length``: under gunicorn a chunked request (``Transfer-Encoding: chunked``) has
``wsgi.input_terminated`` set, and Werkzeug then wraps the input in a ``LimitedStream`` that
simply stops at the limit, so ``request.get_data()`` returns the first 16 KiB of a larger body
without any error. Forwarding that would send a truncated body as if it were the caller's.

:func:`read_body` reads at most ``limit + 1`` bytes itself and refuses with 413 when there are
more, and with 400 when a declared ``Content-Length`` was not delivered. Every route that reads a
body uses it.
"""

from __future__ import annotations

from flask import request

from fhir_proxy.errors import Problem

# cuff-sim's own limit (MAX_BODY_BYTES): a larger body could never be a reading.
MAX_BODY_BYTES = 16 * 1024


def too_large(limit: int) -> Problem:
    # The same shape cuff-sim answers (code too-long, field body), so the dashboard's
    # CuffSendError reads it as "too-large" whichever side refused.
    return Problem(
        413,
        "too-long",
        "too-large",
        f"The body must be at most {limit} bytes.",
        expression=["body"],
    )


def read_body(limit: int = MAX_BODY_BYTES) -> bytes:
    declared = request.content_length
    if declared is not None and declared > limit:
        raise too_large(limit)
    # request.stream: the declared length when there is one; the whole (terminated) input for
    # a chunked body under gunicorn; empty when neither is known (Werkzeug's safe fallback).
    data = request.stream.read(limit + 1)
    if len(data) > limit:
        raise too_large(limit)
    if declared is not None and len(data) != declared:
        raise Problem(400, "incomplete", "incomplete-body", "The body ended early.")
    return data
