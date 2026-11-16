terraform {
  # 1.11 is the first release with write-only arguments (`secret_data_wo`), which the optional
  # reviewer-password version relies on to keep the value out of state.
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
