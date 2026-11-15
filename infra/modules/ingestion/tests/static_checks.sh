#!/usr/bin/env bash
# Static checks on the ingestion module that `terraform test` cannot express. A test asserts on
# the resources it names; it cannot list every resource of a type or see a hardcoded value in a
# resource it does not look at. This script reads the committed top-level *.tf files (comments
# stripped) and fails if:
#   1. any public principal (allUsers, allAuthenticatedUsers) appears;
#   2. any min_instance_count is not 0, or scaling uses manual_instance_count (always-on);
#   3. any authoritative IAM resource (*_iam_binding, *_iam_policy) or project-level IAM is declared;
#   4. any service account key (google_service_account_key) is declared;
#   5. any IAM member resource other than the two reviewed ones is declared, or a `member` is not
#      built from var.service_account_emails;
#   6. invoker_iam_disabled is set to anything but false, or a secret reaches an environment
#      variable (value_source / secret_key_ref);
#   7. a project is hardcoded: a literal `project = "..."`, a literal projects/<id>/ path, a
#      service account email or an Artifact Registry path with a literal project;
#   8. a `module` block is declared (resources could hide in a child module);
#   9. the push endpoint output lost its depends_on on the invoker binding, or a Scheduler job
#      lost its depends_on on the grant it needs (ordering: terraform test cannot see depends_on).
#
# Usage: infra/modules/ingestion/tests/static_checks.sh [module dir]   (needs bash, grep, sed, awk)

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
code="$(for f in "${tf_files[@]}"; do sed -e 's/#.*$//' "$f" | awk -v f="$(basename "$f")" '{print f ":" NR ": " $0}'; done)"

check() { # check <description> <offending lines>
  if [[ -n "$2" ]]; then
    echo "FAIL: $1"
    printf '%s\n' "$2"
    fail=1
  else
    echo "ok:   $1"
  fi
}

# 1. Public principals.
check "no allUsers / allAuthenticatedUsers" \
  "$(printf '%s\n' "$code" | grep -E 'allUsers|allAuthenticatedUsers' || true)"

# 2. Scale to zero. Every min_instance_count must be the literal 0; a variable could be set >0.
check "every min_instance_count is 0" \
  "$(printf '%s\n' "$code" | grep -E 'min_instance_count' | grep -vE 'min_instance_count[[:space:]]*=[[:space:]]*0[[:space:]]*$' || true)"
check "no manual_instance_count" \
  "$(printf '%s\n' "$code" | grep -E 'manual_instance_count' || true)"
# The check above passes vacuously if scaling blocks disappear; require one per service.
n_min="$(printf '%s\n' "$code" | grep -cE 'min_instance_count[[:space:]]*=[[:space:]]*0[[:space:]]*$' || true)"
n_svc="$(printf '%s\n' "$code" | grep -cE 'resource[[:space:]]+"google_cloud_run_v2_service"[[:space:]]+"' || true)"
if [[ "$n_svc" -lt 1 || "$n_min" -lt "$n_svc" ]]; then
  echo "FAIL: each google_cloud_run_v2_service needs min_instance_count = 0 ($n_svc services, $n_min settings)"
  fail=1
fi

# 3. Authoritative or project-level IAM.
check "no authoritative IAM (_iam_binding, _iam_policy)" \
  "$(printf '%s\n' "$code" | grep -E 'resource[[:space:]]+"[a-z0-9_]+_iam_(binding|policy)"' || true)"
check "no project-level IAM" \
  "$(printf '%s\n' "$code" | grep -E 'resource[[:space:]]+"google_project_iam_' || true)"

# 4. Service account keys.
check "no service account key resources" \
  "$(printf '%s\n' "$code" | grep -E 'resource[[:space:]]+"google_service_account_key"' || true)"

# 5. The IAM member resources are exactly the two reviewed ones.
members="$(printf '%s\n' "$code" | grep -E 'resource[[:space:]]+"[a-z0-9_]+_iam_member"' || true)"
check "IAM members are only google_cloud_run_v2_service_iam_member.invoker and google_cloud_run_v2_job_iam_member.seed_runner" \
  "$(printf '%s\n' "$members" | grep -vE '"google_cloud_run_v2_service_iam_member"[[:space:]]+"invoker"|"google_cloud_run_v2_job_iam_member"[[:space:]]+"seed_runner"' | grep -v '^$' || true)"
for want in '"google_cloud_run_v2_service_iam_member"[[:space:]]+"invoker"' '"google_cloud_run_v2_job_iam_member"[[:space:]]+"seed_runner"'; do
  if ! printf '%s\n' "$members" | grep -qE "$want"; then
    echo "FAIL: expected IAM resource missing: $want"
    fail=1
  fi
done
check "every member is serviceAccount:\${var.service_account_emails[...]}" \
  "$(printf '%s\n' "$code" | grep -E '^[^:]+:[0-9]+:[[:space:]]*member[[:space:]]*=' | grep -vE 'member[[:space:]]*=[[:space:]]*"serviceAccount:\$\{var\.service_account_emails\[' || true)"

# 6. IAM check on, no secrets in environment variables.
check "invoker_iam_disabled is only ever false" \
  "$(printf '%s\n' "$code" | grep -E 'invoker_iam_disabled' | grep -vE 'invoker_iam_disabled[[:space:]]*=[[:space:]]*false[[:space:]]*$' || true)"
check "no secret in an environment variable (value_source / secret_key_ref)" \
  "$(printf '%s\n' "$code" | grep -E 'value_source|secret_key_ref' || true)"

# 7. Hardcoded project. Projects come from var.project_id (or var.healthcare_project_id).
check "no literal project argument" \
  "$(printf '%s\n' "$code" | grep -E '(^|[^a-z_])project[[:space:]]*=[[:space:]]*"' || true)"
check "no literal projects/<id>/ path" \
  "$(printf '%s\n' "$code" | grep -E 'projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/' || true)"
check "no literal service account email" \
  "$(printf '%s\n' "$code" | grep -E '@[a-z0-9-]+\.iam\.gserviceaccount\.com' || true)"
check "no literal Artifact Registry path" \
  "$(printf '%s\n' "$code" | grep -E 'docker\.pkg\.dev/[a-z]' || true)"

# 8. Child modules.
check "no module calls" \
  "$(printf '%s\n' "$code" | grep -E '^[^:]+:[0-9]+:[[:space:]]*module[[:space:]]+"' || true)"

# 9. Ordering: the push endpoint output must wait for the invoker binding.
push_block="$(awk '/^output "v2_to_fhir_push_endpoint"/,/^}/' "$here/outputs.tf" | sed -e 's/#.*$//')"
if printf '%s\n' "$push_block" | grep -qE 'depends_on[[:space:]]*=[[:space:]]*\[[^]]*google_cloud_run_v2_service_iam_member\.invoker'; then
  echo "ok:   v2_to_fhir_push_endpoint depends on the invoker binding"
else
  echo "FAIL: output v2_to_fhir_push_endpoint must have depends_on = [google_cloud_run_v2_service_iam_member.invoker]"
  fail=1
fi

# Each Scheduler job must wait for the grant that lets its token in, or its first attempts fail.
sched_dep() { # sched_dep <file> <scheduler resource name> <required dependency>
  local block
  block="$(awk -v r="resource \"google_cloud_scheduler_job\" \"$2\"" 'index($0, r) == 1 {f=1} f {print} f && /^}/ {exit}' "$here/$1" | sed -e 's/#.*$//')"
  if [[ -z "$block" ]]; then
    echo "FAIL: google_cloud_scheduler_job.$2 not found in $1"
    fail=1
  elif printf '%s\n' "$block" | grep -qE "depends_on[[:space:]]*=[[:space:]]*\[[^]]*$3"; then
    echo "ok:   google_cloud_scheduler_job.$2 depends on $3"
  else
    echo "FAIL: google_cloud_scheduler_job.$2 must have depends_on = [$3]"
    fail=1
  fi
}
sched_dep health_sync.tf health_sync 'google_cloud_run_v2_service_iam_member\.invoker'
sched_dep seeder.tf seed 'google_cloud_run_v2_job_iam_member\.seed_runner'

exit "$fail"
