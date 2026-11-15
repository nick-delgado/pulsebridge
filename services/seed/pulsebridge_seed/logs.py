"""JSON log lines, one object per line, as health-sync writes them (Cloud Logging reads
``severity`` and ``message``; the rest becomes ``jsonPayload``).

Only bounded, non-identifying fields are logged: event names, outcome and reason codes, counts,
UTC range bounds and HTTP statuses. Never heart-rate values, the patient identifier, the seed,
a response body or an exception message (it can quote a value). The data is synthetic, but the
seeder follows the same rule as the services handling real data, so a copy of this code
pointed at real data would not start leaking it.
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
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
