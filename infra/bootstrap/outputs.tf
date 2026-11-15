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
  description = "Name of the GCS bucket for remote state. Each env root takes it in its git-ignored backend.hcl (copied from backend.hcl.example: bucket = \"<this>\"), given to `terraform init -backend-config=backend.hcl`; the prefix is fixed in the root's backend.tf. CI passes it as TF_STATE_BUCKET (infra/envs/README.md, \"CI/CD\")."
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

output "firebase_added" {
  description = "Whether bootstrap added Firebase to the project (add_firebase). module phr's Hosting site needs Firebase; with false, add it another way or leave the dashboard unhosted."
  value       = length(google_firebase_project.this) > 0
}

output "planner_service_account_email" {
  description = "Email of the read-only planner (create_planner), or null. GitHub repository variable <ENV>_GCP_PLANNER_SA (infra/envs/README.md, \"CI/CD\")."
  value       = one(google_service_account.planner[*].email)
}

output "page_deployer_service_account_email" {
  description = "Email of the page deployer (create_page_deployer), or null. GitHub repository variable <ENV>_GCP_PAGE_DEPLOYER_SA."
  value       = one(google_service_account.page_deployer[*].email)
}
