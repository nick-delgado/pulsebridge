# Positive control for tests/state_scan.py, used only by tests/state_scan.tftest.hcl with a mock
# provider. It stores the dummy value the WRONG way, through `secret_data`, which Terraform keeps
# in state. The scan must find the value here; if it cannot, the scan proves nothing about the
# real module.
terraform {
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.11"
    }
  }
}

variable "value" {
  description = "Synthetic dummy value to store in state on purpose."
  type        = string
  sensitive   = true
}

resource "google_secret_manager_secret" "control" {
  project   = "example-project-01"
  secret_id = "control"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "control" {
  secret      = google_secret_manager_secret.control.id
  secret_data = var.value
}
