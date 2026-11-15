"""Structured logs: one JSON object per line on stdout (Cloud Logging reads ``severity``).

What may be logged is decided at each call site, by passing explicit fields: event names, codes,
counts, HTTP statuses, durations, resource ids that are hashes (AuditEvent and message ids), and
the client address of a failed login (docs/spikes/s6.md: "time, client address").

Never logged: a submitted password, a token (session or identity), the patient identifier or
name, observation values, message or ACK text, request or response bodies, query strings,
exception messages or server error text. Exceptions are logged by class name only.
"""

from __future__ import annotations

import json
import logging
import sys

logger = logging.getLogger("fhir_proxy")


class JsonFormatter(logging.Formatter):
    """``severity``, ``message`` and the record's ``fields``. Nothing else from the record: in
    particular no ``exc_info`` traceback, whose text could quote a value."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {"severity": record.levelname, "message": record.getMessage()}
        entry.update(getattr(record, "fields", {}))
        return json.dumps(entry, default=str)


def configure_logging() -> None:
    root = logging.getLogger()
    if not any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    root.setLevel(logging.INFO)


def log(level: int, event: str, **fields: object) -> None:
    logger.log(level, "fhir-proxy: %s", event, extra={"fields": {"event": event, **fields}})
