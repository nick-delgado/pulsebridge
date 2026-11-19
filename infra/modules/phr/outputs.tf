# Outputs are this module's contract with the env roots (T25) and the page deploy (T26).

output "fhir_proxy_url" {
  description = "fhir-proxy's own run.app URL. The page does NOT use it: it calls /api on its own origin through the Hosting rewrite (one front door; a direct call would put a different X-Forwarded-For shape in front of the per-address session limit)."
  value       = google_cloud_run_v2_service.fhir_proxy.uri
}

output "fhir_proxy_service_name" {
  description = "Name of the fhir-proxy Cloud Run service (the rewrite's serviceId)."
  value       = google_cloud_run_v2_service.fhir_proxy.name
}

output "cuff_sim_url" {
  description = "cuff-sim's run.app URL (internal only; fhir-proxy's CUFF_SIM_URL and identity-token audience)."
  value       = google_cloud_run_v2_service.cuff_sim.uri
}

output "cuff_sim_service_name" {
  description = "Name of the cuff-sim Cloud Run service."
  value       = google_cloud_run_v2_service.cuff_sim.name
}

output "region" {
  description = "Region of both services (and the rewrite's region)."
  value       = var.region
}

output "hosting_site_id" {
  description = "Firebase Hosting site ID; the deploy step's firebase.json names it."
  value       = google_firebase_hosting_site.dashboard.site_id
}

output "hosting_default_url" {
  description = "The site's default URL, https://<site_id>.web.app."
  value       = google_firebase_hosting_site.dashboard.default_url
}

output "public_origin" {
  description = "The origin reviewers open (public_origin, or the site's web.app origin). The page calls <public_origin>/api/..."
  value       = local.public_origin
}

output "allowed_origins" {
  description = "fhir-proxy's ALLOWED_ORIGINS: public_origin, https://<site>.web.app and https://<site>.firebaseapp.com (the page's origins, also the reCAPTCHA key's domains), then extra_allowed_origins; no duplicates."
  value       = local.allowed_origins
}

output "public_base_url" {
  description = "fhir-proxy's PUBLIC_BASE_URL: public_origin plus /api."
  value       = "${local.public_origin}${local.path_prefix}"
}

output "firebase_json" {
  description = "The firebase.json the page deploy uses (README, \"Deploying the page\"): write it to web/firebase.json, build web/dist, then `firebase deploy --only hosting`. It holds the site, the /api/** rewrite to fhir-proxy, the security headers (with the Content-Security-Policy) and the ignore list. No secret is in it."
  value       = jsonencode({ hosting = local.firebase_hosting })
}

output "content_security_policy" {
  description = "The Content-Security-Policy header the site sends."
  value       = local.security_headers["Content-Security-Policy"]
}

output "timeouts" {
  description = "Every timeout in the request path, in seconds (README, \"Timeouts end to end\"), and cuff-sim's documented worst cases."
  value       = merge(local.timeouts, { cuff_worst_case_mllp = local.cuff_worst_case.mllp, cuff_worst_case_https = local.cuff_worst_case.https })
}

output "cuff_sim_rate_limit_per_minute" {
  description = "The effective service-wide cuff send limit: cuff-sim's per-instance 6 a minute times cuff_sim_max_instances."
  value       = 6 * var.cuff_sim_max_instances
}

output "recaptcha_site_key" {
  description = "The reCAPTCHA score key's ID (D16): fhir-proxy's RECAPTCHA_SITE_KEY, which the page fetches from /api/auth/config. Public by design (every visitor's browser sees it); not a secret."
  value       = google_recaptcha_enterprise_key.demo.name
}

output "recaptcha_allowed_domains" {
  description = "The domains the reCAPTCHA key accepts tokens from: <site>.web.app, <site>.firebaseapp.com and public_origin's host when set."
  value       = local.recaptcha_allowed_domains
}

output "demo_counter_bucket" {
  description = "Name of the bucket that holds the daily bot-check counters (fhir-proxy's DEMO_COUNTER_BUCKET)."
  value       = google_storage_bucket.demo_counters.name
}

output "demo_settings" {
  description = "The public demo's settings as fhir-proxy gets them (D16): RECAPTCHA_MIN_SCORE, SESSION_TTL_SECONDS, SESSIONS_PER_ADDRESS_PER_HOUR and BOT_CHECKS_PER_DAY."
  value = {
    recaptcha_min_score           = var.recaptcha_min_score
    session_ttl_seconds           = var.session_ttl_seconds
    sessions_per_address_per_hour = var.sessions_per_address_per_hour
    bot_checks_per_day            = var.bot_checks_per_day
  }
}
