# Synthetic values for computed attributes, so mocked plans can assert on them. The numbers and
# names are made up; they are not any real project, account or pool.
#
# The two project numbers differ on purpose: tests can tell whether an output came from the
# created project (resource) or the adopted one (data source).

mock_resource "google_project" {
  defaults = {
    number = "111111111111"
  }
}

mock_data "google_project" {
  defaults = {
    number = "222222222222"
  }
}

mock_resource "google_service_account" {
  defaults = {
    email  = "terraform-deployer@example-project.iam.gserviceaccount.com"
    member = "serviceAccount:terraform-deployer@example-project.iam.gserviceaccount.com"
    name   = "projects/example-project/serviceAccounts/terraform-deployer@example-project.iam.gserviceaccount.com"
  }
}

mock_resource "google_iam_workload_identity_pool" {
  defaults = {
    name = "projects/111111111111/locations/global/workloadIdentityPools/github-actions"
  }
}

mock_resource "google_iam_workload_identity_pool_provider" {
  defaults = {
    name = "projects/111111111111/locations/global/workloadIdentityPools/github-actions/providers/github-oidc"
  }
}
