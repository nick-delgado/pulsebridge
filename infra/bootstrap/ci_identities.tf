# Two more CI identities (T26), both opt-in, so that the deployer's full rights are used only
# where something is applied:
#
#   planner        read-only. `terraform plan` on pull requests (dev) and in the prod-plan
#                  Environment (prod) runs as it, so a pull request's code, or prod's code before
#                  approval, never holds a token that can change anything. It reads the state and
#                  the CI tfvars from the state bucket, and plans with -lock=false (it cannot take
#                  the state lock, which would need object write).
#   page deployer  roles/firebasehosting.admin only. The page deploy job runs the Firebase CLI and
#                  its npm dependencies; with this account a compromised dependency can at worst
#                  change the Hosting site, not the infrastructure.
#
# Like the deployer, neither gets a key; GitHub Actions impersonates them through Workload
# Identity Federation (wif.tf binds each to its own contexts).

resource "google_service_account" "planner" {
  count = var.create_planner ? 1 : 0

  project      = local.project_id
  account_id   = var.planner_account_id
  display_name = "Terraform planner (read-only)"
  description  = "Runs terraform plan for CI: reads resources, IAM policies, secret metadata (never secret values) and the state. Created by infra/bootstrap (T26)."

  depends_on = [google_project_service.bootstrap]
}

resource "google_project_iam_member" "planner" {
  for_each = var.create_planner ? toset(var.planner_roles) : toset([])

  project = local.project_id
  role    = each.value
  member  = google_service_account.planner[0].member
}

# Read the state (and the CI tfvars object next to it); no write, so no lock.
resource "google_storage_bucket_iam_member" "planner_state" {
  count = var.create_planner ? 1 : 0

  bucket = google_storage_bucket.tfstate.name
  role   = "roles/storage.objectViewer"
  member = google_service_account.planner[0].member
}

resource "google_service_account" "page_deployer" {
  count = var.create_page_deployer ? 1 : 0

  project      = local.project_id
  account_id   = var.page_deployer_account_id
  display_name = "Dashboard page deployer"
  description  = "Deploys web/dist to Firebase Hosting from CI. roles/firebasehosting.admin only. Created by infra/bootstrap (T26)."

  depends_on = [google_project_service.bootstrap]
}

# The one role. Not a variable on purpose: anything broader would defeat the point of the account.
resource "google_project_iam_member" "page_deployer" {
  count = var.create_page_deployer ? 1 : 0

  project = local.project_id
  role    = "roles/firebasehosting.admin"
  member  = google_service_account.page_deployer[0].member
}
