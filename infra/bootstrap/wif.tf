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
    # Optional (T26): only jobs that reference one of these GitHub Environments. GitHub puts the
    # `environment` claim in the token only for such a job, and only after the environment's
    # protection rules (required reviewers, allowed branches) have passed. A token without the
    # claim makes the expression fail, and a condition that is not true rejects the token.
    length(var.github_environments) == 0 ? "" : "assertion.environment in [${join(", ", [for e in var.github_environments : "\"${e}\""])}]",
  ]))

  # T26: the CI CONTEXT of a token, one short word that says where the job runs. Each service
  # account below is bound to a list of contexts, so the planner, the deployer and the page
  # deployer can each be reached from different places (a principalSet matches one attribute
  # only, so the context folds the repository ID, the environment, the ref and the event into
  # that one attribute). The values:
  #
  #   other-repository      any token whose repository_id is not var.github_repository_id
  #   env-<name>-main       a job in GitHub Environment <name>, on the main branch, triggered by
  #                         push, schedule or workflow_dispatch
  #   env-<name>-other      a job in that environment anywhere else (bound to nothing)
  #   pull-request          a pull_request event without an environment
  #   main                  a job on the main branch triggered by push, schedule or
  #                         workflow_dispatch, without an environment
  #   other                 everything else: branch pushes, tags, pull_request_target, ...
  #
  # pull_request_target runs on the BASE branch (refs/heads/main) for events from forks, so the
  # event check matters: without it, such a workflow would read as "main".
  github_main_push = "(assertion.ref == \"refs/heads/${var.github_main_branch}\" && assertion.event_name in [\"push\", \"schedule\", \"workflow_dispatch\"])"
  github_ci_context = join("", [
    "assertion.repository_id != \"${var.github_repository_id}\" ? \"other-repository\" : ",
    "(\"environment\" in assertion) ? (\"env-\" + assertion.environment + (${local.github_main_push} ? \"-main\" : \"-other\")) : ",
    "assertion.event_name == \"pull_request\" ? \"pull-request\" : ",
    "${local.github_main_push} ? \"main\" : \"other\"",
  ])

  # Which contexts may impersonate which account.
  wif_bindings = merge(
    { for c in var.deployer_wif_contexts : "deployer/${c}" => { account = google_service_account.deployer.name, context = c } },
    var.create_planner ? { for c in var.planner_wif_contexts : "planner/${c}" => { account = google_service_account.planner[0].name, context = c } } : {},
    var.create_page_deployer ? { for c in var.page_deployer_wif_contexts : "page-deployer/${c}" => { account = google_service_account.page_deployer[0].name, context = c } } : {},
  )
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
  # attribute.ci_context (T26, see the locals) is what the IAM bindings in this file match on.
  attribute_mapping = merge({
    "google.subject"                = "assertion.sub"
    "attribute.repository"          = "assertion.repository"
    "attribute.repository_id"       = "assertion.repository_id"
    "attribute.repository_owner"    = "assertion.repository_owner"
    "attribute.repository_owner_id" = "assertion.repository_owner_id"
    "attribute.ref"                 = "assertion.ref"
    "attribute.ci_context"          = local.github_ci_context
    }, length(var.github_environments) == 0 ? {} : {
    # Google: "You must have an attribute mapping for all attribute condition fields." Mapped only
    # when the condition uses it, so tokens of jobs without an environment are unaffected.
    "attribute.environment" = "assertion.environment"
  })

  attribute_condition = local.github_attribute_condition

  oidc {
    issuer_uri = local.github_oidc_issuer
  }
}

# Who may impersonate which account: the identities in the pool whose ci_context is one of that
# account's contexts. roles/iam.workloadIdentityUser lets a federated identity act as the account.
#
# The context includes the repository's numeric ID (a foreign repository is "other-repository",
# which nothing is bound to), so a binding is never weaker than the pool's condition: the
# condition stops other repositories at the pool, the binding would stop them at the account even
# if the condition were loosened later. On top of that it says WHERE in this repository: the dev
# deployer only from main (default ["main"]), the prod deployer only from the prod Environment on
# main (["env-prod-main"]), the planner from pull requests (dev) or the prod-plan Environment.
resource "google_service_account_iam_member" "github_wif" {
  for_each = local.wif_bindings

  service_account_id = each.value.account
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.ci_context/${each.value.context}"
}
