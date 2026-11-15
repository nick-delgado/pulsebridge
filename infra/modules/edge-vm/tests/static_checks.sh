#!/usr/bin/env bash
# Static checks on the edge-vm module that `terraform test` cannot express: a plan test can
# assert on the resources it names, but not "no resource of this kind exists anywhere". This
# script reads the committed configuration and the startup script and fails if:
#   - any `access_config` block appears (that is what gives an instance an external IP);
#   - any firewall resource is declared (D14: the one rule lives in project-base);
#   - the Compute Engine default account is used (its email, or the data source that looks it up);
#   - a service account key is created, or the adapter is given a key file (--credentials);
#   - an instance metadata key looks like it holds a password, token, key or SSH keys;
#   - a variable is named like a password or key (no secret may enter through Terraform);
#   - a project ID is hardcoded (a literal projects/<id>/ other than the public cos-cloud images,
#     or a literal `project = "..."`);
#   - the startup script turns on tracing (`set -x`), which would print secrets;
#   - any `module` block is declared, so none of the above can hide in a child module.
#
# Usage: infra/modules/edge-vm/tests/static_checks.sh   (from anywhere; needs bash, grep, awk)

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0

ok() { echo "ok:   $*"; }
bad() {
  echo "FAIL: $*"
  fail=1
}

# Committed configuration only: top-level *.tf files (override files are git-ignored).
tf_files=()
for f in "$here"/*.tf; do
  case "$f" in *_override.tf | */override.tf) continue ;; esac
  tf_files+=("$f")
done
scripts=("$here/scripts/edge-startup.sh" "$here/templates/startup-config.sh.tftpl")

# Code lines only: drop whole-line comments and trailing "# ..." comments, keep file:line.
code_lines() {
  grep -nH -v -E '^[[:space:]]*(#|//)' "$@" | sed -E 's/[[:space:]]+#[^"]*$//' || true
}

# 1. No external IP.
hits="$(code_lines "${tf_files[@]}" | grep -E 'access_config' || true)"
if [[ -n "$hits" ]]; then bad "access_config found (it would give the VM an external IP):"; echo "$hits"; else ok "no access_config"; fi

# 2. No firewall resources.
hits="$(code_lines "${tf_files[@]}" | grep -E 'resource[[:space:]]+"google_compute_(firewall|network_firewall_policy|firewall_policy)' || true)"
if [[ -n "$hits" ]]; then bad "firewall resource declared (D14: firewall rules live in project-base):"; echo "$hits"; else ok "no firewall resources"; fi

# 3. No default Compute Engine account. The email may appear only in variables.tf prose (the
#    description and error message that explain why it is refused).
hits="$(code_lines "${tf_files[@]}" "${scripts[@]}" | grep -E 'developer\.gserviceaccount\.com|google_compute_default_service_account' |
  grep -v -E '^[^:]*/variables\.tf:[0-9]+:[[:space:]]*(description|error_message)[[:space:]]*=' || true)"
if [[ -n "$hits" ]]; then bad "the Compute Engine default service account is referenced:"; echo "$hits"; else ok "no default Compute Engine account"; fi

# The instance must name its account from the variable.
if code_lines "$here/main.tf" | grep -qE 'email[[:space:]]*=[[:space:]]*var\.service_account_email'; then
  ok "instance service account comes from var.service_account_email"
else
  bad "the instance's service_account.email is not var.service_account_email"
fi

# 4. No service account keys, no key file for the adapter.
hits="$(code_lines "${tf_files[@]}" "${scripts[@]}" | grep -E 'google_service_account_key|--credentials|GOOGLE_APPLICATION_CREDENTIALS' || true)"
if [[ -n "$hits" ]]; then bad "service account key or key file found:"; echo "$hits"; else ok "no service account keys or key files"; fi

# 5. Metadata keys. They are all in local.instance_metadata in main.tf; extract that block by
#    counting parentheses and braces, then list the quoted keys on the left of "=" or "=>".
metadata_block="$(awk '
  /^[[:space:]]*instance_metadata[[:space:]]*=/ { on = 1 }
  on {
    print
    n += gsub(/[({]/, "&") - gsub(/[)}]/, "&")
    if (started && n <= 0) exit
    if (n > 0) started = 1
  }' "$here/main.tf")"
if [[ -z "$metadata_block" ]]; then
  bad "local.instance_metadata not found in main.tf (the metadata check would pass vacuously)"
else
  keys="$(printf '%s\n' "$metadata_block" | grep -oE '"[^"]+"[[:space:]]*(=>|=)' | sed -E 's/"([^"]+)".*/\1/' || true)"
  if ! printf '%s\n' "$keys" | grep -qx 'startup-script'; then
    bad "could not read the metadata keys (startup-script not among them)"
  fi
  suspicious="$(printf '%s\n' "$keys" | grep -v -x 'block-project-ssh-keys' | grep -iE 'ssh-?keys|sshkeys|password|passwd|secret|token|credential|private-?key|api-?key' || true)"
  if [[ -n "$suspicious" ]]; then bad "metadata key looks like it carries a secret or SSH keys:"; echo "$suspicious"; else ok "metadata keys: $(echo "$keys" | tr '\n' ' ')"; fi
fi

# 6. No variable that would carry a secret into Terraform (and so into state).
hits="$(grep -nH -E '^[[:space:]]*variable[[:space:]]+"[^"]*(password|passwd|secret_value|token|private_key|api_key)[^"]*"' "${tf_files[@]}" || true)"
if [[ -n "$hits" ]]; then bad "a variable is named like a secret value:"; echo "$hits"; else ok "no secret-valued variables"; fi

# 7. No hardcoded project. Literal projects/<id>/ paths are allowed only for the public
#    cos-cloud image project; every other project comes from a variable.
hits="$(code_lines "${tf_files[@]}" "${scripts[@]}" | grep -oE '^[^:]+:[0-9]+:.*projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/' | grep -v 'projects/cos-cloud/' || true)"
if [[ -n "$hits" ]]; then bad "hardcoded project path:"; echo "$hits"; else ok "no hardcoded project paths"; fi
hits="$(code_lines "${tf_files[@]}" | grep -E '(^|[^_a-z])project[[:space:]]*=[[:space:]]*"' || true)"
if [[ -n "$hits" ]]; then bad "literal project argument:"; echo "$hits"; else ok "no literal project arguments"; fi

# 8. No tracing in the startup script (set -x / xtrace would print every expanded value).
hits="$(code_lines "${scripts[@]}" | grep -E 'set[[:space:]]+-[a-zA-Z]*x|xtrace|BASH_XTRACEFD' || true)"
if [[ -n "$hits" ]]; then bad "the startup script enables tracing:"; echo "$hits"; else ok "no tracing in the startup script"; fi

# 9. No child modules.
hits="$(grep -nH -E '^[[:space:]]*module[[:space:]]+"' "${tf_files[@]}" || true)"
if [[ -n "$hits" ]]; then bad "module call found (resources could hide in it):"; echo "$hits"; else ok "no module calls"; fi

exit "$fail"
