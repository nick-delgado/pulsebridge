#!/usr/bin/env bash
# Static checks on the bootstrap configuration that `terraform test` cannot express (a test can
# assert on resources that exist, but not that a resource TYPE is absent).
#
# AC: no key files are created. Bootstrap must declare no credential-minting resources at all:
#   - google_service_account_key: a long-lived JSON key for a service account
#   - google_storage_hmac_key:    an HMAC key (access ID + secret) for a service account
#   - google_apikeys_key:         an API key
# and no `module` blocks, so a key cannot hide inside a child module.
# It must also declare no `backend` block: bootstrap's state starts local by design (README.md).
#
# Usage: infra/bootstrap/tests/static_checks.sh   (from anywhere; needs only bash and grep)

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0

check_absent() {
  local pattern="$1" reason="$2" hits
  # Only committed configuration: top-level *.tf files (override files are git-ignored).
  hits="$(grep -nE "$pattern" "$here"/*.tf | grep -vE '_override\.tf:' || true)"
  if [[ -n "$hits" ]]; then
    echo "FAIL: $reason"
    echo "$hits"
    fail=1
  else
    echo "ok:   $reason"
  fi
}

check_absent '^[[:space:]]*(resource|data)[[:space:]]+"(google_service_account_key|google_storage_hmac_key|google_apikeys_key)"' \
  "no service account keys, HMAC keys or API keys are declared"
check_absent '^[[:space:]]*module[[:space:]]+"' \
  "no module calls (a key cannot be hidden in a child module)"
check_absent '^[[:space:]]*backend[[:space:]]+"' \
  "no backend block (bootstrap state starts local)"

exit "$fail"
