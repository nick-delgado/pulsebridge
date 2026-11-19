# Module `phr` (task T22): the dashboard's front door and the cuff simulator.
#
#   browser ──HTTPS──> Firebase Hosting site (static page from web/dist, security headers)
#                        │ rewrite /api/**  (same origin: no CORS needed)
#                        ▼
#                      fhir-proxy  (Cloud Run, public: the __session cookie is the gate, D10/D16)
#                        ├── reCAPTCHA (Fraud Defense): one assessment per demo session (demo.tf)
#                        ├── Cloud Storage: the daily bot-check counter bucket (demo.tf)
#                        │ Direct VPC egress, ALL_TRAFFIC, serverless subnet (D14)
#                        ├── Cloud Healthcare API: FHIR store, audit FHIR store, HL7v2 store (read)
#                        ├── cuff-sim   (internal only; invoker = fhir-proxy's account only)
#                        ├── health-sync /status (module ingestion; internal only)
#                        └── edge VM: OpenEMR HTTPS (T15, later)
#                      cuff-sim
#                        │ Direct VPC egress, PRIVATE_RANGES_ONLY, serverless subnet
#                        ├── edge VM: MLLP adapter on mllp_host:mllp_port
#                        └── Cloud Healthcare API: HL7v2 ingest, audit FHIR store (write)
#
# Design rules (README.md explains each):
#   - Nothing is always on: both services scale to zero (min_instance_count = 0); Hosting has no
#     hourly charge.
#   - Exactly one thing is public: fhir-proxy, with Cloud Run's invoker IAM check disabled
#     (Google's recommended way to make a service public). No allUsers / allAuthenticatedUsers
#     binding anywhere. cuff-sim keeps the IAM check, and only fhir-proxy's account may invoke it.
#   - IAM is resource-level and additive (_iam_member) on resources this module owns.
#   - Secrets reach fhir-proxy only as files (secret volumes at version "latest").

locals {
  healthcare_project_id = coalesce(var.healthcare_project_id, var.project_id)

  fhir_proxy_name = "${var.name_prefix}-fhir-proxy"
  cuff_sim_name   = "${var.name_prefix}-cuff-sim"

  # The patient's name in the messages cuff-sim builds (PID-5). With synthetic data it is the
  # seeder's synthetic name, which variables.tf enforces: the seeder refuses to touch a Patient
  # with any other name, and the cuff path's first message creates the Patient by conditional
  # create (D11) if the seeder has not.
  patient_family_name = var.use_synthetic_data ? "Synthetic" : var.patient_family_name
  patient_given_name  = var.use_synthetic_data ? "Demo" : var.patient_given_name

  # Every timeout in the request path, in seconds, in one place (README, "Timeouts end to end").
  # The relations between them are asserted by the tests:
  #   cuff-sim worst case (MLLP and HTTPS) < proxy cuff forward timeout < Hosting's 60 s limit
  #   proxy cuff forward timeout < cuff-sim's own request timeout
  #   proxy budgets + 10 s <= proxy request timeout (the proxy refuses to start otherwise)
  # Hosting's 60 s also includes the proxy's cold start (README, "Timeouts end to end").
  timeouts = {
    # Firebase Hosting ends a rewritten request at 60 s: "Firebase Hosting is subject to a
    # 60-second request timeout" (firebase.google.com/docs/hosting/cloud-run). Not settable.
    hosting_limit = 60
    # fhir-proxy (services/fhir-proxy/README.md, "Time budget").
    proxy_request_budget = 50 # REQUEST_BUDGET_SECONDS: every route but the cuff send
    proxy_cuff_forward   = 55 # CUFF_FORWARD_TIMEOUT_SECONDS: POST /cuff/readings
    # REQUEST_TIMEOUT_SECONDS = the Cloud Run request timeout. The proxy requires each budget to
    # be at least 10 s below it (55 + 10 = 65). Behind Hosting nothing waits longer than 60 s, so
    # a larger value only serves direct run.app callers and lets a slow (trickled) request body
    # hold one of the 8 threads longer: 70 s, not the service's default 120 s.
    proxy_request = 70
    # cuff-sim (services/cuff-sim/README.md, "Time limits"). T = HTTPS_TIMEOUT_SECONDS.
    cuff_mllp  = 5 # MLLP_TIMEOUT_SECONDS: one deadline for connect, write and the ACK frame
    cuff_https = 6 # HTTPS_TIMEOUT_SECONDS, lowered from 10 so the HTTPS worst case fits in 55 s
    # Its own Cloud Run request timeout: above the proxy's forward timeout, so a send the proxy
    # already answered 504 for still finishes and records its AuditEvent (D12). This replaces
    # the work plan's older "90 s" (sized for HTTPS_TIMEOUT_SECONDS = 10 without Hosting).
    cuff_request = 60
  }
  # cuff-sim's documented worst cases: each Healthcare call is up to 2 x T, doubled by a token
  # refresh and a 401 retry. MLLP = MLLP + audit write; HTTPS = ingest + audit write.
  cuff_worst_case = {
    mllp  = local.timeouts.cuff_mllp + 4 * local.timeouts.cuff_https # 5 + 24 = 29 s
    https = 8 * local.timeouts.cuff_https                            # 48 s
  }

  # The rewrite puts the proxy under /api on the page's own origin. Hosting forwards the full
  # path (/api/status), so the proxy serves every route under PATH_PREFIX.
  path_prefix   = "/api"
  site_id       = coalesce(var.hosting_site_id, var.project_id)
  public_origin = coalesce(var.public_origin, "https://${local.site_id}.web.app")

  # The secrets fhir-proxy mounts, one directory each (Cloud Run refuses two secret volumes in
  # one directory). Key = volume name; the file inside is always "value"; env is the variable
  # that tells the service where the file is.
  proxy_secret_files = merge(
    {
      # D16: the HS256 key of the demo session token (32 random bytes).
      "session-signing-key" = {
        secret = var.secret_ids["session-signing-key"]
        env    = "SESSION_SIGNING_KEY_FILE"
      }
    },
    var.openemr == null ? {} : {
      "openemr-client" = {
        secret = var.openemr.client_secret_id
        env    = "OPENEMR_CLIENT_FILE"
      }
    },
  )

  # Who may invoke what (roles/run.invoker). The whole invoker policy of this module; the tests
  # compare the created bindings with it, and tests/static_checks.sh refuses any other invoker
  # resource and any public member. fhir-proxy's invoker right on health-sync is module
  # ingestion's (it owns that service), so it is not repeated here.
  service_invokers = {
    "cuff-sim/fhir-proxy" = { service = "cuff-sim", workload = "fhir-proxy" }
  }
  service_resource_names = {
    "cuff-sim" = google_cloud_run_v2_service.cuff_sim.name
  }
}

# The invoker bindings. Additive _iam_member, one per row of local.service_invokers.
resource "google_cloud_run_v2_service_iam_member" "invoker" {
  for_each = local.service_invokers

  project  = var.project_id
  location = var.region
  name     = local.service_resource_names[each.value.service]
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.service_account_emails[each.value.workload]}"
}

# CI's page deployer may READ the fhir-proxy service (roles/run.viewer: run.services.get), and
# only that service. Firebase Hosting checks run.services.get on a rewrite's Cloud Run target when
# a version is finalized (`firebase deploy`), so without this the page deploy fails after upload.
# Additive _iam_member on a resource this module owns, like the invoker grants; resource-level so
# the account cannot read the other services' configuration. Optional: null means no grant.
resource "google_cloud_run_v2_service_iam_member" "page_deployer_viewer" {
  count = var.page_deployer_service_account_email == null ? 0 : 1

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.fhir_proxy.name
  role     = "roles/run.viewer"
  member   = "serviceAccount:${var.page_deployer_service_account_email}"
}
