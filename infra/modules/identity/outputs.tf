output "service_account_emails" {
  description = "Service account email per workload, keyed health-sync, v2-to-fhir, cuff-sim, fhir-proxy, edge-vm, scheduler-invoker, pubsub-push (and seeder when created). Other modules use these for resource-level bindings and as the identity their resources run as."
  value       = { for k, sa in google_service_account.this : k => sa.email }
}

output "service_account_members" {
  description = "IAM member string (\"serviceAccount:<email>\") per workload, ready for the `member` argument of a `google_*_iam_member` resource."
  value       = { for k, sa in google_service_account.this : k => "serviceAccount:${sa.email}" }
}

output "service_account_names" {
  description = "Fully qualified service account name (projects/<project>/serviceAccounts/<email>) per workload, for bindings on the account itself such as google_service_account_iam_member."
  value       = { for k, sa in google_service_account.this : k => sa.name }
}

output "secret_ids" {
  description = "Secret ID (the short name, e.g. \"pb-reviewer-password\") per secret key. Cloud Run secret references and the scripts that add versions use this."
  value       = { for k, s in google_secret_manager_secret.this : k => s.secret_id }
}

output "secret_resource_ids" {
  description = "Full secret resource ID (projects/<project>/secrets/<secret_id>) per secret key."
  value       = { for k, s in google_secret_manager_secret.this : k => s.id }
}

output "secret_readers" {
  description = "Which workloads may read each secret (the accessor bindings this module creates). Informational, for READMEs and checks in other modules."
  value       = { for k, cfg in local.secrets : k => cfg.readers }
}

output "reviewer_password_version_created" {
  description = "True when this module wrote a reviewer-password version from tfvars (dev). False means the secret is empty until scripts/secrets/set-reviewer-password runs, and the proxy's login answers 503."
  value       = length(google_secret_manager_secret_version.reviewer_password) > 0
}
