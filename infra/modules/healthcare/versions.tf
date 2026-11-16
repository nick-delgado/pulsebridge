# No provider configuration and no backend here: a module inherits the provider from the root
# module that calls it (infra/envs/*), so the same module works against any project.
terraform {
  # 1.11 is the repo-wide floor (write-only arguments, cross-variable validation).
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.11"
    }
    # google_project_service_identity (the Healthcare service agent, T38) exists only in the beta
    # provider. The env roots configure the default `google-beta` and Terraform passes it down.
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 8.11"
    }
  }
}
