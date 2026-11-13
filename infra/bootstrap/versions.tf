# Bootstrap is a ROOT module: it is the directory a human runs `terraform init/apply` in, so it pins
# the Terraform and provider versions and commits .terraform.lock.hcl (modules do not).
#
# There is deliberately no `backend` block. Bootstrap creates the GCS bucket that every other root
# keeps its state in, so on first run that bucket does not exist yet and bootstrap's own state has
# to start as a local file. README.md ("State: the chicken-and-egg") explains how to move it into
# the bucket afterwards with a git-ignored backend_override.tf.

terraform {
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.11"
    }
  }
}

# No project, region or credentials here on purpose.
# - Credentials: the human's own Application Default Credentials
#   (`gcloud auth application-default login`), never a service account key.
# - Project: every resource below sets `project` explicitly from var.project_id, so the provider's
#   default project (which it would otherwise pick up from CLOUDSDK_CORE_PROJECT and similar
#   environment variables) is never used by accident.
provider "google" {}
