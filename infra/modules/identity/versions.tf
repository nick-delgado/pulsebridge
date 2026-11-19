terraform {
  # The same floor as every other module and root (1.11, the first with write-only arguments).
  # This module no longer writes any secret version (T44 removed dev's reviewer password).
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.11"
    }
    # google_project_service_identity (the Pub/Sub service agent) exists only in the beta
    # provider. The env roots configure the default `google-beta` and Terraform passes it down.
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 8.11"
    }
  }
}
