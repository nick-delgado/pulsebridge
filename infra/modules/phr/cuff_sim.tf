# cuff-sim (T13): builds one HL7v2 ORU^R01 blood-pressure message per request, sends it to the
# HL7v2 store over MLLP (through the edge VM's adapter) or HTTPS ingest, and records every send
# attempt as an AuditEvent in the audit FHIR store (D12, D15). Settings follow
# services/cuff-sim/README.md ("Time limits", "Who may call it", "Configuration", "Running").

resource "google_cloud_run_v2_service" "cuff_sim" {
  project     = var.project_id
  location    = var.region
  name        = local.cuff_sim_name
  description = "PulseBridge cuff-sim: simulated blood-pressure cuff, HL7v2 ORU^R01 over MLLP or HTTPS ingest. Invoked only by fhir-proxy."
  labels      = var.labels

  # Internal only: only VPC and Google-internal sources reach it. fhir-proxy reaches it because
  # it sends ALL its traffic through the VPC (fhir_proxy.tf). D10: it is not public.
  ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  # Keep Cloud Run's IAM check on (the default, stated so tests/static_checks.sh can see it):
  # Cloud Run checks fhir-proxy's OIDC token before a request reaches the container, which has
  # no authentication code of its own ("Who may call it").
  invoker_iam_disabled = false

  # The provider defaults this to true, which makes `terraform destroy` fail. No data here.
  deletion_protection = var.deletion_protection

  template {
    service_account = var.service_account_emails["cuff-sim"]

    # min 0: scale to zero. max: the rate limit is a token bucket per instance (one gunicorn
    # worker per instance), so the service-wide limit is 6 sends a minute x this number.
    scaling {
      min_instance_count = 0
      max_instance_count = var.cuff_sim_max_instances
    }

    # One worker with four threads (README "Running"): four requests at a time per instance.
    max_instance_request_concurrency = 4

    # Above fhir-proxy's forward timeout (55 s), so an attempt the proxy has already answered
    # 504 for still finishes and records its AuditEvent; below would cut it off unrecorded.
    timeout = "${local.timeouts.cuff_request}s"

    # Direct VPC egress (D14): the instance gets addresses in the serverless subnet, the one
    # source project-base's firewall lets reach the VM's MLLP port. PRIVATE_RANGES_ONLY: only
    # traffic to private addresses (the VM) goes through the VPC; its Cloud Healthcare calls
    # take Cloud Run's normal path to Google's public endpoints. It calls no internal-only
    # service, so it has no reason to route everything through the VPC.
    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = var.network_name
        subnetwork = var.serverless_subnet_name
      }
    }

    containers {
      # By digest (validated in variables.tf), so this apply runs exactly the built image.
      image = var.cuff_sim_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        # CPU only while a request is being handled: an idle instance is not billed for CPU.
        cpu_idle          = true
        startup_cpu_boost = false
      }

      # GET /cuff/samples lists T07's samples from the installed package data: offline, no
      # token, no rate limit, and it answers only once create_app() has checked the whole
      # configuration (the image's gunicorn --preload: a bad configuration exits before port
      # 8080 opens). Probes come from Cloud Run inside the instance, not through the service
      # URL, so ingress and IAM do not apply. Cloud Run: "Any 2XX or 3XX response is a
      # success"; timeout must not exceed the period; failureThreshold x periodSeconds <= 600 s.
      startup_probe {
        initial_delay_seconds = 0
        period_seconds        = 10
        timeout_seconds       = 5
        failure_threshold     = 12 # 120 s for a cold start
        http_get {
          path = "/cuff/samples"
          port = 8080
        }
      }

      # services/cuff-sim/README.md, "Configuration". RATE_LIMIT_*, SENDING_* and PORT keep the
      # service's (and Cloud Run's) defaults. No secret: cuff-sim reads none.
      dynamic "env" {
        for_each = {
          GCP_PROJECT_ID              = local.healthcare_project_id
          HEALTHCARE_LOCATION         = var.healthcare_location
          HEALTHCARE_DATASET_ID       = var.healthcare_dataset_id
          HL7V2_STORE_ID              = var.hl7v2_store_id
          AUDIT_FHIR_STORE_ID         = var.audit_fhir_store_id
          MLLP_HOST                   = var.mllp_host
          MLLP_PORT                   = tostring(var.mllp_port)
          MLLP_TIMEOUT_SECONDS        = tostring(local.timeouts.cuff_mllp)
          HTTPS_TIMEOUT_SECONDS       = tostring(local.timeouts.cuff_https)
          PATIENT_ID                  = var.patient_id
          PATIENT_ASSIGNING_AUTHORITY = var.patient_assigning_authority
          PATIENT_FAMILY_NAME         = local.patient_family_name
          PATIENT_GIVEN_NAME          = local.patient_given_name
        }
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }
}
