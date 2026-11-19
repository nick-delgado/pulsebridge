# THIS FILE IS IDENTICAL IN infra/envs/dev AND infra/envs/prod: both roots call the same stack
# composition with their own variables, plus the per-environment settings from env.tf. See
# infra/envs/README.md ("Why one composition") and infra/envs/_stack/main.tf for the wiring.

module "stack" {
  source = "../_stack"

  # Per-environment settings (env.tf).
  environment          = local.environment
  protect_from_destroy = local.protect_from_destroy

  project_id           = var.project_id
  region               = var.region
  zone                 = var.zone
  name_prefix          = var.name_prefix
  identity_name_prefix = var.identity_name_prefix
  labels               = var.labels

  use_synthetic_data = var.use_synthetic_data
  ingest_enabled     = var.ingest_enabled

  v2_to_fhir_image  = var.v2_to_fhir_image
  fhir_proxy_image  = var.fhir_proxy_image
  cuff_sim_image    = var.cuff_sim_image
  health_sync_image = var.health_sync_image
  seed_image        = var.seed_image

  patient_id                  = var.patient_id
  patient_assigning_authority = var.patient_assigning_authority
  patient_family_name         = var.patient_family_name
  patient_given_name          = var.patient_given_name
  time_zone                   = var.time_zone

  alert_email                 = var.alert_email
  alert_notification_channels = var.alert_notification_channels

  mllp_port          = var.mllp_port
  openemr_https_port = var.openemr_https_port
  enable_iap_ssh     = var.enable_iap_ssh
  enable_cloud_nat   = var.enable_cloud_nat
  openemr_scopes     = var.openemr_scopes

  trusted_proxy_hops    = var.trusted_proxy_hops
  public_origin         = var.public_origin
  hosting_site_id       = var.hosting_site_id
  extra_allowed_origins = var.extra_allowed_origins

  page_deployer_service_account_email = var.page_deployer_service_account_email

  v2_to_fhir_ingress  = var.v2_to_fhir_ingress
  health_sync_ingress = var.health_sync_ingress

  create_build_service_account    = var.create_build_service_account
  build_trigger_repository        = var.build_trigger_repository
  build_trigger_approval_required = var.build_trigger_approval_required
}
