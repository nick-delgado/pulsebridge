# The PulseBridge stack: the one composition both environment roots call (infra/envs/dev and
# infra/envs/prod). It is a child module, not a root, so it holds no provider configuration and
# no backend: the root that calls it configures `google` and `google-beta` and the state backend,
# and Terraform passes those default (un-aliased) provider configurations down to this module and
# on to the seven modules it wires. No aliases are needed: every module uses the default `google`
# configuration, and only module phr also uses the default `google-beta` one.
#
# Lock files belong to root modules only: infra/envs/.gitignore keeps this directory's
# .terraform.lock.hcl (created by `terraform init` in tests) out of git.
terraform {
  # Same floor as the modules: 1.11 brings write-only arguments (the dev reviewer password).
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.11"
    }
    # Module phr's Firebase Hosting site exists only in the beta provider at 8.11.
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 8.11"
    }
  }
}
