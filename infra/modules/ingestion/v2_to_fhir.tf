# v2-to-fhir (T12): receives the HL7v2 store's notifications from Pub/Sub push at
# POST /pubsub/hl7v2, fetches the stored ORU^R01, maps it to a FHIR Observation and labels the
# message with its application-level outcome (D7). Deployed in every configuration: the cuff
# path works with real and with synthetic wearable data.
#
# The push subscription itself lives in module healthcare (push_enabled, push_endpoint,
# push_service_account_email). This module creates the service it pushes to and the one invoker
# binding that lets the push identity in; output v2_to_fhir_push_endpoint carries the URL.

resource "google_cloud_run_v2_service" "v2_to_fhir" {
  project     = var.project_id
  location    = var.region
  name        = local.v2_to_fhir_name
  description = "PulseBridge v2-to-fhir: HL7v2 ORU^R01 notifications (Pub/Sub push) to FHIR R4 Observations."
  labels      = var.labels

  # Internal-only by default: Pub/Sub push from a subscription in the same project counts as
  # internal traffic (README, "Ingress"). The URL still exists; requests from the internet are
  # refused before IAM is even checked.
  ingress = var.v2_to_fhir_ingress

  # Keep Cloud Run's IAM check on (false is the default; stated so a reviewer, and
  # tests/static_checks.sh, can see it). true would let anyone who reaches the URL call it.
  invoker_iam_disabled = false

  # The provider defaults this to true, which makes `terraform destroy` fail. The service holds
  # no data, so dev can be destroyed nightly.
  deletion_protection = var.deletion_protection

  template {
    service_account = var.service_account_emails["v2-to-fhir"]

    # Scale to zero: no instance, no charge, while no message arrives. The first push after an
    # idle spell pays a cold start, which Pub/Sub's 60 s ack deadline (module healthcare) covers.
    scaling {
      min_instance_count = 0
      max_instance_count = var.v2_to_fhir_max_instances
    }

    # gunicorn runs one worker with four threads (README "Running"), so four requests at a time.
    max_instance_request_concurrency = 4
    timeout                          = var.v2_to_fhir_request_timeout

    containers {
      # By digest (validated in variables.tf), so this apply runs exactly the built image.
      image = var.v2_to_fhir_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        # CPU only while a request is being handled ("request-based billing"): an instance kept
        # warm between requests is not billed for CPU.
        cpu_idle          = true
        startup_cpu_boost = false
      }

      # services/v2-to-fhir/README.md, "Configuration". create_app() builds the store resource
      # names from these at start-up and raises on an id that cannot form one. The variables'
      # validation refuses such ids at plan time, so that path should not be reached. If it
      # were, the deploy would NOT reliably fail: gunicorn's master opens port 8080 before its
      # worker builds the app, so Cloud Run's default TCP startup probe can pass while the
      # worker crashes, and pushes then fail (and are retried, then dead-lettered). An HTTP
      # startup probe would close that gap, but this service has no GET route to probe (its
      # only route is POST /pubsub/hl7v2); see the README, "What a bad configuration does".
      dynamic "env" {
        for_each = merge(
          local.healthcare_env,
          { HL7V2_STORE_ID = var.hl7v2_store_id },
          var.patient_assigning_authority == null ? {} : { PATIENT_ASSIGNING_AUTHORITY = var.patient_assigning_authority },
        )
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }
}
