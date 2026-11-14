# Exactly the values later consumers need. None of them is a secret: they are identifiers.

output "project_id" {
  description = "ID of the created or adopted project. Env roots pass it as their project_id variable."
  value       = local.project_id
}

output "project_number" {
  description = "Numeric project number. Used in Workload Identity principal strings and some service agent emails."
  value       = local.project_number
}

output "state_bucket_name" {
  description = "Name of the GCS bucket for remote state. Goes in each env root's backend \"gcs\" block as `bucket` (with a per-env `prefix`)."
  value       = google_storage_bucket.tfstate.name
}

output "deployer_service_account_email" {
  description = "Email of the deployer service account. GitHub Actions passes it as `service_account` to google-github-actions/auth."
  value       = google_service_account.deployer.email
}

output "workload_identity_provider" {
  description = "Full resource name of the Workload Identity provider, projects/<number>/locations/global/workloadIdentityPools/<pool>/providers/<provider>. GitHub Actions passes it as `workload_identity_provider` to google-github-actions/auth."
  value       = google_iam_workload_identity_pool_provider.github.name
}
