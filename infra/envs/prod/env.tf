# THE settings that make this root "prod". Together with backend.tf (the state prefix) this is the
# ONLY file that differs between infra/envs/dev and infra/envs/prod; a test diffs the two roots
# and fails on any other difference. Each setting is explained in infra/envs/README.md
# ("dev and prod").

locals {
  environment = "prod"

  # prod holds real data (the deployer's own heart rate) and is the public demo: the Healthcare
  # dataset and stores PREVENT deletion, the secrets and the Cloud Run services and job are
  # deletion-protected, so a stray `terraform destroy` fails instead of deleting them.
  protect_from_destroy = true
}

variable "use_synthetic_data" {
  description = "false (prod default): real Google Health data through health-sync and the connect script; patient_id must NOT start with SYN-. true: the synthetic-data seeder instead. Switching it on an existing deployment destroys one set of workloads and needs a different patient: do it on a fresh FHIR store (module ingestion README, \"The two switches\"); with protect_from_destroy the destroy of the Cloud Run workloads is refused until deletion protection is lifted."
  type        = bool
  default     = false
  nullable    = false
}
