"""Structured logs: one JSON object per line, which Cloud Logging reads as a structured entry
(``severity`` and ``message`` are its own fields; the rest becomes ``jsonPayload``).

Callers pass only bounded, non-identifying fields in ``extra={"fields": {...}}``: event names,
outcome codes, counts, UTC range bounds, durations and HTTP statuses. Never heart-rate values,
tokens, secrets, the patient id, the device name or a response body (README, "Logging").
"""

from __future__ import annotations

import json
import logging
import sys
from typing import TextIO


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {"severity": record.levelname, "message": record.getMessage()}
        entry.update(getattr(record, "fields", {}))
        return json.dumps(entry, default=str)


def configure_logging(stream: TextIO | None = None) -> None:
    root = logging.getLogger()
    if not any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        handler = logging.StreamHandler(stream or sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    # urllib3 logs each connection at DEBUG only; keep it from ever logging request lines.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
