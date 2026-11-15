# Workload Identity Federation (WIF) for GitHub Actions.
#
# A GitHub Actions job can ask GitHub for a signed OIDC token (a JWT) describing the run:
# which repository, branch, workflow and so on. Google's Security Token Service (STS) checks that
# token's signature against GitHub's published keys (the issuer below), applies the attribute
# condition, and returns a short-lived federated token. The job then exchanges that for an
# access token of the deployer service account (IAM Credentials API), which works for about an
# hour. Nothing long-lived is stored in GitHub.

locals {
  github_oidc_issuer = "https://token.actions.githubusercontent.com"

  # The attribute condition is a CEL expression evaluated on every token exchange. Any token
  # for which it is false is rejected before any Google token is issued. Without it, ANY
  # GitHub repository in the world could get a federated token from this pool, because they
  # all share the same issuer.
  github_attribute_condition = join(" && ", compact([
    # The name: readable, and what a human checks in the console.
    "assertion.repository == \"${var.github_repository}\"",
    # The numeric ID: required. A name can be taken over (the repository is deleted or renamed,
    # or its owner account is renamed, and someone creates a new one with the old name); the ID
    # belongs to exactly one repository forever. The name alone is therefore not enough.
    "assertion.repository_id == \"${var.github_repository_id}\"",
    # Optional: the owner's numeric ID, for the same reason at the account level.
    var.github_repository_owner_id == null ? "" : "assertion.repository_owner_id == \"${var.github_repository_owner_id}\"",
    # Optional: only these git refs. Values are validated to contain no quotes or backslashes,
    # so they cannot break out of the CEL string literal (no expression injection).
    length(var.github_allowed_refs) == 0 ? "" : "assertion.ref in [${join(", ", [for r in var.github_allowed_refs : "\"${r}\""])}]",
  ]))
}

resource "google_iam_workload_identity_pool" "github" {
  project                   = local.project_id
  workload_identity_pool_id = var.wif_pool_id
  display_name              = "GitHub Actions"
  description               = "Federates GitHub Actions OIDC tokens from ${var.github_repository}. Created by infra/bootstrap."

  depends_on = [google_project_service.bootstrap]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = local.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = var.wif_provider_id
  display_name                       = "GitHub OIDC"
  description                        = "Trusts GitHub's OIDC issuer, restricted to one repository."

  # attribute_mapping copies claims from GitHub's token ("assertion.*") into Google attributes.
  # google.subject is required and identifies the caller in audit logs, for example
  # "repo:owner/name:ref:refs/heads/main". Google limits google.subject to 127 bytes; a long
  # owner, repository or branch name can exceed that and the exchange then fails (README.md).
  # attribute.repository_id is what the IAM binding in this file matches on.
  attribute_mapping = {
    "google.subject"                = "assertion.sub"
    "attribute.repository"          = "assertion.repository"
    "attribute.repository_id"       = "assertion.repository_id"
    "attribute.repository_owner"    = "assertion.repository_owner"
    "attribute.repository_owner_id" = "assertion.repository_owner_id"
    "attribute.ref"                 = "assertion.ref"
  }

  attribute_condition = local.github_attribute_condition

  oidc {
    issuer_uri = local.github_oidc_issuer
  }
}

# Who may impersonate the deployer: every identity in the pool whose repository_id attribute is
# exactly var.github_repository_id. roles/iam.workloadIdentityUser lets a federated identity act
# as this service account.
#
# The binding keys on the numeric ID, not the name, so it is never weaker than the condition
# above: a principalSet can match one attribute only, and the ID is the one that cannot be
# reused by a different repository. This is a second, independent check: the condition stops
# other repositories at the pool; this binding would stop them at the service account even if
# the condition were loosened later.
resource "google_service_account_iam_member" "deployer_github_wif" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository_id/${var.github_repository_id}"
}
