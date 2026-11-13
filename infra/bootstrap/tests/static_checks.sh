#!/usr/bin/env bash
# Static checks on the bootstrap configuration that `terraform test` cannot express (a test can
# assert on resources that exist, but not that a resource TYPE is absent, nor read `lifecycle`).
#
# Scans every committed configuration file in infra/bootstrap: *.tf and *.tf.json (Terraform
# loads both). Override files (*_override.tf, *_override.tf.json) are git-ignored and skipped.
#
# 1. No credential-minting resources (AC: no key files are created):
#      google_service_account_key  a long-lived JSON key for a service account
#      google_storage_hmac_key     an HMAC key (access ID + secret) for a service account
#      google_apikeys_key          an API key
# 2. No module calls, so a key or anything else cannot hide inside a child module.
# 3. No backend block: bootstrap's state starts local by design (README.md).
# 4. No authoritative IAM resources (google_*_iam_binding, google_*_iam_policy). They replace
#    the whole member list of a role (or the whole policy), so in adopt mode they would silently
#    remove existing members from a real project. Bootstrap uses additive *_iam_member only.
# 5. The state bucket keeps `prevent_destroy = true`.
#
# Usage: infra/bootstrap/tests/static_checks.sh   (from anywhere; needs only bash, grep and awk)

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
shopt -s nullglob
files=()
for f in "$here"/*.tf "$here"/*.tf.json; do
  case "$f" in *_override.tf | *_override.tf.json) continue ;; esac
  files+=("$f")
done
fail=0

report() {
  local hits="$1" reason="$2"
  if [[ -n "$hits" ]]; then
    echo "FAIL: $reason"
    echo "$hits"
    fail=1
  else
    echo "ok:   $reason"
  fi
}

# check_absent <HCL regex> <JSON regex> <reason>
# HCL declares `resource "type" "name" {`; JSON declares `"resource": { "type": { ... } }`, so in
# JSON the type (or `module`, `backend`) appears as a quoted object key.
check_absent() {
  local hcl="$1" json="$2" reason="$3" hits=""
  for f in "${files[@]}"; do
    case "$f" in
      *.tf.json) hits+="$(grep -nHE "$json" "$f" || true)" ;;
      *.tf) hits+="$(grep -nHE "$hcl" "$f" || true)" ;;
    esac
  done
  report "$hits" "$reason"
}

key_types='google_service_account_key|google_storage_hmac_key|google_apikeys_key'
check_absent "^[[:space:]]*(resource|data)[[:space:]]+\"($key_types)\"" \
  "\"($key_types)\"[[:space:]]*:" \
  "no service account keys, HMAC keys or API keys are declared"

check_absent '^[[:space:]]*module[[:space:]]+"' \
  '"module"[[:space:]]*:' \
  "no module calls (nothing can be hidden in a child module)"

check_absent '^[[:space:]]*backend[[:space:]]+"' \
  '"backend"[[:space:]]*:' \
  "no backend block (bootstrap state starts local)"

check_absent '^[[:space:]]*resource[[:space:]]+"google_[a-z0-9_]+_iam_(binding|policy)"' \
  '"google_[a-z0-9_]+_iam_(binding|policy)"[[:space:]]*:' \
  "no authoritative IAM resources (*_iam_binding, *_iam_policy); use additive *_iam_member"

# The tfstate bucket block, from its `resource` line to the first line that closes it at column 0.
bucket_block="$(awk '/^resource "google_storage_bucket" "tfstate"/{on=1} on{print} on&&/^}/{exit}' "$here"/*.tf)"
if [[ -n "$bucket_block" ]] && grep -qE '^[[:space:]]*prevent_destroy[[:space:]]*=[[:space:]]*true[[:space:]]*(#.*)?$' <<<"$bucket_block"; then
  report "" "state bucket has lifecycle { prevent_destroy = true }"
else
  report "(google_storage_bucket.tfstate has no prevent_destroy = true)" "state bucket has lifecycle { prevent_destroy = true }"
fi

exit "$fail"
