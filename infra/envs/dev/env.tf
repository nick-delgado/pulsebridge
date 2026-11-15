# THE settings that make this root "dev". Together with backend.tf (the state prefix) this is the
# ONLY file that differs between infra/envs/dev and infra/envs/prod; a test diffs the two roots
# and fails on any other difference. Each setting is explained in infra/envs/README.md
# ("dev and prod").

locals {
  environment = "dev"

  # dev is destroyed nightly (T26) and holds synthetic data: everything may be destroyed.
  protect_from_destroy = false

  # dev may set the reviewer password from tfvars (module identity's write-only version).
  allow_reviewer_password_from_tfvars = true
}

variable "use_synthetic_data" {
  description = "true (dev default): the synthetic-data seeder fills the FHIR store, so a stranger can run the whole stack without a Pixel Watch or a Google Health OAuth app; patient_id must start with SYN-. false: real Google Health data through health-sync and the connect script. Switching it on an existing deployment destroys one set of workloads and needs a different patient: do it on a fresh FHIR store (module ingestion README, \"The two switches\")."
  type        = bool
  default     = true
  nullable    = false
}
