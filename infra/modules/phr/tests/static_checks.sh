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
#   6. any IAM member resource other than google_cloud_run_v2_service_iam_member.invoker and
#      .page_deployer_viewer and google_storage_bucket_iam_member.demo_counter_writer, a
#      `member` not built from var.service_account_emails (or, in page_deployer_viewer only,
#      var.page_deployer_service_account_email), a page deployer grant that is not
#      roles/run.viewer on google_cloud_run_v2_service.fhir_proxy (T41), or a counter bucket
#      grant that is not roles/storage.objectUser for fhir-proxy on that bucket (T44);
#   7. a secret reaches an environment variable (value_source / secret_key_ref), or a secret
#      volume is pinned to anything but "latest";
#   8. a project or domain is hardcoded: a literal `project = "..."`, a projects/<id>/ path, a
#      service account email, an Artifact Registry path, a literal https://<host> outside
#      variables.tf (whose descriptions and messages carry examples), or a literal site or
#      serviceId;
#   9. a `module` block is declared (resources could hide in a child module);
#  10. a Firebase Hosting version or release is declared (Terraform cannot upload the page; a
#      release would replace the live site with an empty one; hosting.tf explains);
#  11. fhir-proxy lost its depends_on on the cuff-sim invoker grant or on the counter bucket's
#      grant (T44);
#  12. the demo (T44): a bucket without uniform access or enforced public access prevention, a
#      reCAPTCHA key that allows all domains or has testing options (a fixed testing score
#      would let every bot through), or a key that is not SCORE.
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

# 6. The IAM member resources are exactly the reviewed two.
members="$(printf '%s\n' "$code" | grep -E 'resource[[:space:]]+"[a-z0-9_]+_iam_member"' || true)"
check "IAM members are only the run invoker and page_deployer_viewer grants and the counter bucket's writer" \
  "$(printf '%s\n' "$members" | grep -vE '"google_cloud_run_v2_service_iam_member"[[:space:]]+"(invoker|page_deployer_viewer)"[[:space:]]*\{' | grep -vE '"google_storage_bucket_iam_member"[[:space:]]+"demo_counter_writer"[[:space:]]*\{' | grep -v '^$' || true)"
counter_block="$(block demo.tf google_storage_bucket_iam_member demo_counter_writer)"
if printf '%s\n' "$counter_block" | grep -qE '^[[:space:]]*bucket[[:space:]]*=[[:space:]]*google_storage_bucket\.demo_counters\.name[[:space:]]*$' \
  && printf '%s\n' "$counter_block" | grep -qE '^[[:space:]]*role[[:space:]]*=[[:space:]]*"roles/storage\.objectUser"[[:space:]]*$' \
  && printf '%s\n' "$counter_block" | grep -qE '^[[:space:]]*member[[:space:]]*=[[:space:]]*"serviceAccount:\$\{var\.service_account_emails\["fhir-proxy"\]\}"[[:space:]]*$'; then
  echo "ok:   the counter bucket's one grant is roles/storage.objectUser for fhir-proxy"
else
  echo "FAIL: google_storage_bucket_iam_member.demo_counter_writer must grant roles/storage.objectUser on google_storage_bucket.demo_counters to var.service_account_emails[\"fhir-proxy\"]"
  fail=1
fi
for r in invoker page_deployer_viewer; do
  if ! printf '%s\n' "$members" | grep -qE "\"google_cloud_run_v2_service_iam_member\"[[:space:]]+\"$r\""; then
    echo "FAIL: expected IAM resource missing: google_cloud_run_v2_service_iam_member.$r"
    fail=1
  fi
done
viewer_block="$(block main.tf google_cloud_run_v2_service_iam_member page_deployer_viewer)"
check "every member outside page_deployer_viewer is serviceAccount:\${var.service_account_emails[...]}" \
  "$(printf '%s\n' "$code" | grep -E '^[^:]+:[0-9]+:[[:space:]]*member[[:space:]]*=' | grep -vE 'member[[:space:]]*=[[:space:]]*"serviceAccount:\$\{var\.service_account_emails\[' | grep -vE 'member[[:space:]]*=[[:space:]]*"serviceAccount:\$\{var\.page_deployer_service_account_email\}"[[:space:]]*$' || true)"
n_pd="$(printf '%s\n' "$code" | grep -cE 'var\.page_deployer_service_account_email\}' || true)"
if [[ "$n_pd" -eq 1 ]] \
  && printf '%s\n' "$viewer_block" | grep -qE 'member[[:space:]]*=[[:space:]]*"serviceAccount:\$\{var\.page_deployer_service_account_email\}"' \
  && printf '%s\n' "$viewer_block" | grep -qE '^[[:space:]]*role[[:space:]]*=[[:space:]]*"roles/run\.viewer"[[:space:]]*$' \
  && printf '%s\n' "$viewer_block" | grep -qE '^[[:space:]]*name[[:space:]]*=[[:space:]]*google_cloud_run_v2_service\.fhir_proxy\.name[[:space:]]*$'; then
  echo "ok:   the page deployer gets roles/run.viewer on fhir-proxy only, in page_deployer_viewer only"
else
  echo "FAIL: var.page_deployer_service_account_email must be the member of page_deployer_viewer only ($n_pd uses), with role roles/run.viewer on google_cloud_run_v2_service.fhir_proxy.name"
  fail=1
fi

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
# T44: and on the counter bucket grant, so the first demo session does not meet a 403 there.
for edge in google_cloud_run_v2_service_iam_member.invoker google_storage_bucket_iam_member.demo_counter_writer; do
  if printf '%s\n' "$proxy_block" | grep -qE "depends_on[[:space:]]*=[[:space:]]*\[[^]]*${edge//./\\.}([^a-z_]|$)"; then
    echo "ok:   fhir-proxy depends on $edge"
  else
    echo "FAIL: google_cloud_run_v2_service.fhir_proxy's depends_on must include $edge"
    fail=1
  fi
done

# 12. The demo's bucket and key (T44).
bucket_block="$(block demo.tf google_storage_bucket demo_counters)"
key_block="$(block demo.tf google_recaptcha_enterprise_key demo)"
if printf '%s\n' "$bucket_block" | grep -qE '^[[:space:]]*uniform_bucket_level_access[[:space:]]*=[[:space:]]*true[[:space:]]*$' \
  && printf '%s\n' "$bucket_block" | grep -qE '^[[:space:]]*public_access_prevention[[:space:]]*=[[:space:]]*"enforced"[[:space:]]*$'; then
  echo "ok:   the counter bucket has uniform access and enforced public access prevention"
else
  echo "FAIL: google_storage_bucket.demo_counters needs uniform_bucket_level_access = true and public_access_prevention = \"enforced\""
  fail=1
fi
check "no other bucket" \
  "$(printf '%s\n' "$code" | grep -E 'resource[[:space:]]+"google_storage_bucket"[[:space:]]+"' | grep -v '"demo_counters"' || true)"
check "no reCAPTCHA key that allows every domain" \
  "$(printf '%s\n' "$code" | grep -E 'allow_all_domains[[:space:]]*=' | grep -vE 'allow_all_domains[[:space:]]*=[[:space:]]*false[[:space:]]*$' || true)"
check "no reCAPTCHA testing options (a fixed score lets every bot through)" \
  "$(printf '%s\n' "$code" | grep -E 'testing_options|testing_score' || true)"
if printf '%s\n' "$key_block" | grep -qE '^[[:space:]]*integration_type[[:space:]]*=[[:space:]]*"SCORE"[[:space:]]*$'; then
  echo "ok:   the demo key is a SCORE key"
else
  echo "FAIL: google_recaptcha_enterprise_key.demo must have integration_type = \"SCORE\""
  fail=1
fi

exit "$fail"
