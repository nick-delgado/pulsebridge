# fhir-proxy (T14): the dashboard's only backend and the only public service (D10). Anonymous
# demo sessions (D16: a reCAPTCHA bot check, then a signed __session cookie; demo.tf), a narrow
# read API over the FHIR stores and the HL7v2 store, the messages log, and
# cuff forwarding to cuff-sim with its own identity. Settings follow
# services/fhir-proxy/README.md, "For T22: deploying it".

resource "google_cloud_run_v2_service" "fhir_proxy" {
  project     = var.project_id
  location    = var.region
  name        = local.fhir_proxy_name
  description = "PulseBridge fhir-proxy: the dashboard's API behind the Firebase Hosting rewrite /api/**. Public; the demo session cookie is the gate."
  labels      = var.labels

  # All: Firebase Hosting reaches the service at its default run.app URL from outside the VPC.
  # Firebase Hosting is not among the sources Cloud Run counts as "internal", and it is not an
  # external Application Load Balancer, so neither restrictive setting admits it
  # (docs.cloud.google.com/run/docs/securing/ingress; README, "Public access and ingress").
  ingress = "INGRESS_TRAFFIC_ALL"

  # Public. Google: "The recommended way to make a public service is to disable the Cloud Run
  # Invoker IAM check" (docs.cloud.google.com/run/docs/authenticating/public). With the check
  # off, Cloud Run does not check the caller at all; the proxy's own __session cookie is the
  # gate. No allUsers binding is created.
  invoker_iam_disabled = true

  # The provider defaults this to true, which makes `terraform destroy` fail. No data here.
  deletion_protection = var.deletion_protection

  template {
    service_account = var.service_account_emails["fhir-proxy"]

    # min 0: scale to zero. max 2: the per-address session limit is per process and the image
    # runs one worker, so the real limit is SESSIONS_PER_ADDRESS_PER_HOUR x 2; two instances
    # also cap the bill if someone floods the public URL. The DAILY bot-check cap does not
    # multiply: it is counted in the bucket, shared by both (demo.tf).
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    # Equal to gunicorn's thread count (--threads 8, one worker): each request holds one thread
    # for its whole life, so a higher number would queue requests inside a busy instance instead
    # of starting the second one, and a slow client trickling a body holds one of only 8.
    max_instance_request_concurrency = 8

    # The hard stop above both of the proxy's own budgets (70 s: main.tf explains why not 120);
    # REQUEST_TIMEOUT_SECONDS below is the same number (the proxy checks its budgets are at
    # least 10 s below it).
    timeout = "${local.timeouts.proxy_request}s"

    # Direct VPC egress (D14) with ALL_TRAFFIC: every outbound request, including the ones to
    # cuff-sim's and health-sync's *.run.app URLs and to Google APIs, goes through the VPC.
    # Only then do the internal-only services count the call as internal ("Requests from Cloud
    # Run ... require additional configuration before they are recognized as internal",
    # docs.cloud.google.com/run/docs/securing/private-networking). The subnet has Private
    # Google Access (project-base), so Google APIs are reached without Cloud NAT. The proxy
    # talks only to Google services and the VM, so nothing needs the internet.
    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = var.network_name
        subnetwork = var.serverless_subnet_name
      }
    }

    # Each secret as a volume, read from Secret Manager on every file read at version "latest":
    # "When reading a volume, Cloud Run always fetches the secret value from the Secret Manager
    # to use the value with the latest version" (docs.cloud.google.com/run/docs/configuring/
    # services/secrets). A new signing key therefore takes effect without a redeploy, once the
    # proxy reads the file again. An environment variable would be resolved once at instance
    # start, and would put the value in the service's configuration.
    #
    # Cloud Run checks at DEPLOY time that every mounted secret has a version to resolve: a
    # revision that mounts a secret with no enabled version fails to deploy. So the signing
    # key's version must be added (scripts/secrets/set-session-signing-key.sh) after the
    # identity apply and before this service is created or changed (infra/envs/README.md,
    # "Rolling out the public demo").
    dynamic "volumes" {
      for_each = local.proxy_secret_files
      content {
        name = volumes.key
        secret {
          secret = volumes.value.secret
          items {
            version = "latest"
            path    = "value"
          }
        }
      }
    }

    containers {
      # By digest (validated in variables.tf), so this apply runs exactly the built image.
      image = var.fhir_proxy_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi" # measured headroom for 8 threads (no scrypt since D16)
        }
        cpu_idle          = true
        startup_cpu_boost = false
      }

      # A TCP probe, stated explicitly. There is no HTTP route that proves the app started and
      # answers without a token: every GET answers 401, which a probe counts as a failure
      # ("Any 2XX or 3XX response is a success"), and adding a public route is not wanted. The
      # image's gunicorn --preload makes the TCP probe meaningful: create_app() checks the
      # whole configuration in the master BEFORE port 8080 opens, so a bad configuration exits
      # without ever listening and this probe fails (tests/tooling/test_image_smoke.py checks
      # that inside the container). Cloud Run's default would be one 240 s attempt.
      startup_probe {
        initial_delay_seconds = 0
        period_seconds        = 10
        timeout_seconds       = 5
        failure_threshold     = 12 # 120 s for a cold start
        tcp_socket {
          port = 8080
        }
      }

      # /secrets/<volume>/value, one directory per secret.
      dynamic "volume_mounts" {
        for_each = local.proxy_secret_files
        content {
          name       = volume_mounts.key
          mount_path = "/secrets/${volume_mounts.key}"
        }
      }

      # services/fhir-proxy/README.md, "For T22", variable by variable (fhir_proxy/config.py).
      # Only file PATHS of secrets are here; no secret value is ever an environment variable
      # (tests/static_checks.sh refuses value_source / secret_key_ref).
      dynamic "env" {
        for_each = merge(
          {
            GCP_PROJECT_ID        = local.healthcare_project_id
            HEALTHCARE_LOCATION   = var.healthcare_location
            HEALTHCARE_DATASET_ID = var.healthcare_dataset_id
            FHIR_STORE_ID         = var.fhir_store_id
            AUDIT_FHIR_STORE_ID   = var.audit_fhir_store_id
            HL7V2_STORE_ID        = var.hl7v2_store_id
            PATIENT_ID            = var.patient_id
            # The same PID values cuff-sim sends: with them the trace shows a sent sample's
            # exact bytes (the proxy requires all three or none).
            PATIENT_ASSIGNING_AUTHORITY = var.patient_assigning_authority
            PATIENT_FAMILY_NAME         = local.patient_family_name
            PATIENT_GIVEN_NAME          = local.patient_given_name
            # The identity token's audience is this URL (Cloud Run checks it).
            CUFF_SIM_URL = google_cloud_run_v2_service.cuff_sim.uri
            # One front door: the page reaches the proxy only through the rewrite.
            PATH_PREFIX     = local.path_prefix
            PUBLIC_BASE_URL = "${local.public_origin}${local.path_prefix}"
            ALLOWED_ORIGINS = join(",", concat([local.public_origin], var.extra_allowed_origins))
            # 2 by default: measured behind Firebase Hosting (README, "The client address and
            # the direct run.app URL").
            TRUSTED_PROXY_HOPS = tostring(var.trusted_proxy_hops)
            # The session token's `iss`: deployment-specific, so a token from another PulseBridge
            # deployment never verifies here even if a key were reused.
            TOKEN_ISSUER = "pulsebridge-fhir-proxy:${var.project_id}:${var.name_prefix}"
            # D16, the T43 to T45 contract (demo.tf). The site key is public; the page reads it
            # from GET /auth/config, never from its build. The assessment is created in
            # GCP_PROJECT_ID, the project that owns the key.
            RECAPTCHA_SITE_KEY            = google_recaptcha_enterprise_key.demo.name
            RECAPTCHA_ACTION              = local.recaptcha_action
            RECAPTCHA_MIN_SCORE           = tostring(var.recaptcha_min_score)
            SESSION_TTL_SECONDS           = tostring(var.session_ttl_seconds)
            SESSIONS_PER_ADDRESS_PER_HOUR = tostring(var.sessions_per_address_per_hour)
            BOT_CHECKS_PER_DAY            = tostring(var.bot_checks_per_day)
            DEMO_COUNTER_BUCKET           = google_storage_bucket.demo_counters.name
            # Under Firebase Hosting's 60 s; the request timeout is the same as Cloud Run's.
            REQUEST_BUDGET_SECONDS       = tostring(local.timeouts.proxy_request_budget)
            CUFF_FORWARD_TIMEOUT_SECONDS = tostring(local.timeouts.proxy_cuff_forward)
            REQUEST_TIMEOUT_SECONDS      = tostring(local.timeouts.proxy_request)
          },
          # Absent with synthetic data: the status strip then says health-sync-not-deployed.
          var.health_sync_url == null ? {} : { HEALTH_SYNC_URL = var.health_sync_url },
          # T15's settings, only when module openemr's outputs are given.
          var.openemr == null ? {} : {
            OPENEMR_SITE_ADDRESS  = var.openemr.site_address
            OPENEMR_TOKEN_URL     = var.openemr.token_url
            OPENEMR_FHIR_BASE_URL = var.openemr.fhir_base_url
            OPENEMR_SCOPES        = var.openemr.scopes
          },
          { for key, f in local.proxy_secret_files : f.env => "/secrets/${key}/value" },
        )
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  # The first forwarded cuff request must not meet a 403: the invoker grant on cuff-sim exists
  # before this revision serves. Likewise the first demo session must not meet a 403 on the
  # counter bucket (503 bot-check-unavailable).
  depends_on = [google_cloud_run_v2_service_iam_member.invoker, google_storage_bucket_iam_member.demo_counter_writer]
}
