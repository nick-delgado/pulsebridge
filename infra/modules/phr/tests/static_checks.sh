#!/usr/bin/env bash
# Static checks on the phr module that `terraform test` cannot express. A test asserts on the
# resources it names; it cannot list every resource of a type or see a hardcoded value in a
# resource it does not look at. This script reads the committed top-level *.tf files (comments
# stripped) and fails if:
#   1. any public principal (allUsers, allAuthenticatedUsers) appears;
#   2. invoker_iam_disabled = true appears anywhere but in the fhir-proxy service, or more than
#      once; every other invoker_iam_disabled is false; ingress ALL appears anywhere but there;
#   3. any min_instance_count is not 0, or scaling uses manual_instance_count (always-on);
#   4. any authoritative IAM resource (*_iam_binding, *_iam_policy) or project-level IAM;
#   5. any service account key (google_service_account_key);
#   6. any IAM member resource other than google_cloud_run_v2_service_iam_member.invoker, or a
#      `member` not built from var.service_account_emails;
#   7. a secret reaches an environment variable (value_source / secret_key_ref), or a secret
#      volume is pinned to anything but "latest";
#   8. a project or domain is hardcoded: a literal `project = "..."`, a projects/<id>/ path, a
#      service account email, an Artifact Registry path, a literal https://<host> outside
#      variables.tf (whose descriptions and messages carry examples), or a literal site or
#      serviceId;
#   9. a `module` block is declared (resources could hide in a child module);
#  10. a Firebase Hosting version or release is declared (Terraform cannot upload the page; a
#      release would replace the live site with an empty one; hosting.tf explains);
#  11. fhir-proxy lost its depends_on on the cuff-sim invoker grant.
#
# Usage: infra/modules/phr/tests/static_checks.sh [module dir]   (needs bash, grep, sed, awk)

set -euo pipefail

here="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
fail=0

tf_files=()
for f in "$here"/*.tf; do
  case "$f" in *_override.tf | */override.tf) continue ;; esac
  tf_files+=("$f")
done
if [[ ${#tf_files[@]} -eq 0 ]]; then
  echo "FAIL: no .tf files in $here"
  exit 1
fi

# Strip '#' comments (this module uses no '#' inside strings) and keep file:line prefixes.
strip() { for f in "$@"; do sed -e 's/#.*$//' "$f" | awk -v f="$(basename "$f")" '{print f ":" NR ": " $0}'; done; }
code="$(strip "${tf_files[@]}")"
code_not_vars="$(printf '%s\n' "$code" | grep -v '^variables\.tf:' || true)"

check() { # check <description> <offending lines>
  if [[ -n "$2" ]]; then
    echo "FAIL: $1"
    printf '%s\n' "$2"
    fail=1
  else
    echo "ok:   $1"
  fi
}

# The text of one resource block: block <file> <type> <name>
block() {
  awk -v r="resource \"$2\" \"$3\"" 'index($0, r) == 1 {f=1} f {print} f && /^}/ {exit}' "$here/$1" | sed -e 's/#.*$//'
}

# 1. Public principals.
check "no allUsers / allAuthenticatedUsers" \
  "$(printf '%s\n' "$code" | grep -E 'allUsers|allAuthenticatedUsers' || true)"

# 2. Exactly one public service: fhir-proxy, made public by disabling the invoker check.
proxy_block="$(block fhir_proxy.tf google_cloud_run_v2_service fhir_proxy)"
n_disabled="$(printf '%s\n' "$code" | grep -cE 'invoker_iam_disabled[[:space:]]*=[[:space:]]*true[[:space:]]*$' || true)"
if [[ "$n_disabled" -eq 1 ]] && printf '%s\n' "$proxy_block" | grep -qE 'invoker_iam_disabled[[:space:]]*=[[:space:]]*true[[:space:]]*$'; then
  echo "ok:   invoker_iam_disabled = true only on fhir-proxy"
else
  echo "FAIL: invoker_iam_disabled = true must appear exactly once, in google_cloud_run_v2_service.fhir_proxy ($n_disabled found)"
  fail=1
fi
check "every other invoker_iam_disabled is the literal false" \
  "$(printf '%s\n' "$code" | grep -E 'invoker_iam_disabled' | grep -vE 'invoker_iam_disabled[[:space:]]*=[[:space:]]*(true|false)[[:space:]]*$' || true)"
n_all="$(printf '%s\n' "$code" | grep -cE 'INGRESS_TRAFFIC_ALL' || true)"
if [[ "$n_all" -eq 1 ]] && printf '%s\n' "$proxy_block" | grep -qE 'ingress[[:space:]]*=[[:space:]]*"INGRESS_TRAFFIC_ALL"'; then
  echo "ok:   ingress ALL only on fhir-proxy"
else
  echo "FAIL: INGRESS_TRAFFIC_ALL must appear exactly once, in google_cloud_run_v2_service.fhir_proxy ($n_all found)"
  fail=1
fi
check "every ingress is a literal" \
  "$(printf '%s\n' "$code" | grep -E '^[^:]+:[0-9]+:[[:space:]]*ingress[[:space:]]*=' | grep -vE 'ingress[[:space:]]*=[[:space:]]*"INGRESS_TRAFFIC_(ALL|INTERNAL_ONLY)"[[:space:]]*$' || true)"

# 3. Scale to zero. Every min_instance_count must be the literal 0; a variable could be set >0.
check "every min_instance_count is 0" \
  "$(printf '%s\n' "$code" | grep -E 'min_instance_count' | grep -vE 'min_instance_count[[:space:]]*=[[:space:]]*0[[:space:]]*$' || true)"
check "no manual_instance_count" \
  "$(printf '%s\n' "$code" | grep -E 'manual_instance_count' || true)"
n_min="$(printf '%s\n' "$code" | grep -cE 'min_instance_count[[:space:]]*=[[:space:]]*0[[:space:]]*$' || true)"
n_svc="$(printf '%s\n' "$code" | grep -cE 'resource[[:space:]]+"google_cloud_run_v2_service"[[:space:]]+"' || true)"
if [[ "$n_svc" -lt 2 || "$n_min" -lt "$n_svc" ]]; then
  echo "FAIL: each google_cloud_run_v2_service needs min_instance_count = 0 ($n_svc services, $n_min settings)"
  fail=1
fi

# 4. Authoritative or project-level IAM.
check "no authoritative IAM (_iam_binding, _iam_policy)" \
  "$(printf '%s\n' "$code" | grep -E 'resource[[:space:]]+"[a-z0-9_]+_iam_(binding|policy)"' || true)"
check "no project-level IAM" \
  "$(printf '%s\n' "$code" | grep -E 'resource[[:space:]]+"google_project_iam_' || true)"

# 5. Service account keys.
check "no service account key resources" \
  "$(printf '%s\n' "$code" | grep -E 'resource[[:space:]]+"google_service_account_key"' || true)"

# 6. The IAM member resources are exactly the reviewed one.
members="$(printf '%s\n' "$code" | grep -E 'resource[[:space:]]+"[a-z0-9_]+_iam_member"' || true)"
check "IAM members are only google_cloud_run_v2_service_iam_member.invoker" \
  "$(printf '%s\n' "$members" | grep -vE '"google_cloud_run_v2_service_iam_member"[[:space:]]+"invoker"' | grep -v '^$' || true)"
if ! printf '%s\n' "$members" | grep -qE '"google_cloud_run_v2_service_iam_member"[[:space:]]+"invoker"'; then
  echo "FAIL: expected IAM resource missing: google_cloud_run_v2_service_iam_member.invoker"
  fail=1
fi
check "every member is serviceAccount:\${var.service_account_emails[...]}" \
  "$(printf '%s\n' "$code" | grep -E '^[^:]+:[0-9]+:[[:space:]]*member[[:space:]]*=' | grep -vE 'member[[:space:]]*=[[:space:]]*"serviceAccount:\$\{var\.service_account_emails\[' || true)"

# 7. Secrets only as files, always at "latest".
check "no secret in an environment variable (value_source / secret_key_ref)" \
  "$(printf '%s\n' "$code" | grep -E 'value_source|secret_key_ref' || true)"
check "every secret volume item is version \"latest\"" \
  "$(printf '%s\n' "$code" | grep -vE '^versions\.tf:' | grep -E '^[^:]+:[0-9]+:[[:space:]]*version[[:space:]]*=' | grep -vE 'version[[:space:]]*=[[:space:]]*"latest"[[:space:]]*$' || true)"

# 8. Hardcoded project or domain.
check "no literal project argument" \
  "$(printf '%s\n' "$code" | grep -E '(^|[^a-z_])project[[:space:]]*=[[:space:]]*"' || true)"
check "no literal projects/<id>/ path" \
  "$(printf '%s\n' "$code" | grep -E 'projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/' || true)"
check "no literal service account email" \
  "$(printf '%s\n' "$code" | grep -E '@[a-z0-9-]+\.iam\.gserviceaccount\.com' || true)"
check "no literal Artifact Registry path" \
  "$(printf '%s\n' "$code" | grep -E 'docker\.pkg\.dev/[a-z]' || true)"
check "no literal https://<host> outside variables.tf" \
  "$(printf '%s\n' "$code_not_vars" | grep -E 'https://[A-Za-z0-9]' || true)"
check "no literal site or serviceId" \
  "$(printf '%s\n' "$code" | grep -E '(site_id|site|serviceId)[[:space:]]*=[[:space:]]*"' || true)"

# 9. Child modules.
check "no module calls" \
  "$(printf '%s\n' "$code" | grep -E '^[^:]+:[0-9]+:[[:space:]]*module[[:space:]]+"' || true)"

# 10. No Hosting version or release: Terraform cannot upload the page.
check "no google_firebase_hosting_version / _release" \
  "$(printf '%s\n' "$code" | grep -E 'resource[[:space:]]+"google_firebase_hosting_(version|release|channel)"' || true)"

# 11. Ordering: the proxy's revision waits for its invoker grant on cuff-sim.
if printf '%s\n' "$proxy_block" | grep -qE 'depends_on[[:space:]]*=[[:space:]]*\[[^]]*google_cloud_run_v2_service_iam_member\.invoker'; then
  echo "ok:   fhir-proxy depends on the cuff-sim invoker binding"
else
  echo "FAIL: google_cloud_run_v2_service.fhir_proxy must have depends_on = [google_cloud_run_v2_service_iam_member.invoker]"
  fail=1
fi

exit "$fail"
