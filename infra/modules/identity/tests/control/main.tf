# Positive control for tests/state_scan.py, used only by tests/state_scan/state_scan.tftest.hcl
# with a mock provider. It stores the dummy value the WRONG ways, where Terraform keeps it in
# state: verbatim through `secret_data`, and base64-encoded behind a one-character prefix in an
# annotation (so the encoded copy does not start on a 3-byte boundary). The scan must find both;
# if it cannot, it proves nothing about the real module.
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

  annotations = {
    encoded = base64encode("x${var.value}")
  }

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "control" {
  secret      = google_secret_manager_secret.control.id
  secret_data = var.value
}
