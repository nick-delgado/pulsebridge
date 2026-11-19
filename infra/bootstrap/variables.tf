# Inputs for the one-time bootstrap root. Nothing author-specific has a default: the project ID,
# billing account and GitHub repository always come from the person running bootstrap
# (terraform.tfvars, git-ignored; see terraform.tfvars.example).

# ---------------------------------------------------------------------------------------------
# Project: create a new one, or adopt an existing one
# ---------------------------------------------------------------------------------------------

variable "create_project" {
  description = "true: create a new GCP project with var.project_id and link var.billing_account to it. false: adopt an existing project with var.project_id and create nothing at project level. No default, so the choice is always explicit."
  type        = bool
  nullable    = false
}

variable "project_id" {
  description = "ID of the GCP project to create (create_project = true) or adopt (create_project = false). Project IDs are globally unique, 6 to 30 characters, lowercase letters, digits and hyphens, starting with a letter."
  type        = string
  nullable    = false

  validation {
    # The GCP project ID rule: 6-30 chars, starts with a letter, no trailing hyphen.
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be 6-30 characters of lowercase letters, digits and hyphens, start with a letter and not end with a hyphen."
  }
}

variable "project_name" {
  description = "Human-readable display name for a newly created project. Only used when create_project = true. Defaults to project_id."
  type        = string
  default     = null

  validation {
    # Display-name rule: 4-30 chars of letters, digits, spaces, hyphens, single and double quotes and exclamation marks.
    condition     = var.project_name == null || can(regex("^[A-Za-z0-9 '\"!-]{4,30}$", coalesce(var.project_name, "xxxx")))
    error_message = "project_name must be 4-30 characters of letters, digits, spaces, hyphens, quotes or exclamation marks."
  }

  validation {
    condition     = var.project_name == null || var.create_project
    error_message = "project_name is only used when create_project = true; leave it unset when adopting an existing project."
  }
}

variable "billing_account" {
  description = "Billing account ID (format XXXXXX-XXXXXX-XXXXXX) to link to a newly created project. Required when create_project = true; must be unset when adopting, because bootstrap does not change an existing project's billing."
  type        = string
  default     = null

  validation {
    condition     = var.billing_account == null || can(regex("^[A-F0-9]{6}-[A-F0-9]{6}-[A-F0-9]{6}$", coalesce(var.billing_account, "x")))
    error_message = "billing_account must look like 0123AB-4567CD-89EF01 (three groups of six uppercase hex characters)."
  }

  validation {
    # Cross-variable validation (Terraform >= 1.9): a new project without billing cannot enable
    # most APIs, so fail at plan time rather than half-way through an apply.
    condition     = var.create_project == (var.billing_account != null)
    error_message = "billing_account is required when create_project = true and must be unset when create_project = false."
  }
}

variable "org_id" {
  description = "Optional numeric organization ID to create the new project under. Leave unset (the default) for a personal Google account with no organization. Mutually exclusive with folder_id; only used when create_project = true."
  type        = string
  default     = null

  validation {
    condition     = var.org_id == null || can(regex("^[0-9]+$", coalesce(var.org_id, "x")))
    error_message = "org_id must be the numeric organization ID, for example 123456789012."
  }

  validation {
    condition     = var.org_id == null || var.folder_id == null
    error_message = "Set at most one of org_id and folder_id: a project has exactly one parent."
  }

  validation {
    condition     = var.org_id == null || var.create_project
    error_message = "org_id is only used when create_project = true; bootstrap never moves an adopted project."
  }
}

variable "folder_id" {
  description = "Optional numeric folder ID to create the new project under. Leave unset for a personal account. Mutually exclusive with org_id; only used when create_project = true."
  type        = string
  default     = null

  validation {
    condition     = var.folder_id == null || can(regex("^[0-9]+$", coalesce(var.folder_id, "x")))
    error_message = "folder_id must be the numeric folder ID, for example 123456789012."
  }

  validation {
    condition     = var.folder_id == null || var.create_project
    error_message = "folder_id is only used when create_project = true; bootstrap never moves an adopted project."
  }
}

variable "auto_create_network" {
  description = "Only for a newly created project: keep the 'default' VPC network GCP creates automatically. Default false, because that network ships with firewall rules open to the internet (SSH, RDP, ICMP) and the stack builds its own VPC (module project-base). With false, the provider enables the Compute Engine API to delete the default network."
  type        = bool
  default     = false
  nullable    = false
}

variable "add_firebase" {
  description = "Add Firebase to the project (google_firebase_project), with the Firebase Management API (firebase.googleapis.com) enabled first. Firebase Hosting, which serves the dashboard (D4, module phr), needs it. Default true. Bootstrap runs once with the owner's credentials, and adding Firebase needs firebase.projects.update (owner or editor), so doing it here means the deployer never needs roles/firebase.admin. It CANNOT BE UNDONE: \"Once Firebase has been added to a Google Project it cannot be removed\" (provider docs). It costs nothing. false: no Firebase; then Hosting is unavailable and module phr's site cannot be created, unless Firebase is added another way (README, \"Firebase\")."
  type        = bool
  default     = true
  nullable    = false
}

variable "labels" {
  description = "Labels added to the project (when created) and the state bucket, for example { app = \"pulsebridge\" }. Keys and values: lowercase letters, digits, hyphens and underscores; keys start with a letter."
  type        = map(string)
  default     = {}
  nullable    = false

  validation {
    condition = alltrue([
      for k, v in var.labels :
      can(regex("^[a-z][a-z0-9_-]{0,62}$", k)) && can(regex("^[a-z0-9_-]{0,63}$", v))
    ])
    error_message = "Label keys must start with a lowercase letter; keys and values may contain only lowercase letters, digits, hyphens and underscores (max 63 characters)."
  }
}

# ---------------------------------------------------------------------------------------------
# Remote state bucket
# ---------------------------------------------------------------------------------------------

variable "state_bucket_name" {
  description = "Name of the GCS bucket that holds Terraform state for every root (env roots use one prefix each). Bucket names are global. Defaults to \"<project_id>-tfstate\"."
  type        = string
  default     = null

  validation {
    condition     = var.state_bucket_name == null || can(regex("^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$", coalesce(var.state_bucket_name, "x")))
    error_message = "state_bucket_name must be 3-63 characters of lowercase letters, digits, hyphens, underscores and dots, starting and ending with a letter or digit."
  }

  validation {
    condition     = var.state_bucket_name == null || !startswith(coalesce(var.state_bucket_name, "x"), "goog")
    error_message = "Bucket names may not start with \"goog\"."
  }
}

variable "state_bucket_location" {
  description = "Location of the state bucket. Default us-central1: a single region inside the Cloud Storage always-free tier (us-central1, us-east1, us-west1), which is plenty for a few kilobytes of state."
  type        = string
  default     = "us-central1"
  nullable    = false

  validation {
    condition     = can(regex("^[a-z]+(-[a-z0-9]+)*$", lower(var.state_bucket_location))) && length(var.state_bucket_location) >= 2
    error_message = "state_bucket_location must be a GCS location such as us-central1, europe-west2 or EU."
  }
}

variable "state_noncurrent_versions_to_keep" {
  description = "How many older (noncurrent) versions of each state file the bucket keeps for recovery. Older ones are deleted by a lifecycle rule so the bucket does not grow forever."
  type        = number
  default     = 10
  nullable    = false

  validation {
    condition     = var.state_noncurrent_versions_to_keep >= 1 && floor(var.state_noncurrent_versions_to_keep) == var.state_noncurrent_versions_to_keep
    error_message = "state_noncurrent_versions_to_keep must be a whole number of at least 1; keeping no old versions defeats the point of versioning."
  }
}

# ---------------------------------------------------------------------------------------------
# Deployer service account
# ---------------------------------------------------------------------------------------------

variable "deployer_account_id" {
  description = "account_id (the part before @) of the deployer service account that env roots and CI run as. 6-30 characters."
  type        = string
  default     = "terraform-deployer"
  nullable    = false

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.deployer_account_id))
    error_message = "deployer_account_id must be 6-30 characters of lowercase letters, digits and hyphens, start with a letter and not end with a hyphen."
  }
}

variable "deployer_roles" {
  description = "Project-level IAM roles granted to the deployer service account. The default is the smallest set of predefined roles that can create the whole PulseBridge stack; see README.md (\"Deployer roles\") for the reasoning. Basic roles (owner, editor) are rejected."
  type        = list(string)
  nullable    = false
  default = [
    # Enable the stack's APIs (module project-base).
    "roles/serviceusage.serviceUsageAdmin",
    # VPC, subnet, Cloud NAT, static IP (networkAdmin); firewall rules (securityAdmin);
    # the e2-micro and its disk (instanceAdmin.v1).
    "roles/compute.networkAdmin",
    "roles/compute.securityAdmin",
    "roles/compute.instanceAdmin.v1",
    # Container images (module project-base).
    "roles/artifactregistry.admin",
    # One service account per service, and attaching them to Cloud Run, the VM and Scheduler.
    "roles/iam.serviceAccountAdmin",
    "roles/iam.serviceAccountUser",
    # Project-level IAM bindings for those service accounts (module identity).
    # This is the most powerful role in the list; README.md explains the trade-off.
    "roles/resourcemanager.projectIamAdmin",
    # Secrets without values, and their accessor bindings (module identity).
    "roles/secretmanager.admin",
    # Healthcare dataset, FHIR R4 store, HL7v2 store and their IAM (module healthcare).
    "roles/healthcare.datasetAdmin",
    "roles/healthcare.fhirStoreAdmin",
    "roles/healthcare.hl7V2StoreAdmin",
    # Topic, push subscription, dead-letter topic (module healthcare).
    "roles/pubsub.admin",
    # Cloud Run services and their invoker bindings (modules ingestion, phr).
    "roles/run.admin",
    # Scheduler job that triggers health-sync (module ingestion).
    "roles/cloudscheduler.admin",
    # Log-based metrics for the ingestion alerts (module ingestion; every apply creates them).
    "roles/logging.configWriter",
    # Alert policies on those metrics, and the optional email notification channel (module
    # ingestion, only when an alert address or channel is given).
    "roles/monitoring.alertPolicyEditor",
    "roles/monitoring.notificationChannelEditor",
    # Firebase Hosting: module phr's site and the page deploys (D4; T26's deploy step). Adding
    # Firebase to the project needs more (firebase.projects.update): bootstrap does that once
    # with the owner's credentials (add_firebase), so the deployer never needs roles/firebase.admin.
    "roles/firebasehosting.admin",
    # The public demo's reCAPTCHA (Fraud Defense) score key (module phr, D16, T44): GetKey,
    # CreateKey, UpdateKey and DeleteKey need recaptchaenterprise.keys.get / .create / .update /
    # .delete on the project ("Access control with IAM", API permissions table,
    # docs.cloud.google.com/recaptcha/docs/access-control, read 19 November 2026). Editor holds those
    # (plus assessments, firewall policies and metrics); Admin is recaptchaenterprise.*, broader, and
    # not needed. The stack's one bucket is not covered by a predefined role here: see deployer.tf.
    "roles/recaptchaenterprise.editor",
  ]

  validation {
    condition     = length(var.deployer_roles) > 0
    error_message = "deployer_roles must not be empty: a deployer with no roles cannot deploy anything."
  }

  validation {
    # Role names look like roles/<service>.<role> (predefined), roles/<name> (basic: rejected by
    # the next validation, with its own message) or projects/<p>/roles/<r> (project custom role).
    condition = alltrue([
      for r in var.deployer_roles :
      can(regex("^(roles/[a-zA-Z0-9]+(\\.[a-zA-Z0-9.]+)?|projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/roles/[a-zA-Z0-9_.]+)$", r))
    ])
    error_message = "Each deployer role must be a role name such as roles/<service>.<role> or a project custom role (projects/<project>/roles/<role>)."
  }

  validation {
    # Basic roles are far broader than any deployment needs. roles/owner in particular could
    # grant itself anything, delete the project, and remove every human's access.
    condition     = length(setintersection(toset(var.deployer_roles), toset(["roles/owner", "roles/editor", "roles/viewer"]))) == 0
    error_message = "Basic roles (roles/owner, roles/editor, roles/viewer) are not allowed for the deployer; use predefined roles."
  }
}

# ---------------------------------------------------------------------------------------------
# Workload Identity Federation for GitHub Actions
# ---------------------------------------------------------------------------------------------

variable "github_repository" {
  description = "The ONE GitHub repository, as \"owner/name\", whose Actions workflows may impersonate the deployer service account. Matched exactly (case-sensitive) against the `repository` claim of GitHub's OIDC token."
  type        = string
  nullable    = false

  validation {
    # GitHub owner: 1-39 alphanumerics or single hyphens; repository: letters, digits, '.', '_', '-'.
    condition     = can(regex("^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._-]{1,100}$", var.github_repository))
    error_message = "github_repository must be \"owner/name\", for example \"octo-org/pulsebridge\"."
  }
}

variable "github_repository_id" {
  description = "Numeric ID of the same GitHub repository: the `id` field of https://api.github.com/repos/OWNER/NAME, or `gh api repos/OWNER/NAME --jq .id`. Required. Names can be reused after a repository is deleted or renamed, or its owner account is renamed; the numeric ID never is. Both the attribute condition and the impersonation binding match on it."
  type        = string
  # No default: required. Left nullable on purpose so an explicit null reaches the validation
  # below and is rejected with a clear message (tests/validation.tftest.hcl checks this).

  validation {
    # can() is false for null too, so null is rejected here.
    condition     = can(regex("^[1-9][0-9]*$", var.github_repository_id))
    error_message = "github_repository_id must be the numeric repository ID, for example \"123456789\" (the `id` field of the GitHub API's repository object)."
  }
}

variable "github_repository_owner_id" {
  description = "Optional numeric ID of the repository's owner (user or organization): the `owner.id` field of https://api.github.com/repos/OWNER/NAME, or `gh api repos/OWNER/NAME --jq .owner.id`. When set, the attribute condition also requires `assertion.repository_owner_id` to match."
  type        = string
  default     = null

  validation {
    condition     = var.github_repository_owner_id == null || can(regex("^[1-9][0-9]*$", coalesce(var.github_repository_owner_id, "x")))
    error_message = "github_repository_owner_id must be the numeric owner ID, for example \"12345678\"."
  }
}

variable "github_allowed_refs" {
  description = "Optional list of git refs (for example [\"refs/heads/main\"]) allowed to obtain deployer tokens. Empty (the default) allows every branch, tag and pull-request ref in the repository; see README.md (\"What the condition does not restrict\"). Note that pull-request workflows run on refs/pull/N/merge, so restricting refs also blocks plan-on-PR with this identity."
  type        = list(string)
  default     = []
  nullable    = false

  validation {
    # Each value is pasted into a CEL string literal in the attribute condition, so the allowed
    # characters exclude quotes and backslashes: a value cannot close the literal and inject CEL.
    condition     = alltrue([for r in var.github_allowed_refs : can(regex("^refs/(heads|tags)/[A-Za-z0-9._/-]{1,200}$", r))])
    error_message = "Each entry of github_allowed_refs must look like refs/heads/<branch> or refs/tags/<tag>, using only letters, digits, '.', '_', '/' and '-'."
  }
}

variable "github_environments" {
  description = "Optional list of GitHub Environment names (for example [\"prod-plan\", \"prod\"]) whose jobs alone may obtain deployer tokens. Empty (the default) adds nothing. When set, the attribute condition also requires `assertion.environment in [...]`: only a job that references one of these environments gets a token, and GitHub applies the environment's protection rules (required reviewers, allowed branches) BEFORE such a job starts, so the gate is enforced by GitHub rather than by workflow YAML. Use it for the prod project (infra/envs/README.md, \"CI/CD\"). Pull-request plans reference no environment, so a project with this set cannot be planned from pull requests."
  type        = list(string)
  default     = []
  nullable    = false

  validation {
    # Pasted into a CEL string literal, like github_allowed_refs: no quotes or backslashes.
    condition     = alltrue([for e in var.github_environments : can(regex("^[A-Za-z0-9._-]{1,255}$", e))])
    error_message = "Each entry of github_environments must be an environment name of letters, digits, '.', '_' and '-'."
  }
}

# CI contexts (wif.tf): "main", "pull-request" or "env-<environment>-main". The three accounts'
# lists must never share a context, or a job could become two of them (a page deploy job would
# hold the deployer). With github_environments set (prod), every context must be one of those
# environments: a non-environment context would be refused by the pool's condition anyway, and
# a default meant for dev must not pass silently.

variable "deployer_wif_contexts" {
  description = "The CI contexts (wif.tf) whose jobs may impersonate the deployer. Default [\"main\"] is dev's: jobs on main run by push, schedule or workflow_dispatch (the dev apply and the nightly destroy). prod: [\"env-prod-main\"] (only jobs in the prod GitHub Environment on main); with github_environments set, \"main\" is refused. Never \"pull-request\"."
  type        = list(string)
  default     = ["main"]
  nullable    = false

  validation {
    condition     = length(var.deployer_wif_contexts) > 0 && alltrue([for c in var.deployer_wif_contexts : can(regex("^(main|env-[A-Za-z0-9._-]{1,255}-main)$", c))])
    error_message = "deployer_wif_contexts must be a non-empty list of \"main\" or \"env-<environment>-main\" (never \"pull-request\")."
  }

  validation {
    condition     = length(var.github_environments) == 0 || alltrue([for c in var.deployer_wif_contexts : contains([for e in var.github_environments : "env-${e}-main"], c)])
    error_message = "With github_environments set (prod), deployer_wif_contexts must name those environments, for example [\"env-prod-main\"]; the dev default [\"main\"] does not apply."
  }
}

variable "create_planner" {
  description = "Create the read-only planner service account (ci_identities.tf, T26) that CI's terraform plan runs as. Default false. Opt in for CI."
  type        = bool
  default     = false
  nullable    = false
}

variable "planner_account_id" {
  description = "account_id of the planner service account."
  type        = string
  default     = "terraform-planner"
  nullable    = false

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.planner_account_id))
    error_message = "planner_account_id must be 6-30 characters of lowercase letters, digits and hyphens, start with a letter and not end with a hyphen."
  }
}

variable "deployer_bucket_role_id" {
  description = "ID of the deployer's custom project role for the stack's buckets (deployer.tf, T44): bucket create, update, delete and IAM, plus listing and deleting objects to empty a bucket on destroy; no object reads or writes. A deleted custom role keeps its ID reserved for about 37 days."
  type        = string
  default     = "pulsebridgeDeployerBuckets"
  nullable    = false

  validation {
    condition     = can(regex("^[a-zA-Z0-9_.]{3,64}$", var.deployer_bucket_role_id))
    error_message = "deployer_bucket_role_id must be 3-64 letters, digits, '_' or '.'."
  }
}

variable "planner_role_id" {
  description = "ID of the planner's custom project role. A deleted custom role keeps its ID reserved for about 37 days."
  type        = string
  default     = "pulsebridgeTerraformPlanner"
  nullable    = false

  validation {
    condition     = can(regex("^[a-zA-Z0-9_.]{3,64}$", var.planner_role_id))
    error_message = "planner_role_id must be 3-64 letters, digits, '_' or '.'."
  }
}

variable "planner_extra_permissions" {
  description = "Permissions added to the planner's custom role (ci_identities.tf), if a real plan is refused on one the built-in list lacks. Only `<service>.<resource>.get` or `.getIamPolicy` are accepted, and never one that reads data (FHIR resources, HL7v2 messages, log entries, secret values, objects)."
  type        = list(string)
  default     = []
  nullable    = false

  validation {
    condition = alltrue([
      for p in var.planner_extra_permissions :
      can(regex("^[a-z0-9]+\\.[A-Za-z0-9]+\\.(get|getIamPolicy)$", p)) && !can(regex("(?i)(fhirResources|hl7V2Messages|dicom|consent|logEntries|versions|objects|SerialPort|Screenshot)", p))
    ])
    error_message = "planner_extra_permissions may contain only <service>.<resource>.get or .getIamPolicy permissions that read no data (no FHIR resources, HL7v2 messages, log entries, secret versions or objects)."
  }
}

variable "planner_wif_contexts" {
  description = "The CI contexts whose jobs may impersonate the planner. Default [\"pull-request\"] is dev's. prod: [\"env-prod-plan-main\"] (the prod-plan Environment on main); with github_environments set, \"pull-request\" is refused."
  type        = list(string)
  default     = ["pull-request"]
  nullable    = false

  validation {
    condition     = length(var.planner_wif_contexts) > 0 && alltrue([for c in var.planner_wif_contexts : can(regex("^(pull-request|main|env-[A-Za-z0-9._-]{1,255}-main)$", c))])
    error_message = "planner_wif_contexts must be a non-empty list of \"pull-request\", \"main\" or \"env-<environment>-main\"."
  }

  validation {
    condition     = length(var.github_environments) == 0 || alltrue([for c in var.planner_wif_contexts : contains([for e in var.github_environments : "env-${e}-main"], c)])
    error_message = "With github_environments set (prod), planner_wif_contexts must name those environments, for example [\"env-prod-plan-main\"]."
  }

  validation {
    condition     = length(setintersection(toset(var.planner_wif_contexts), toset(var.deployer_wif_contexts))) == 0
    error_message = "planner_wif_contexts and deployer_wif_contexts must not share a context: a planning job would hold the deployer."
  }
}

variable "create_page_deployer" {
  description = "Create the page deployer service account (ci_identities.tf, T26) used by CI's page deploy jobs: roles/firebasehosting.admin and roles/serviceusage.serviceUsageViewer on the project, nothing else here (the env roots add roles/run.viewer on fhir-proxy's service only; T41). Default false."
  type        = bool
  default     = false
  nullable    = false
}

variable "page_deployer_account_id" {
  description = "account_id of the page deployer service account."
  type        = string
  default     = "page-deployer"
  nullable    = false

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.page_deployer_account_id))
    error_message = "page_deployer_account_id must be 6-30 characters of lowercase letters, digits and hyphens, start with a letter and not end with a hyphen."
  }
}

variable "page_deployer_wif_contexts" {
  description = "The CI contexts whose jobs may impersonate the page deployer: its own GitHub Environment on main, never a context of the deployer or the planner (a page job runs npm packages, which could otherwise ask for their token). Default [\"env-dev-page-main\"] is dev's; prod: [\"env-prod-page-main\"]."
  type        = list(string)
  default     = ["env-dev-page-main"]
  nullable    = false

  validation {
    condition     = length(var.page_deployer_wif_contexts) > 0 && alltrue([for c in var.page_deployer_wif_contexts : can(regex("^env-[A-Za-z0-9._-]{1,255}-main$", c))])
    error_message = "page_deployer_wif_contexts must be a non-empty list of \"env-<environment>-main\": the page deployer has its own GitHub Environment."
  }

  validation {
    condition     = length(var.github_environments) == 0 || alltrue([for c in var.page_deployer_wif_contexts : contains([for e in var.github_environments : "env-${e}-main"], c)])
    error_message = "With github_environments set (prod), page_deployer_wif_contexts must name one of them, for example [\"env-prod-page-main\"]."
  }

  validation {
    condition     = length(setintersection(toset(var.page_deployer_wif_contexts), toset(concat(var.deployer_wif_contexts, var.planner_wif_contexts)))) == 0
    error_message = "page_deployer_wif_contexts must share no context with deployer_wif_contexts or planner_wif_contexts: the page job would then hold the deployer or the planner."
  }
}

variable "allow_build_trigger" {
  description = "Grant the deployer roles/cloudbuild.builds.editor, which creating project-base's Cloud Build trigger needs (T26). Default false: set it only together with build_trigger_repository in an env root. The role also lets the holder start builds."
  type        = bool
  default     = false
  nullable    = false
}

variable "wif_pool_id" {
  description = "ID of the Workload Identity pool. 4-32 lowercase letters, digits and hyphens; the \"gcp-\" prefix is reserved. A deleted pool's ID cannot be reused for 30 days."
  type        = string
  default     = "github-actions"
  nullable    = false

  validation {
    condition     = can(regex("^[a-z0-9-]{4,32}$", var.wif_pool_id)) && !startswith(var.wif_pool_id, "gcp-")
    error_message = "wif_pool_id must be 4-32 lowercase letters, digits and hyphens and must not start with \"gcp-\"."
  }
}

variable "wif_provider_id" {
  description = "ID of the OIDC provider inside the pool. 4-32 lowercase letters, digits and hyphens; the \"gcp-\" prefix is reserved."
  type        = string
  default     = "github-oidc"
  nullable    = false

  validation {
    condition     = can(regex("^[a-z0-9-]{4,32}$", var.wif_provider_id)) && !startswith(var.wif_provider_id, "gcp-")
    error_message = "wif_provider_id must be 4-32 lowercase letters, digits and hyphens and must not start with \"gcp-\"."
  }
}
