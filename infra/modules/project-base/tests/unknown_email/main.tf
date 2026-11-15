# Test harness for tests/project_base.tftest.hcl, run "mirror_grant_count_is_known_with_an_unknown_email".
# It feeds the module the email of a service account created in the same plan, so the email is
# UNKNOWN at plan time, exactly as on an env root's first plan (module identity creates the
# account). The grant's count must still be known. No provider configuration: the test mocks it.
terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.11"
    }
  }
}

resource "google_service_account" "edge_vm" {
  project    = "example-project-123"
  account_id = "pb-edge-vm"
}

module "project_base" {
  source = "../.."

  project_id                    = "example-project-123"
  edge_vm_service_account_email = google_service_account.edge_vm.email
}
