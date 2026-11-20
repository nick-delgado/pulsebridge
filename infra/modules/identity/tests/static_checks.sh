#!/usr/bin/env bash
# Static checks on the identity module that `terraform test` cannot express (T44).
#
# Until T44 this module could write one secret value (dev's reviewer password, through the
# write-only argument secret_data_wo), and tests/state_scan.py proved that value never reached
# state. D16 removed the password, and with it every secret version: the module now creates only
# empty secrets, and scripts add the values after apply. A mocked `terraform test` can only
# assert on resources that exist, so it cannot show that nobody adds a version again. This script
# reads the committed top-level *.tf files (comments stripped) and fails if:
#   1. a secret version (google_secret_manager_secret_version) or a secret value argument
#      (secret_data, secret_data_wo) appears: a value written by Terraform is in its plan, and
#      secret_data also in its state;
#   2. a data source reads a secret version (it would copy the value into state);
#   3. a service account key (google_service_account_key) appears;
#   4. an authoritative IAM resource (*_iam_binding, *_iam_policy) appears: those replace grants
#      made by other modules or by hand;
#   5. the old reviewer password is still referenced.
#
# Usage: infra/modules/identity/tests/static_checks.sh [module dir]   (needs bash, grep, sed)

set -euo pipefail

here="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
fail=0

tf_files=()
for f in "$here"/*.tf; do
  tf_files+=("$f")
done
if [[ ! -e "${tf_files[0]}" ]]; then
  echo "FAIL: no .tf files in $here"
  exit 1
fi

# Code only: drop everything after a # (no string in this module holds a #).
code="$(sed 's/#.*$//' "${tf_files[@]}")"

check() { # check <description> <extended regex>
  if grep -Eq -- "$2" <<<"$code"; then
    echo "FAIL: $1"
    grep -En -- "$2" <<<"$code" | sed 's/^/    /'
    fail=1
  else
    echo "ok:   $1"
  fi
}

check "no secret version resource" 'resource[[:space:]]+"google_secret_manager_secret_version"'
check "no secret value argument (secret_data, secret_data_wo)" '\bsecret_data(_wo)?\b'
check "no data source reading a secret version" 'data[[:space:]]+"google_secret_manager_secret_version'
check "no service account key" 'google_service_account_key'
check "no authoritative IAM resource" 'resource[[:space:]]+"google_[a-z0-9_]+_iam_(binding|policy)"'
check "no reviewer password (D16 replaced it)" 'reviewer[-_]password'

exit "$fail"
