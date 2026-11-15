# A reusable module pins the Terraform and provider versions it is written against, but holds no
# provider configuration and no backend: the calling root module (infra/envs/*) configures the
# `google` provider and the state backend, and passes the project and zone in as variables.
terraform {
  # Same floor as the other modules (1.11 brings `override_during = plan`, which the tests use).
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.11"
    }
  }
}
