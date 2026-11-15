#!/usr/bin/env bash
# Run the Google Health connect flow (task T11). One command:
#
#   scripts/connect-google-health/connect.sh [flags]      # --help lists them
#
# On first use (and whenever this tool or the two libraries change) it creates a private virtual
# environment outside the repository and installs this tool with libs/pulsebridge_fhir and
# libs/pulsebridge_gcp into it; then it runs connect-google-health with your flags. Needs
# Python 3.13 or newer (set PYTHON to choose one) and `gcloud auth application-default login`.
# The environment lives in CONNECT_VENV (default ~/.cache/pulsebridge/connect-google-health).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
python="${PYTHON:-python3}"
venv="${CONNECT_VENV:-${XDG_CACHE_HOME:-$HOME/.cache}/pulsebridge/connect-google-health}"

# A fingerprint of everything installed from the checkout, so a changed file reinstalls.
fingerprint="$("$python" - "$here" "$repo/libs/pulsebridge_fhir" "$repo/libs/pulsebridge_gcp" <<'PY'
import hashlib, pathlib, sys
digest = hashlib.sha256()
for root in sys.argv[1:]:
    for path in sorted(pathlib.Path(root).rglob("*")):
        if path.is_file() and (path.suffix == ".py" or path.name == "pyproject.toml") \
                and "tests" not in path.parts and "__pycache__" not in path.parts:
            digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
print(digest.hexdigest()[:16])
PY
)"

if [ ! -e "$venv/.installed-$fingerprint" ]; then
  echo "Installing connect-google-health into $venv (first run or after an update)..." >&2
  "$python" -m venv "$venv"
  "$venv/bin/python" -m pip install --quiet --upgrade "pip>=25.1"
  "$venv/bin/python" -m pip install --quiet \
    "$repo/libs/pulsebridge_fhir" "$repo/libs/pulsebridge_gcp" "$here"
  rm -f "$venv"/.installed-*
  touch "$venv/.installed-$fingerprint"
fi

exec "$venv/bin/connect-google-health" "$@"
