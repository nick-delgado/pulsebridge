#!/usr/bin/env bash
# Static checks on the project-base configuration that `terraform test` cannot express. A test can
# assert on resources it names, but it cannot list every resource of a TYPE, so it would not
# notice a new google_compute_firewall added outside the `allow` map (for example one opening
# 22, 3389 or 2575 to 0.0.0.0/0). This script fails if:
#   - any google_compute_firewall other than "allow" (the for_each map) and "deny_all_ingress" is
#     declared, so every rule that allows traffic goes through the map the tests check;
#   - any `module` block is declared, so a firewall rule cannot hide in a child module.
#
# Usage: infra/modules/project-base/tests/static_checks.sh   (from anywhere; needs bash and grep)

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0

# Committed configuration only: top-level *.tf files (override files are git-ignored).
tf_files=()
for f in "$here"/*.tf; do
  case "$f" in *_override.tf | */override.tf) continue ;; esac
  tf_files+=("$f")
done

firewalls="$(grep -nE '^[[:space:]]*resource[[:space:]]+"google_compute_firewall"[[:space:]]+"' "${tf_files[@]}" || true)"
unexpected="$(printf '%s\n' "$firewalls" | grep -vE '"google_compute_firewall"[[:space:]]+"(allow|deny_all_ingress)"' | grep -v '^$' || true)"
if [[ -n "$unexpected" ]]; then
  echo "FAIL: only google_compute_firewall \"allow\" and \"deny_all_ingress\" may be declared; add allow rules to local.allow_rules"
  echo "$unexpected"
  fail=1
else
  echo "ok:   firewall rules are only the allow map and the explicit deny-all"
fi

# The two expected rules must actually be there, or the check above passes vacuously.
for name in allow deny_all_ingress; do
  if ! printf '%s\n' "$firewalls" | grep -qE "\"google_compute_firewall\"[[:space:]]+\"$name\""; then
    echo "FAIL: google_compute_firewall \"$name\" is missing"
    fail=1
  fi
done

modules="$(grep -nE '^[[:space:]]*module[[:space:]]+"' "${tf_files[@]}" || true)"
if [[ -n "$modules" ]]; then
  echo "FAIL: no module calls (a firewall rule could hide in a child module)"
  echo "$modules"
  fail=1
else
  echo "ok:   no module calls"
fi

exit "$fail"
