# An environment ROOT: the directory a person or CI runs `terraform init/plan/apply` in. THIS FILE
# IS IDENTICAL IN infra/envs/dev AND infra/envs/prod (a test checks it).
#
# A root pins the Terraform and provider versions and commits .terraform.lock.hcl, which records
# the exact provider builds (with their checksums) the versions resolved to, so every run uses
# the same provider bytes. The constraints match the modules' (~> 8.11).
terraform {
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.11"
    }
    # Firebase Hosting (module phr) exists only in the beta provider at 8.11.
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 8.11"
    }
  }
}
