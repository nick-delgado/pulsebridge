#!/usr/bin/env bash
# Static checks on the openemr module that `terraform test` cannot express ("no block of this kind
# exists anywhere", "no literal of this shape is committed"). Fails if:
#   - any resource, data source, module call or provider block is declared: this module only
#     renders values (README, "What this module is"), so in particular it holds no IAM binding and
#     no firewall rule (those live in identity and project-base);
#   - a project ID is hardcoded (a literal projects/<id>/ path, or `project = "..."`);
#   - a password-, token- or key-looking literal is committed in the configuration, the templates
#     or the scripts (an assignment of a quoted literal to a name like *password*, *secret*,
#     *token*, or a PEM private-key header);
#   - a script turns on tracing (`set -x`), which would print every expanded value;
#   - a script uses `docker run -v`/`--volume` (bind mounts must be --mount type=bind), publishes
#     with -p, or switches TLS verification off (curl -k / --insecure);
#   - a variable is named like a secret value.
#
# Usage: infra/modules/openemr/tests/static_checks.sh   (from anywhere; needs bash, grep, sed)

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0
ok() { echo "ok:   $*"; }
bad() {
  echo "FAIL: $*"
  fail=1
}

tf_files=()
for f in "$here"/*.tf; do
  case "$f" in *_override.tf | */override.tf) continue ;; esac
  tf_files+=("$f")
done
scripts=("$here"/scripts/*.sh "$here"/scripts/*.php "$here"/templates/*.tftpl)

# Code lines only: drop whole-line comments (#, //) and trailing "# ..." comments, keep file:line.
code_lines() {
  grep -nH -v -E '^[[:space:]]*(#|//)' "$@" | sed -E 's/[[:space:]]+#[^"]*$//' || true
}

# 1. No resources, data sources, modules or providers at all.
hits="$(grep -nH -E '^[[:space:]]*(resource|data|module|provider)[[:space:]]+"' "${tf_files[@]}" || true)"
if [[ -n "$hits" ]]; then bad "this module must declare no resource, data source, module or provider:"; echo "$hits"; else ok "no resources, data sources, modules or providers (so no IAM and no firewall resources)"; fi
hits="$(grep -nH -E 'google_[a-z0-9_]*_iam_|google_compute_firewall|required_providers' "${tf_files[@]}" || true)"
if [[ -n "$hits" ]]; then bad "IAM, firewall or provider requirement mentioned in configuration:"; echo "$hits"; else ok "no IAM, firewall or provider requirement"; fi

# 2. No hardcoded project.
hits="$(code_lines "${tf_files[@]}" "${scripts[@]}" | grep -E 'projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/' || true)"
if [[ -n "$hits" ]]; then bad "hardcoded project path:"; echo "$hits"; else ok "no hardcoded project paths"; fi
hits="$(code_lines "${tf_files[@]}" | grep -E '(^|[^_a-z])project(_id)?[[:space:]]*=[[:space:]]*"' || true)"
if [[ -n "$hits" ]]; then bad "literal project argument:"; echo "$hits"; else ok "no literal project arguments"; fi

# 3. No password-, token- or key-looking literal. An assignment of a non-empty quoted literal that
#    does not start with $ or { to a name containing password, passwd, secret, token or pwd.
hits="$(code_lines "${tf_files[@]}" "${scripts[@]}" |
  grep -iE '(password|passwd|pwd|secret|token)[a-z0-9_]*[[:space:]]*[:=]>?[[:space:]]*["'"'"'][^"'"'"'$\{%]' |
  grep -vE 'OPENEMR_(DB|ADMIN)_SECRET=|MARIADB_ROOT_SECRET=|description|error_message' || true)"
if [[ -n "$hits" ]]; then bad "password-, secret- or token-looking literal:"; echo "$hits"; else ok "no password-, secret- or token-looking literals"; fi
hits="$(grep -rnH -E -- '-----BEGIN [A-Z ]*PRIVATE KEY-----' "${tf_files[@]}" "${scripts[@]}" "$here/tests" || true)"
if [[ -n "$hits" ]]; then bad "PEM private-key header committed:"; echo "$hits"; else ok "no PEM private-key material"; fi

# 4. No tracing.
hits="$(code_lines "${scripts[@]}" | grep -E 'set[[:space:]]+-[a-zA-Z]*x|xtrace|BASH_XTRACEFD' || true)"
if [[ -n "$hits" ]]; then bad "a script enables tracing:"; echo "$hits"; else ok "no tracing in the scripts"; fi

# 5. Docker and curl usage: bind mounts only with --mount, no -p, no TLS switched off.
#    -v and -p are flagged in their docker form (an argument with a colon: SRC:DST, HOST:CONTAINER),
#    so `mkdir -p dir` and `awk -v name=value` are not.
hits="$(code_lines "$here"/scripts/*.sh | grep -E -- '(^|[[:space:]])(-v|-p)[[:space:]]+"?[^[:space:]]*:|--volume|(^|[[:space:]])(-k|--insecure)([[:space:]]|$)' || true)"
if [[ -n "$hits" ]]; then bad "docker -v/--volume/-p or curl -k/--insecure in a script:"; echo "$hits"; else ok "no -v/--volume/-p and no curl -k/--insecure"; fi
n="$(code_lines "$here"/scripts/*.sh | grep -cE -- '--publish' || true)"
if [[ "$n" -eq 1 ]]; then ok "exactly one --publish in the scripts"; else bad "expected exactly one --publish in the scripts, found $n"; fi

# 6. No variable that would carry a secret into Terraform (and so into state and metadata).
hits="$(grep -nH -E '^[[:space:]]*variable[[:space:]]+"[^"]*(password|passwd|secret_value|token|private_key|api_key)[^"]*"' "${tf_files[@]}" || true)"
if [[ -n "$hits" ]]; then bad "a variable is named like a secret value:"; echo "$hits"; else ok "no secret-valued variables"; fi

exit "$fail"
