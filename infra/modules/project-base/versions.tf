# A reusable module pins the Terraform and provider versions it is written against, but holds no
# provider configuration and no backend: the calling root module (infra/envs/*) configures the
# `google` provider and the state backend, and passes the project and region in as variables.
terraform {
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.11"
    }
  }
}
