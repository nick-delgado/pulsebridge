# use_synthetic_data = false: a real Google Health deployment (health-sync exists).
# Mocked Google providers only; run with Google credentials removed (make tf-test).

mock_provider "google" {
  override_resource {
    target = google_cloud_run_v2_service.cuff_sim
    values = { uri = "https://pulsebridge-cuff-sim-abc123-uc.a.run.app" }
  }
  override_resource {
    target = google_cloud_run_v2_service.fhir_proxy
    values = { uri = "https://pulsebridge-fhir-proxy-abc123-uc.a.run.app" }
  }
  # A synthetic site key ID (the real one is Google's random 40-character ID).
  override_resource {
    target = google_recaptcha_enterprise_key.demo
    values = { name = "synthetic-test-site-key-0000000000000000" }
  }
}

mock_provider "google-beta" {
  override_resource {
    target = google_firebase_hosting_site.dashboard
    values = { default_url = "https://example-project.web.app" }
  }
}

variables {
  project_id            = "example-project"
  region                = "us-central1"
  healthcare_location   = "us-central1"
  healthcare_dataset_id = "pulsebridge"
  fhir_store_id         = "pulsebridge-fhir"
  audit_fhir_store_id   = "pulsebridge-audit"
  hl7v2_store_id        = "pulsebridge-hl7v2"

  patient_id                  = "PAT-0001"
  patient_assigning_authority = "EXAMPLE"
  patient_family_name         = "Example"
  patient_given_name          = "Pat"

  fhir_proxy_image = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/fhir-proxy@sha256:4444444444444444444444444444444444444444444444444444444444444444"
  cuff_sim_image   = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/cuff-sim@sha256:5555555555555555555555555555555555555555555555555555555555555555"

  service_account_emails = {
    "fhir-proxy"  = "pb-fhir-proxy@example-project.iam.gserviceaccount.com"
    "cuff-sim"    = "pb-cuff-sim@example-project.iam.gserviceaccount.com"
    "health-sync" = "pb-health-sync@example-project.iam.gserviceaccount.com"
  }
  secret_ids = {
    "session-signing-key"        = "pulsebridge-session-signing-key"
    "openemr-client-private-key" = "pb-openemr-client-private-key"
  }

  network_name           = "pulsebridge-vpc"
  serverless_subnet_name = "pulsebridge-serverless"
  mllp_host              = "10.10.0.2"
  mllp_port              = 2575

  health_sync_url = "https://pulsebridge-health-sync-abc123-uc.a.run.app"
}

# ---- who can reach what ---------------------------------------------------------------------

run "only_fhir_proxy_is_public" {
  command = apply

  assert {
    condition     = google_cloud_run_v2_service.fhir_proxy.invoker_iam_disabled == true && google_cloud_run_v2_service.fhir_proxy.ingress == "INGRESS_TRAFFIC_ALL"
    error_message = "fhir-proxy must be public the way a Hosting rewrite needs: invoker IAM check off, ingress ALL (Hosting calls its run.app URL from outside the VPC)."
  }
  assert {
    condition     = google_cloud_run_v2_service.cuff_sim.invoker_iam_disabled == false && google_cloud_run_v2_service.cuff_sim.ingress == "INGRESS_TRAFFIC_INTERNAL_ONLY"
    error_message = "cuff-sim must keep the IAM check and be internal-only (D10)."
  }
  assert {
    condition = toset([
      for b in google_cloud_run_v2_service_iam_member.invoker : "${b.name} ${b.role} ${b.member}"
    ]) == toset(["pulsebridge-cuff-sim roles/run.invoker serviceAccount:pb-fhir-proxy@example-project.iam.gserviceaccount.com"])
    error_message = "The only invoker binding must be fhir-proxy's account on cuff-sim."
  }
  assert {
    condition = alltrue([
      for b in google_cloud_run_v2_service_iam_member.invoker : !contains(["allUsers", "allAuthenticatedUsers"], b.member)
    ])
    error_message = "No binding may name a public principal."
  }
}

# T41: CI's page deployer may read the fhir-proxy service (Hosting checks run.services.get on a
# rewrite's target when a version is finalized), and nothing else. Off by default.
run "no_page_deployer_grant_by_default" {
  command = apply

  assert {
    condition     = length(google_cloud_run_v2_service_iam_member.page_deployer_viewer) == 0
    error_message = "With page_deployer_service_account_email null there must be no page deployer grant."
  }
}

run "page_deployer_reads_fhir_proxy_only" {
  command = apply
  variables {
    page_deployer_service_account_email = "page-deployer@example-project.iam.gserviceaccount.com"
  }

  assert {
    condition = [
      for b in google_cloud_run_v2_service_iam_member.page_deployer_viewer : "${b.project} ${b.location} ${b.name} ${b.role} ${b.member}"
    ] == ["example-project us-central1 pulsebridge-fhir-proxy roles/run.viewer serviceAccount:page-deployer@example-project.iam.gserviceaccount.com"]
    error_message = "The page deployer must get exactly one grant: roles/run.viewer on the fhir-proxy service."
  }
  assert {
    condition     = google_cloud_run_v2_service_iam_member.page_deployer_viewer[0].name == google_cloud_run_v2_service.fhir_proxy.name
    error_message = "The page deployer's grant must be on the fhir-proxy service (the /api rewrite's target), not cuff-sim."
  }
  assert {
    # The invoker policy is unchanged: the page deployer may not invoke anything.
    condition = toset([
      for b in google_cloud_run_v2_service_iam_member.invoker : "${b.name} ${b.role} ${b.member}"
    ]) == toset(["pulsebridge-cuff-sim roles/run.invoker serviceAccount:pb-fhir-proxy@example-project.iam.gserviceaccount.com"])
    error_message = "Setting the page deployer must not change the invoker bindings."
  }
}

# ---- scaling, concurrency, timeouts ---------------------------------------------------------

run "both_scale_to_zero_with_caps" {
  command = apply

  assert {
    condition = (
      google_cloud_run_v2_service.fhir_proxy.template[0].scaling[0].min_instance_count == 0 &&
      google_cloud_run_v2_service.cuff_sim.template[0].scaling[0].min_instance_count == 0
    )
    error_message = "Both services must scale to zero."
  }
  assert {
    condition     = google_cloud_run_v2_service.fhir_proxy.template[0].scaling[0].max_instance_count == 2
    error_message = "fhir-proxy must be capped at two instances."
  }
  assert {
    condition     = google_cloud_run_v2_service.cuff_sim.template[0].scaling[0].max_instance_count == 1 && output.cuff_sim_rate_limit_per_minute == 6
    error_message = "cuff-sim defaults to one instance, so its per-instance rate limit (6 a minute) is service-wide."
  }
  assert {
    condition     = google_cloud_run_v2_service.fhir_proxy.template[0].max_instance_request_concurrency == 8
    error_message = "fhir-proxy's per-instance concurrency must equal gunicorn's thread count (8)."
  }
  assert {
    condition     = google_cloud_run_v2_service.cuff_sim.template[0].max_instance_request_concurrency == 4
    error_message = "cuff-sim's concurrency must equal its four threads."
  }
}

run "timeouts_fit_under_hostings_60_seconds" {
  command = apply

  # Read from what is deployed (the env maps and the Cloud Run timeouts), not from the locals.

  assert {
    condition     = google_cloud_run_v2_service.fhir_proxy.template[0].timeout == "${ { for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name => e.value }["REQUEST_TIMEOUT_SECONDS"]}s"
    error_message = "fhir-proxy's Cloud Run request timeout must equal its REQUEST_TIMEOUT_SECONDS."
  }
  assert {
    # proxy forward timeout < 60 (Hosting) and < cuff-sim's own request timeout
    condition = (
      tonumber({ for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name => e.value }["CUFF_FORWARD_TIMEOUT_SECONDS"]) < 60 &&
      tonumber({ for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name => e.value }["REQUEST_BUDGET_SECONDS"]) < 60 &&
      tonumber({ for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name => e.value }["CUFF_FORWARD_TIMEOUT_SECONDS"]) < tonumber(trimsuffix(google_cloud_run_v2_service.cuff_sim.template[0].timeout, "s"))
    )
    error_message = "The proxy's budgets must be under Hosting's 60 s, and its cuff forward timeout below cuff-sim's request timeout (so an attempt the proxy gave up on is still recorded)."
  }
  assert {
    # cuff-sim's worst cases (its README, "Time limits"): MLLP = MLLP_TIMEOUT + 4 x T,
    # HTTPS = 8 x T, T = HTTPS_TIMEOUT_SECONDS; both below the proxy's forward timeout.
    condition = (
      tonumber({ for e in google_cloud_run_v2_service.cuff_sim.template[0].containers[0].env : e.name => e.value }["MLLP_TIMEOUT_SECONDS"]) + 4 * tonumber({ for e in google_cloud_run_v2_service.cuff_sim.template[0].containers[0].env : e.name => e.value }["HTTPS_TIMEOUT_SECONDS"]) < tonumber({ for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name => e.value }["CUFF_FORWARD_TIMEOUT_SECONDS"]) &&
      8 * tonumber({ for e in google_cloud_run_v2_service.cuff_sim.template[0].containers[0].env : e.name => e.value }["HTTPS_TIMEOUT_SECONDS"]) < tonumber({ for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name => e.value }["CUFF_FORWARD_TIMEOUT_SECONDS"])
    )
    error_message = "cuff-sim's worst case (MLLP and HTTPS) must be below the proxy's cuff forward timeout."
  }
  assert {
    # The proxy refuses to start unless each budget is at least 10 s below REQUEST_TIMEOUT_SECONDS
    # (fhir_proxy/config.py, REQUEST_TIMEOUT_MARGIN_SECONDS).
    condition = (
      tonumber({ for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name => e.value }["CUFF_FORWARD_TIMEOUT_SECONDS"]) + 10 <= tonumber({ for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name => e.value }["REQUEST_TIMEOUT_SECONDS"]) &&
      tonumber({ for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name => e.value }["REQUEST_BUDGET_SECONDS"]) + 10 <= tonumber({ for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name => e.value }["REQUEST_TIMEOUT_SECONDS"])
    )
    error_message = "Both proxy budgets must be at least 10 s below REQUEST_TIMEOUT_SECONDS, or the proxy does not start."
  }
  assert {
    # Behind Hosting nothing waits longer than 60 s; a longer request timeout only lets a slow
    # body hold a thread longer.
    condition     = tonumber(trimsuffix(google_cloud_run_v2_service.fhir_proxy.template[0].timeout, "s")) <= 70
    error_message = "fhir-proxy's request timeout must stay at 70 s or less."
  }
  assert {
    condition     = output.timeouts.cuff_worst_case_mllp == 29 && output.timeouts.cuff_worst_case_https == 48 && output.timeouts.proxy_cuff_forward == 55 && output.timeouts.cuff_request == 60 && output.timeouts.proxy_request == 70
    error_message = "The documented numbers: worst cases 29 s / 48 s, forward 55 s, cuff-sim request timeout 60 s, proxy request timeout 70 s."
  }
}

# ---- network --------------------------------------------------------------------------------

run "direct_vpc_egress" {
  command = apply

  assert {
    condition = (
      google_cloud_run_v2_service.fhir_proxy.template[0].vpc_access[0].egress == "ALL_TRAFFIC" &&
      google_cloud_run_v2_service.fhir_proxy.template[0].vpc_access[0].network_interfaces[0].network == "pulsebridge-vpc" &&
      google_cloud_run_v2_service.fhir_proxy.template[0].vpc_access[0].network_interfaces[0].subnetwork == "pulsebridge-serverless" &&
      google_cloud_run_v2_service.fhir_proxy.template[0].vpc_access[0].connector == null
    )
    error_message = "fhir-proxy must use Direct VPC egress (no connector) on the serverless subnet with ALL_TRAFFIC, so its calls to internal-only services count as internal."
  }
  assert {
    condition = (
      google_cloud_run_v2_service.cuff_sim.template[0].vpc_access[0].egress == "PRIVATE_RANGES_ONLY" &&
      google_cloud_run_v2_service.cuff_sim.template[0].vpc_access[0].network_interfaces[0].network == "pulsebridge-vpc" &&
      google_cloud_run_v2_service.cuff_sim.template[0].vpc_access[0].network_interfaces[0].subnetwork == "pulsebridge-serverless" &&
      google_cloud_run_v2_service.cuff_sim.template[0].vpc_access[0].connector == null
    )
    error_message = "cuff-sim must use Direct VPC egress on the serverless subnet (PRIVATE_RANGES_ONLY: the VM is a private address)."
  }
}

# ---- images, accounts, probes ---------------------------------------------------------------

run "images_accounts_and_probes" {
  command = apply

  assert {
    condition = (
      google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].image == var.fhir_proxy_image &&
      google_cloud_run_v2_service.cuff_sim.template[0].containers[0].image == var.cuff_sim_image
    )
    error_message = "Each service must run its image by digest as given."
  }
  assert {
    condition = (
      google_cloud_run_v2_service.fhir_proxy.template[0].service_account == "pb-fhir-proxy@example-project.iam.gserviceaccount.com" &&
      google_cloud_run_v2_service.cuff_sim.template[0].service_account == "pb-cuff-sim@example-project.iam.gserviceaccount.com"
    )
    error_message = "Each service must run as its own account."
  }
  assert {
    condition = (
      length(google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].startup_probe[0].tcp_socket) == 1 &&
      length(google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].startup_probe[0].http_get) == 0 &&
      google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].startup_probe[0].tcp_socket[0].port == 8080
    )
    error_message = "fhir-proxy's startup probe must be TCP on 8080: every GET answers 401 without a token, which an HTTP probe counts as a failure."
  }
  assert {
    condition     = google_cloud_run_v2_service.cuff_sim.template[0].containers[0].startup_probe[0].http_get[0].path == "/cuff/samples" && google_cloud_run_v2_service.cuff_sim.template[0].containers[0].startup_probe[0].http_get[0].port == 8080
    error_message = "cuff-sim's startup probe must be HTTP GET /cuff/samples (answers 200 offline once the app has started)."
  }
  assert {
    # Cloud Run: timeout must not exceed the period; failureThreshold x periodSeconds <= 600.
    condition = alltrue([
      for p in [
        google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].startup_probe[0],
        google_cloud_run_v2_service.cuff_sim.template[0].containers[0].startup_probe[0],
      ] : p.timeout_seconds <= p.period_seconds && p.failure_threshold * p.period_seconds <= 600
    ])
    error_message = "Startup probes must satisfy Cloud Run's limits (timeout <= period, threshold x period <= 600 s)."
  }
  assert {
    condition     = google_cloud_run_v2_service.fhir_proxy.deletion_protection == false && google_cloud_run_v2_service.cuff_sim.deletion_protection == false
    error_message = "By default both services must be destroyable (deletion_protection = false)."
  }
}

# ---- secrets --------------------------------------------------------------------------------

run "session_signing_key_is_a_file_at_latest" {
  command = apply

  assert {
    condition = (
      length(google_cloud_run_v2_service.fhir_proxy.template[0].volumes) == 1 &&
      google_cloud_run_v2_service.fhir_proxy.template[0].volumes[0].name == "session-signing-key" &&
      google_cloud_run_v2_service.fhir_proxy.template[0].volumes[0].secret[0].secret == "pulsebridge-session-signing-key" &&
      google_cloud_run_v2_service.fhir_proxy.template[0].volumes[0].secret[0].items[0].version == "latest" &&
      google_cloud_run_v2_service.fhir_proxy.template[0].volumes[0].secret[0].items[0].path == "value"
    )
    error_message = "The session-signing-key secret must be a volume at version latest (a new key then takes effect without a redeploy)."
  }
  assert {
    condition = (
      length(google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].volume_mounts) == 1 &&
      google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].volume_mounts[0].name == "session-signing-key" &&
      google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].volume_mounts[0].mount_path == "/secrets/session-signing-key"
    )
    error_message = "The secret volume must be mounted at /secrets/session-signing-key."
  }
  assert {
    condition = alltrue([
      for e in concat(tolist(google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env), tolist(google_cloud_run_v2_service.cuff_sim.template[0].containers[0].env)) :
      length(e.value_source) == 0
    ])
    error_message = "No environment variable may take its value from a secret."
  }
  assert {
    condition     = length(google_cloud_run_v2_service.cuff_sim.template[0].volumes) == 0
    error_message = "cuff-sim reads no secret."
  }
}

# ---- the exact environments -----------------------------------------------------------------

run "fhir_proxy_environment" {
  command = apply

  # TRUSTED_PROXY_HOPS "2" is the module's default, measured behind Firebase Hosting (README,
  # "The client address and the direct run.app URL"); the service's own default is 1.

  assert {
    condition = { for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name => e.value } == {
      GCP_PROJECT_ID                = "example-project"
      HEALTHCARE_LOCATION           = "us-central1"
      HEALTHCARE_DATASET_ID         = "pulsebridge"
      FHIR_STORE_ID                 = "pulsebridge-fhir"
      AUDIT_FHIR_STORE_ID           = "pulsebridge-audit"
      HL7V2_STORE_ID                = "pulsebridge-hl7v2"
      PATIENT_ID                    = "PAT-0001"
      PATIENT_ASSIGNING_AUTHORITY   = "EXAMPLE"
      PATIENT_FAMILY_NAME           = "Example"
      PATIENT_GIVEN_NAME            = "Pat"
      SESSION_SIGNING_KEY_FILE      = "/secrets/session-signing-key/value"
      CUFF_SIM_URL                  = "https://pulsebridge-cuff-sim-abc123-uc.a.run.app"
      HEALTH_SYNC_URL               = "https://pulsebridge-health-sync-abc123-uc.a.run.app"
      PATH_PREFIX                   = "/api"
      PUBLIC_BASE_URL               = "https://example-project.web.app/api"
      ALLOWED_ORIGINS               = "https://example-project.web.app"
      TRUSTED_PROXY_HOPS            = "2"
      TOKEN_ISSUER                  = "pulsebridge-fhir-proxy:example-project:pulsebridge"
      REQUEST_BUDGET_SECONDS        = "50"
      CUFF_FORWARD_TIMEOUT_SECONDS  = "55"
      REQUEST_TIMEOUT_SECONDS       = "70"
      RECAPTCHA_SITE_KEY            = "synthetic-test-site-key-0000000000000000"
      RECAPTCHA_ACTION              = "view_demo"
      RECAPTCHA_MIN_SCORE           = "0.5"
      SESSION_TTL_SECONDS           = "86400"
      SESSIONS_PER_ADDRESS_PER_HOUR = "5"
      BOT_CHECKS_PER_DAY            = "300"
      DEMO_COUNTER_BUCKET           = "example-project-pulsebridge-demo-counters"
    }
    error_message = "fhir-proxy must get exactly the environment its README lists for T22, plus the demo settings of the T43 to T45 contract (D16)."
  }
}

run "cuff_sim_environment" {
  command = apply

  assert {
    condition = { for e in google_cloud_run_v2_service.cuff_sim.template[0].containers[0].env : e.name => e.value } == {
      GCP_PROJECT_ID              = "example-project"
      HEALTHCARE_LOCATION         = "us-central1"
      HEALTHCARE_DATASET_ID       = "pulsebridge"
      HL7V2_STORE_ID              = "pulsebridge-hl7v2"
      AUDIT_FHIR_STORE_ID         = "pulsebridge-audit"
      MLLP_HOST                   = "10.10.0.2"
      MLLP_PORT                   = "2575"
      MLLP_TIMEOUT_SECONDS        = "5"
      HTTPS_TIMEOUT_SECONDS       = "6"
      PATIENT_ID                  = "PAT-0001"
      PATIENT_ASSIGNING_AUTHORITY = "EXAMPLE"
      PATIENT_FAMILY_NAME         = "Example"
      PATIENT_GIVEN_NAME          = "Pat"
    }
    error_message = "cuff-sim must get exactly the environment its README lists, with HTTPS_TIMEOUT_SECONDS = 6 and no FHIR_STORE_ID (D15: no access to health data)."
  }
}

run "custom_origin_hops_and_dev_origin" {
  command = apply
  variables {
    public_origin         = "https://phr.example.org"
    extra_allowed_origins = ["http://localhost:5173"]
    trusted_proxy_hops    = 1 # not the default (2): a value that ignored the variable fails
  }

  assert {
    condition = (
      { for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name => e.value }["PUBLIC_BASE_URL"] == "https://phr.example.org/api" &&
      { for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name => e.value }["ALLOWED_ORIGINS"] == "https://phr.example.org,http://localhost:5173" &&
      { for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name => e.value }["TRUSTED_PROXY_HOPS"] == "1"
    )
    error_message = "public_origin, extra_allowed_origins and trusted_proxy_hops must reach the proxy."
  }
}

# ---- OpenEMR (T15) is optional ----------------------------------------------------------------

run "openemr_settings_when_given" {
  command = apply
  variables {
    openemr = {
      site_address     = "https://10.10.0.2"
      token_url        = "https://10.10.0.2/oauth2/default/token"
      fhir_base_url    = "https://10.10.0.2/apis/default/fhir"
      scopes           = "system/Patient.read system/Observation.write"
      client_secret_id = "pb-openemr-client-private-key"
    }
  }

  assert {
    condition = {
      for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name => e.value if startswith(e.name, "OPENEMR_")
      } == {
      OPENEMR_SITE_ADDRESS  = "https://10.10.0.2"
      OPENEMR_TOKEN_URL     = "https://10.10.0.2/oauth2/default/token"
      OPENEMR_FHIR_BASE_URL = "https://10.10.0.2/apis/default/fhir"
      OPENEMR_SCOPES        = "system/Patient.read system/Observation.write"
      OPENEMR_CLIENT_FILE   = "/secrets/openemr-client/value"
    }
    error_message = "With openemr given, the four settings and the client file path must be set."
  }
  assert {
    condition = (
      toset([for v in google_cloud_run_v2_service.fhir_proxy.template[0].volumes : "${v.name} ${v.secret[0].secret} ${v.secret[0].items[0].version}"]) ==
      toset(["session-signing-key pulsebridge-session-signing-key latest", "openemr-client pb-openemr-client-private-key latest"])
    )
    error_message = "With openemr given, its client secret must be a second volume at version latest."
  }
  assert {
    condition     = contains([for m in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].volume_mounts : m.mount_path], "/secrets/openemr-client")
    error_message = "The OpenEMR client secret must be mounted at /secrets/openemr-client."
  }
}

run "no_openemr_by_default" {
  command = apply

  assert {
    condition     = length([for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e if startswith(e.name, "OPENEMR_")]) == 0
    error_message = "Without openemr, no OPENEMR_* variable may be set."
  }
}

# ---- Hosting ----------------------------------------------------------------------------------

run "hosting_site_and_rewrite" {
  command = apply

  assert {
    condition     = google_firebase_hosting_site.dashboard.site_id == "example-project" && google_firebase_hosting_site.dashboard.project == "example-project"
    error_message = "The site defaults to the project's default site ID."
  }
  assert {
    condition     = length(google_firebase_project.this) == 0
    error_message = "By default Firebase is added to the project outside this module (manage_firebase_project = false)."
  }
  assert {
    condition = (
      jsondecode(output.firebase_json).hosting.site == "example-project" &&
      jsondecode(output.firebase_json).hosting.public == "dist" &&
      jsondecode(output.firebase_json).hosting.rewrites == [{ source = "/api/**", run = { serviceId = "pulsebridge-fhir-proxy", region = "us-central1", pinTag = false } }]
    )
    error_message = "firebase.json must rewrite /api/** to the fhir-proxy service in its region, and deploy web/dist to the site."
  }
  assert {
    condition     = contains(jsondecode(output.firebase_json).hosting.ignore, "**/.*") && contains(jsondecode(output.firebase_json).hosting.ignore, "**/.vite/**")
    error_message = "The ignore list must keep dot-directories (dist/.vite/manifest.json) off the site."
  }
  assert {
    condition = (
      [for h in jsondecode(output.firebase_json).hosting.headers : h if h.source == "**"][0].headers ==
      [for k in sort(keys(jsondecode(file("${path.module}/hosting/security-headers.json")))) : { key = k, value = jsondecode(file("${path.module}/hosting/security-headers.json"))[k] }]
    )
    error_message = "Every response must get the committed security headers."
  }
  assert {
    condition = (
      strcontains(output.content_security_policy, "default-src 'none'") &&
      strcontains(output.content_security_policy, "frame-ancestors 'none'") &&
      !strcontains(output.content_security_policy, "unsafe-inline") &&
      !strcontains(output.content_security_policy, "unsafe-eval") &&
      !strcontains(output.content_security_policy, "*")
    )
    error_message = "The CSP must deny by default, forbid framing and allow no unsafe-inline, unsafe-eval or wildcard."
  }
}

run "firebase_project_when_managed" {
  command = apply
  variables {
    manage_firebase_project = true
    hosting_site_id         = "pulsebridge-demo-site"
  }

  assert {
    condition     = length(google_firebase_project.this) == 1 && google_firebase_project.this[0].project == "example-project"
    error_message = "manage_firebase_project = true must add Firebase to the project."
  }
  assert {
    condition     = google_firebase_hosting_site.dashboard.site_id == "pulsebridge-demo-site" && jsondecode(output.firebase_json).hosting.site == "pulsebridge-demo-site" && output.public_origin == "https://pulsebridge-demo-site.web.app"
    error_message = "hosting_site_id must name the site, the deploy and the public origin."
  }
}
