# Remote state in the GCS bucket bootstrap created (one bucket per project; dev and prod are
# separate projects, D1). PARTIAL backend configuration: the bucket name is NOT here, because it
# names the deployer's project. Give it at init:
#
#   cp backend.hcl.example backend.hcl        # git-ignored (infra/envs/.gitignore); set bucket
#   terraform init -backend-config=backend.hcl
#
# The prefix is fixed here on purpose: it is not author-specific, and pinning it per root means a
# backend.hcl copied from the other environment can never make prod write into dev's state.
# The GCS backend locks the state natively (a lock object next to it), so two applies cannot run
# at once.
terraform {
  backend "gcs" {
    prefix = "prod"
  }
}
