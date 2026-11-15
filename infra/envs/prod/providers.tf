# Provider configuration. THIS FILE IS IDENTICAL IN infra/envs/dev AND infra/envs/prod.
#
# - project/region/zone are set explicitly from the variables, so the provider never falls back to
#   CLOUDSDK_CORE_PROJECT or a gcloud default that points at another project.
# - Credentials are never configured here (no key file): CI gets deployer credentials through
#   Workload Identity Federation; a person uses their own Application Default Credentials and,
#   with deployer_service_account_email set, impersonates the deployer.
# - These are the DEFAULT (un-aliased) configurations, which Terraform passes down to
#   module.stack and on to the seven modules automatically.

provider "google" {
  project                     = var.project_id
  region                      = var.region
  zone                        = var.zone
  impersonate_service_account = var.deployer_service_account_email
}

# The Firebase Management and Hosting APIs refuse calls whose credentials carry no quota project,
# so the beta provider bills its calls to this project (the provider's google_firebase_* docs ask
# for user_project_override; module phr README, "Usage"; the same as bootstrap's firebase.tf).
provider "google-beta" {
  project                     = var.project_id
  region                      = var.region
  zone                        = var.zone
  impersonate_service_account = var.deployer_service_account_email
  user_project_override       = true
  billing_project             = var.project_id
}
