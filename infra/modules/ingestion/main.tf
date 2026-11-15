# Module `ingestion` (task T20): the compute that brings data into the FHIR store.
#
#   Pub/Sub push (module healthcare) --OIDC as pubsub-push--> v2-to-fhir   (always)
#   Cloud Scheduler, every 15 min  --OIDC as scheduler-invoker--> health-sync /sync   (real data)
#   fhir-proxy (module phr)        --OIDC as fhir-proxy--> health-sync /status        (real data)
#   Cloud Scheduler, hourly        --OAuth as scheduler-invoker--> seeder job :run   (synthetic)
#
# Design rules (README.md explains each):
#   - Nothing is always on: every service scales to zero (min_instance_count = 0), the job runs
#     only when executed, Scheduler jobs exist only when ingest_enabled is true.
#   - Nothing is public: no allUsers / allAuthenticatedUsers binding, invoker_iam_disabled is
#     false, and each service has exactly the invokers listed in local.service_invokers.
#   - IAM is resource-level and additive (_iam_member) on resources this module owns, so it cannot
#     remove grants made elsewhere (module identity's README, "who creates which binding").

locals {
  healthcare_project_id = coalesce(var.healthcare_project_id, var.project_id)

  # Which wearable source exists. Exactly one of these is true (T25: they are exclusive).
  deploy_health_sync = !var.use_synthetic_data
  deploy_seeder      = var.use_synthetic_data

  v2_to_fhir_name  = "${var.name_prefix}-v2-to-fhir"
  health_sync_name = "${var.name_prefix}-health-sync"
  seed_name        = "${var.name_prefix}-seed"

  # Coordinates every service reads at start-up (the services' READMEs, "Configuration").
  healthcare_env = {
    GCP_PROJECT_ID        = local.healthcare_project_id
    HEALTHCARE_LOCATION   = var.healthcare_location
    HEALTHCARE_DATASET_ID = var.healthcare_dataset_id
    FHIR_STORE_ID         = var.fhir_store_id
    PATIENT_ID            = var.patient_id
  }

  # The Scheduler interval for health-sync. health-sync's SYNC_INTERVAL_MINUTES must equal it
  # (the service checks LOOKBACK >= FINAL_AFTER + SYNC_INTERVAL), so both come from here.
  sync_interval_minutes = 15

  # The three Google Health secrets health-sync mounts, one directory each (Cloud Run refuses two
  # secret volumes in one directory). Key = identity's secret key = volume name; the file inside
  # is always "value"; env is the variable that tells the service where the file is.
  google_health_secrets = {
    "google-health-client-id"     = "GOOGLE_HEALTH_CLIENT_ID_FILE"
    "google-health-client-secret" = "GOOGLE_HEALTH_CLIENT_SECRET_FILE"
    "google-health-refresh-token" = "GOOGLE_HEALTH_REFRESH_TOKEN_FILE"
  }

  # Who may invoke what (roles/run.invoker). This table is the whole invoker policy of the
  # module; the tests compare the created bindings with it, and tests/static_checks.sh refuses
  # any other invoker resource and any public member.
  service_invokers = merge(
    {
      "v2-to-fhir/pubsub-push" = { service = "v2-to-fhir", workload = "pubsub-push" }
    },
    local.deploy_health_sync ? {
      "health-sync/scheduler-invoker" = { service = "health-sync", workload = "scheduler-invoker" }
      "health-sync/fhir-proxy"        = { service = "health-sync", workload = "fhir-proxy" }
    } : {}
  )

  # The created services by key, so each binding references (and so waits for) its service.
  # health-sync is added only when it exists; indexing health_sync[0] directly would fail when
  # count is 0.
  service_resource_names = merge(
    { "v2-to-fhir" = google_cloud_run_v2_service.v2_to_fhir.name },
    { for s in google_cloud_run_v2_service.health_sync : "health-sync" => s.name },
  )
}

# The invoker bindings (roles/run.invoker = run.routes.invoke on this one service). Additive
# _iam_member, one per (service, caller) in local.service_invokers. Never allUsers or
# allAuthenticatedUsers: neither service is public (D10: fhir-proxy is the one front door).
resource "google_cloud_run_v2_service_iam_member" "invoker" {
  for_each = local.service_invokers

  project  = var.project_id
  location = var.region
  name     = local.service_resource_names[each.value.service]
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.service_account_emails[each.value.workload]}"
}
