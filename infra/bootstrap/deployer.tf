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
# T26: roles/cloudbuild.builds.editor only when the build trigger is opted in (allow_build_trigger):
# creating a Cloud Build trigger needs cloudbuild.builds.create, which also lets the holder run
# builds, so it is not granted by default.
locals {
  deployer_roles = toset(concat(var.deployer_roles, var.allow_build_trigger ? ["roles/cloudbuild.builds.editor"] : []))
}

resource "google_project_iam_member" "deployer" {
  for_each = local.deployer_roles

  project = local.project_id
  role    = each.value
  member  = google_service_account.deployer.member
}

# T44 (D16): the stack now creates one bucket, module phr's demo counter bucket. No predefined role
# fits: roles/storage.admin also reads and writes every OBJECT in every bucket of the project, and
# the deployer has no reason to read or write a counter (only fhir-proxy writes them; module phr
# grants that on the bucket). So a CUSTOM role with exactly what Terraform does to that bucket:
#   create, read, update (lifecycle, labels, soft delete) and delete the bucket;
#   read and set its IAM policy (google_storage_bucket_iam_member, additive);
#   list and delete objects, which `force_destroy = true` does to empty the bucket before deleting
#   it on a dev destroy.
# Not storage.objects.get or .create: the deployer can neither read nor forge a counter. The role
# is project-level because storage.buckets.create has no narrower resource (the bucket does not
# exist yet). Creating a custom role needs iam.roles.create: bootstrap runs as the owner.
locals {
  deployer_bucket_permissions = sort([
    "storage.buckets.create",
    "storage.buckets.delete",
    "storage.buckets.get",
    "storage.buckets.update",
    "storage.buckets.getIamPolicy",
    "storage.buckets.setIamPolicy",
    "storage.objects.list",
    "storage.objects.delete",
  ])
}

resource "google_project_iam_custom_role" "deployer_buckets" {
  project     = local.project_id
  role_id     = var.deployer_bucket_role_id
  title       = "PulseBridge deployer: buckets"
  description = "Create, update and delete the stack's buckets (module phr's demo counter bucket) and their IAM; list and delete objects only to empty a bucket on destroy. No object reads or writes. Created by infra/bootstrap (T44)."
  permissions = local.deployer_bucket_permissions
  stage       = "GA"

  depends_on = [google_project_service.bootstrap]
}

resource "google_project_iam_member" "deployer_buckets" {
  project = local.project_id
  role    = google_project_iam_custom_role.deployer_buckets.name
  member  = google_service_account.deployer.member
}
