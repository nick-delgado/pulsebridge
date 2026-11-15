# Outputs are this module's contract with the env roots (T25), module healthcare (the push
# subscription) and module phr (fhir-proxy's call to health-sync /status).

output "v2_to_fhir_url" {
  description = "Base URL of the v2-to-fhir service (https://...run.app). Also the OIDC audience Cloud Run checks push tokens against."
  value       = google_cloud_run_v2_service.v2_to_fhir.uri
}

output "v2_to_fhir_push_endpoint" {
  description = "URL module healthcare's push subscription posts to: v2_to_fhir_url plus /pubsub/hl7v2. Pass it as healthcare's push_endpoint. It is only known once pubsub-push may invoke the service, so push never starts before its permission exists."
  value       = "${google_cloud_run_v2_service.v2_to_fhir.uri}/pubsub/hl7v2"

  # An output's value depends only on what it references (here the service). depends_on makes
  # the push subscription, which reads this output, wait for the invoker binding too; otherwise
  # the first pushes could arrive before the grant and be refused (and retried).
  depends_on = [google_cloud_run_v2_service_iam_member.invoker]
}

output "v2_to_fhir_service_name" {
  description = "Name of the v2-to-fhir Cloud Run service."
  value       = google_cloud_run_v2_service.v2_to_fhir.name
}

output "health_sync_url" {
  description = "Base URL of health-sync, or null with synthetic data. fhir-proxy (module phr) calls <url>/status with an OIDC token whose audience is this URL."
  value       = one(google_cloud_run_v2_service.health_sync[*].uri)
}

output "health_sync_service_name" {
  description = "Name of the health-sync Cloud Run service, or null with synthetic data."
  value       = one(google_cloud_run_v2_service.health_sync[*].name)
}

output "seed_job_name" {
  description = "Name of the seeder Cloud Run job, or null without synthetic data. The first seeding runs it by hand: gcloud run jobs execute <name> --region <region> --args=--days,30 --wait."
  value       = one(google_cloud_run_v2_job.seed[*].name)
}

output "region" {
  description = "Region of the services, the job and the Scheduler jobs (for the gcloud commands in the README)."
  value       = var.region
}

output "ingest_enabled" {
  description = "Echo of ingest_enabled. Feed it to module healthcare's push_enabled, so one switch turns on the Scheduler job here and the push subscription there."
  value       = var.ingest_enabled
}

output "scheduler_job_names" {
  description = "Names of the Scheduler jobs that exist (empty while ingest_enabled is false)."
  value       = concat(google_cloud_scheduler_job.health_sync[*].name, google_cloud_scheduler_job.seed[*].name)
}

output "log_metric_names" {
  description = "Log-based metric names by alert key (failed-run, reconnect-needed); empty with synthetic data. In Monitoring they are logging.googleapis.com/user/<name>."
  value       = { for k, m in google_logging_metric.health_sync : k => m.name }
}

output "alert_policy_names" {
  description = "Alert policy resource names by alert key; empty when notification_channels is empty or with synthetic data."
  value       = { for k, p in google_monitoring_alert_policy.health_sync : k => p.name }
}
