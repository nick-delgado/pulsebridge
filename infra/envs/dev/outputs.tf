# THIS FILE IS IDENTICAL IN infra/envs/dev AND infra/envs/prod. Each output is the stack's; the
# descriptions are in infra/envs/_stack/outputs.tf. No secret values: secrets appear by name only.
# `terraform output` lists them; `terraform output -raw <name>` prints one for a script.

output "environment" {
  description = "dev or prod."
  value       = module.stack.environment
}

output "project_id" {
  description = "The project this environment deploys into."
  value       = module.stack.project_id
}

output "region" {
  description = "Region of the services, the job and the dataset (the --region of the runbook's gcloud commands)."
  value       = module.stack.region
}

output "dashboard_url" {
  description = "The address reviewers open (the Hosting origin)."
  value       = module.stack.dashboard_url
}

output "hosting_site_id" {
  description = "Firebase Hosting site ID."
  value       = module.stack.hosting_site_id
}

output "fhir_proxy_url" {
  description = "fhir-proxy's direct run.app URL, for the first-deploy hop measurement only (never for the page)."
  value       = module.stack.fhir_proxy_url
}

output "firebase_json" {
  description = "firebase.json for the page deploy: terraform output -raw firebase_json > web/firebase.json."
  value       = module.stack.firebase_json
}

output "healthcare" {
  description = "Cloud Healthcare coordinates (short IDs) and Pub/Sub names."
  value       = module.stack.healthcare
}

output "secret_name_prefix" {
  description = "Prefix of every Secret Manager secret ID; the connect script's PULSEBRIDGE_NAME_PREFIX."
  value       = module.stack.secret_name_prefix
}

output "secret_ids" {
  description = "Secret Manager secret IDs (names only) by key."
  value       = module.stack.secret_ids
}

output "setup_env" {
  description = "export lines for the connect script and FHIR checks (no secret values; sensitive because it carries the patient ID). terraform output -raw setup_env."
  value       = module.stack.setup_env
  sensitive   = true
}

output "ingest_enabled" {
  description = "Whether the Scheduler job exists and the ingest subscription pushes."
  value       = module.stack.ingest_enabled
}

output "push_enabled" {
  description = "Module healthcare's push switch (always equal to ingest_enabled)."
  value       = module.stack.push_enabled
}

output "scheduler_job_names" {
  description = "Scheduler jobs that exist (empty while ingest_enabled is false)."
  value       = module.stack.scheduler_job_names
}

output "seed_job_name" {
  description = "The seeder Cloud Run job (synthetic data only, else null)."
  value       = module.stack.seed_job_name
}

output "health_sync_service_name" {
  description = "The health-sync Cloud Run service (real data only, else null)."
  value       = module.stack.health_sync_service_name
}

output "v2_to_fhir_service_name" {
  description = "The v2-to-fhir Cloud Run service."
  value       = module.stack.v2_to_fhir_service_name
}

output "alert_policy_names" {
  description = "The ingestion alert policies by key."
  value       = module.stack.alert_policy_names
}

output "edge_vm" {
  description = "The edge VM: name, zone, internal IP, MLLP port, boot image, data disk, hook metadata keys."
  value       = module.stack.edge_vm
}

output "openemr" {
  description = "OpenEMR's internal addresses, registered scopes and image."
  value       = module.stack.openemr
}

output "build_service_account_email" {
  description = "The image-build service account (T26), or null."
  value       = module.stack.build_service_account_email
}

output "build_trigger_name" {
  description = "The Cloud Build trigger on release tags (T26), or null."
  value       = module.stack.build_trigger_name
}

output "image_variable_names" {
  description = "The tfvars names the image build prints."
  value       = module.stack.image_variable_names
}

output "next_steps" {
  description = "The ordered post-apply procedure for this deployment: terraform output -raw next_steps."
  value       = module.stack.next_steps
}
