"""Print a fingerprint of the files connect.sh installs from the checkout (stdlib only).

    python3 fingerprint.py <repository root> <package dir relative to the root>...

connect.sh reinstalls its private environment when this changes. Each file's path is taken
relative to its package, so a checkout that itself lives under a directory named ``tests`` is
still fingerprinted; only ``tests/`` folders and caches inside the packages are left out.
"""

import hashlib
import sys
from pathlib import Path

INCLUDED_NAMES = ("pyproject.toml", "requirements.lock")


def fingerprint(repo: Path, packages: list[str]) -> str:
    digest = hashlib.sha256()
    for package in packages:
        root = repo / package
        for path in sorted(root.rglob("*")):
            inner = path.relative_to(root)
            if not path.is_file() or "tests" in inner.parts or "__pycache__" in inner.parts:
                continue
            if path.suffix == ".py" or path.name in INCLUDED_NAMES:
                digest.update(str(path.relative_to(repo)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()[:16]


if __name__ == "__main__":
    print(fingerprint(Path(sys.argv[1]), sys.argv[2:]))
