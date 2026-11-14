"""Write the samples out as raw files, for sending by hand (for example with ``nc``).

    python -m pulsebridge_hl7v2.samples export <directory>

For each sample it writes ``<name>.hl7`` (the message bytes) and ``<name>.mllp`` (the bytes for
the MLLP socket, framing included). The directory is yours; do not commit the files (see
README.md in this directory).
"""

from __future__ import annotations

import sys
from pathlib import Path

from pulsebridge_hl7v2.samples import all_samples


def export(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for sample in all_samples():
        for suffix, data in ((".hl7", sample.payload), (".mllp", sample.wire_bytes)):
            path = directory / f"{sample.name}{suffix}"
            path.write_bytes(data)  # binary mode: no newline translation
            written.append(path)
    return written


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "export":
        print("usage: python -m pulsebridge_hl7v2.samples export <directory>", file=sys.stderr)
        return 2
    for path in export(Path(argv[1])):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
