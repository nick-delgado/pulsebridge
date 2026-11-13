# The deployer service account: the identity env roots and CI run as when they create the stack.
#
# No google_service_account_key is created, here or anywhere: GitHub Actions gets short-lived
# tokens for this account through Workload Identity Federation (wif.tf), and humans use their
# own login. A stored key would be a long-lived credential that works from anywhere until it is
# rotated.

resource "google_service_account" "deployer" {
  project      = local.project_id
  account_id   = var.deployer_account_id
  display_name = "Terraform deployer"
  description  = "Creates the stack from the env roots; impersonated by GitHub Actions via Workload Identity Federation. Created by infra/bootstrap."

  depends_on = [google_project_service.bootstrap]
}

# One additive binding per role. google_project_iam_member only adds this member to the role;
# it never removes other members, so adopting an existing project cannot wipe its existing IAM
# (google_project_iam_policy or _binding would be authoritative and could).
resource "google_project_iam_member" "deployer" {
  for_each = toset(var.deployer_roles)

  project = local.project_id
  role    = each.value
  member  = google_service_account.deployer.member
}
