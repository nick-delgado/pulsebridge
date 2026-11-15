#!/usr/bin/env bash
# Run the Google Health connect flow (task T11). One command:
#
#   scripts/connect-google-health/connect.sh [flags]      # --help lists them
#
# On first use (and whenever this tool, its lock file or the two libraries change) it creates a
# private virtual environment outside the repository and installs into it:
#   1. every third-party package from requirements.lock, each checked against its SHA-256 hash
#      (--require-hashes) and installed from wheels only (no package's own build code runs);
#   2. libs/pulsebridge_fhir, libs/pulsebridge_gcp and this tool, without dependencies and
#      without build isolation, so the build backend (hatchling) is the hashed one from step 1.
# Then it runs connect-google-health with your flags. Nothing unpinned or unhashed is installed.
#
# Needs Python 3.13 or newer (set PYTHON to choose one) and `gcloud auth application-default
# login`. The environment lives in CONNECT_VENV (default ~/.cache/pulsebridge/connect-google-health).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
python="${PYTHON:-python3}"
venv="${CONNECT_VENV:-${XDG_CACHE_HOME:-$HOME/.cache}/pulsebridge/connect-google-health}"
# pip 24.2 is what Python 3.13.0 ships; --require-hashes, --only-binary and --no-build-isolation
# all work there. The venv's own pip is used as it is: upgrading it would fetch an unhashed pip.
min_pip="24.2"

# A fingerprint of everything installed from the checkout (fingerprint.py says how).
fingerprint="$("$python" -I "$here/fingerprint.py" "$repo" \
  scripts/connect-google-health libs/pulsebridge_fhir libs/pulsebridge_gcp)"

if [ ! -e "$venv/.installed-$fingerprint" ]; then
  echo "Installing connect-google-health into $venv (first run or after an update)..." >&2
  "$python" -m venv "$venv"
  if ! "$venv/bin/python" - "$min_pip" <<'PY'
import sys
from importlib.metadata import version
have = tuple(int(p) for p in version("pip").split(".")[:2])
need = tuple(int(p) for p in sys.argv[1].split("."))
sys.exit(0 if have >= need else 1)
PY
  then
    echo "connect.sh: pip $min_pip or newer is needed in the virtual environment; use a newer Python (PYTHON=...)." >&2
    exit 2
  fi
  "$venv/bin/python" -m pip install --quiet --disable-pip-version-check \
    --require-hashes --only-binary :all: --no-deps -r "$here/requirements.lock"
  "$venv/bin/python" -m pip install --quiet --disable-pip-version-check \
    --no-deps --no-build-isolation \
    "$repo/libs/pulsebridge_fhir" "$repo/libs/pulsebridge_gcp" "$here"
  rm -f "$venv"/.installed-*
  touch "$venv/.installed-$fingerprint"
fi

exec "$venv/bin/connect-google-health" "$@"
