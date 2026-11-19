# The self-serve public demo (D16, T44): what fhir-proxy needs to let an anonymous visitor in.
#
#   browser ── "View the live demo" ──> grecaptcha.enterprise.execute(site key, "view_demo")
#      │                                   (Google returns a short-lived token)
#      └── POST /api/auth/session {recaptcha_token} ──> fhir-proxy
#             1. counts the attempt in today's object in the counter bucket (below), refusing
#                once BOT_CHECKS_PER_DAY is reached;
#             2. creates an assessment of the token (projects.assessments.create, its project
#                role is module identity's roles/recaptchaenterprise.agent);
#             3. on a score >= RECAPTCHA_MIN_SCORE, sets the signed __session cookie (the key is
#                the session-signing-key secret, mounted as a file; fhir_proxy.tf).
#
# This file holds the two resources that only exist for that flow: the score-based site key and
# the counter bucket, with the bucket's one IAM grant. The proxy's settings for them are in
# fhir_proxy.tf (RECAPTCHA_*, DEMO_COUNTER_BUCKET, ...), the CSP that lets the page load
# reCAPTCHA in hosting/security-headers.json.

locals {
  # The action name the page passes to execute() and the proxy requires in the assessment's
  # tokenProperties.action (the T43 to T45 contract). Fixed, not a variable: the page has it
  # from /auth/config, so the three can never disagree.
  recaptcha_action = "view_demo"

  # The hosts the page is served from (the T44 AC): Firebase Hosting serves every site on both
  # <site>.web.app and <site>.firebaseapp.com, plus the custom domain when public_origin is set.
  # A web key accepts tokens only from pages on these domains (and their subdomains: "All
  # subdomains of an allowed domain are automatically allowed", provider docs for
  # web_settings.allowed_domains). Host names only: no scheme, port or path.
  # The same hosts as local.page_origins (main.tf), whose origins ALLOWED_ORIGINS accepts, so
  # the key and the proxy's Origin check can never disagree.
  recaptcha_allowed_domains = [for o in local.page_origins : trimprefix(o, "https://")]

  # Bucket names are GLOBAL across all of Cloud Storage, so the project ID (itself globally
  # unique) is part of it; name_prefix keeps two stacks in one project apart. The work plan
  # writes it <prefix>-demo-counters; the project ID is added because "pulsebridge-demo-counters"
  # would collide between strangers who clone this repository.
  demo_counter_bucket_name = "${var.project_id}-${var.name_prefix}-demo-counters"
}

# A reCAPTCHA key of type SCORE ("reCAPTCHA v3 style"): the page never shows a challenge; every
# execute() returns a token, and the assessment of that token gives a score from 0.0 (likely a
# bot) to 1.0 (likely a person). The proxy, not Google, decides what score passes
# (RECAPTCHA_MIN_SCORE). Free to create; assessments are billed above 10,000 a month per
# organisation (T48 documents the cost).
#
# google provider 8.11, resource google_recaptcha_enterprise_key (schema read from the pinned
# provider with `terraform providers schema -json`): display_name is required; web_settings
# takes integration_type (SCORE, CHECKBOX, INVISIBLE, POLICY_BASED_CHALLENGE), allowed_domains,
# allow_all_domains, allow_amp_traffic ("supported only for the SCORE integration type") and
# challenge_security_preference ("only ... for CHECKBOX and INVISIBLE", so not set here). The
# exported `name` is "the same as the Site Key itself": the public key ID the page uses.
resource "google_recaptcha_enterprise_key" "demo" {
  project      = var.project_id
  display_name = "${var.name_prefix} public demo (score)"
  labels       = var.labels

  web_settings {
    integration_type = "SCORE"
    allowed_domains  = local.recaptcha_allowed_domains
    # Stated explicitly: true would let any web page mint tokens for this key.
    allow_all_domains = false
    allow_amp_traffic = false
  }

  # The key ID is public (the page fetches it from /api/auth/config) and holds no data, so a new
  # key after a destroy costs nothing but a redeploy of the proxy's environment. In prod it
  # follows deletion_protection like the Cloud Run services, so a stray destroy cannot take the
  # demo's door away. PREVENT makes the destroy fail; it does not stop an update.
  deletion_policy = var.deletion_protection ? "PREVENT" : "DELETE"

  lifecycle {
    # The contract (T43 to T45): the proxy creates its assessments in GCP_PROJECT_ID, which is
    # the HEALTHCARE project (fhir_proxy.tf). An assessment must be made in the project that owns
    # the key, and fhir-proxy's assessment role (module identity) is in this project. The stack
    # always uses one project; a split one would fail every bot check, so it is refused here.
    precondition {
      condition     = local.healthcare_project_id == var.project_id
      error_message = "healthcare_project_id must equal project_id while the demo bot check is on: fhir-proxy creates reCAPTCHA assessments in GCP_PROJECT_ID (the Healthcare project), and the key and its IAM grant live in project_id."
    }
  }
}

# The daily bot-check counter (T43): one small object per UTC day, incremented by fhir-proxy
# with a generation precondition (ifGenerationMatch) BEFORE each assessment. An object survives
# scale to zero and is shared by every instance, which an in-memory counter is not; that is the
# whole reason this bucket exists. No hourly cost: billed per object stored and per operation,
# both close to nothing at 300 a day.
resource "google_storage_bucket" "demo_counters" {
  project       = var.project_id
  name          = local.demo_counter_bucket_name
  location      = var.region # next to the proxy: no inter-region transfer
  storage_class = "STANDARD"
  labels        = var.labels

  # Access only through IAM, never per-object ACLs, and no public grant possible at all, even by
  # mistake: "enforced" makes Cloud Storage refuse allUsers / allAuthenticatedUsers.
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # A counter is only needed for its day; after 7 days the object is deleted (age counts days
  # since the object was created). Seven, not one, so a few days stay readable when someone asks
  # "how busy was the demo".
  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "Delete"
    }
  }

  # Each increment REPLACES the object (a new generation). With versioning or soft delete every
  # old generation would be kept (and billed) for nothing: off.
  versioning {
    enabled = false
  }
  soft_delete_policy {
    retention_duration_seconds = 0
  }

  # dev (deletion_protection false): `terraform destroy` deletes the counters with the bucket.
  # prod: a destroy fails while a counter exists (at most 7 days after the last visit), like the
  # other protected resources.
  force_destroy = !var.deletion_protection

  lifecycle {
    # A bucket name is 3 to 63 characters (cloud.google.com/storage/docs/buckets#naming).
    precondition {
      condition     = length(local.demo_counter_bucket_name) <= 63
      error_message = "The demo counter bucket name \"<project_id>-<name_prefix>-demo-counters\" is longer than 63 characters; use a shorter name_prefix."
    }
  }
}

# The bucket's only grant: fhir-proxy may read, create, replace and delete objects IN THIS
# BUCKET. roles/storage.objectUser is the narrowest predefined role for that: a generation-
# matched rewrite of an existing object needs storage.objects.create AND storage.objects.delete
# (replacing an object deletes the old generation), plus .get to read the count. It holds no
# bucket-level rights (it cannot change the bucket, its IAM or its lifecycle). Bucket-level, not
# project: the proxy reaches no other bucket, and nobody else is given write access here.
resource "google_storage_bucket_iam_member" "demo_counter_writer" {
  bucket = google_storage_bucket.demo_counters.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${var.service_account_emails["fhir-proxy"]}"
}
