# The whole PulseBridge stack: seven modules, wired exactly as each module's README says ("For
# T25" / "Usage"). Both environment roots call this one composition, so dev and prod cannot wire
# the modules differently; they differ only in the values they pass (infra/envs/README.md,
# "dev and prod").
#
# Dependency graph (Terraform builds it per resource and per module output, not per module, so
# the arrows that look like cycles are not):
#
#   identity ──emails──> project-base, healthcare, ingestion, edge-vm, phr
#   project-base ──network, subnet, mirror, ports──> edge-vm, openemr, phr
#   healthcare ──store names──> ingestion, edge-vm, phr
#   ingestion ──v2-to-fhir push endpoint, ingest_enabled──> healthcare (only its subscription)
#   edge-vm reserved ADDRESS ──internal_ip──> openemr ──hooks──> edge-vm INSTANCE metadata
#   edge-vm ──mllp_host──> phr;  ingestion ──health_sync_url──> phr

locals {
  labels = merge(var.labels, {
    app         = "pulsebridge"
    environment = var.environment
  })

  # ONE patient for the whole stack. Both modules that need it read these two objects, and
  # checks.tf compares them: if a later edit wired one module to a different value, the cuff
  # path would break silently (v2-to-fhir refuses a PID-3 that is not its patient).
  ingestion_patient = {
    id                  = var.patient_id
    assigning_authority = var.patient_assigning_authority
  }
  phr_patient = {
    id                  = var.patient_id
    assigning_authority = var.patient_assigning_authority
  }

}

# ---------------------------------------------------------------------------------------------
# identity: service accounts and empty secrets. Depends on nothing (identity README).
# ---------------------------------------------------------------------------------------------

module "identity" {
  source = "../../modules/identity"

  project_id  = var.project_id
  name_prefix = var.identity_name_prefix

  # A deployment with real watch data has no spare identity that can write to the FHIR store.
  create_seeder_account = var.use_synthetic_data

  # The OpenEMR registration hook runs on the VM and uploads the client key (openemr README).
  edge_vm_writes_openemr_key = true

  # T26: the image-build account (log writer only here; its repository grants are project-base's).
  create_build_account = var.create_build_service_account

  secret_deletion_protection = var.protect_from_destroy
  secret_labels              = local.labels

  # Dev only (validated in variables.tf): write-only, never in plan or state.
  reviewer_password            = var.reviewer_password
  reviewer_password_wo_version = var.reviewer_password_wo_version
}

# ---------------------------------------------------------------------------------------------
# project-base: APIs, VPC, subnets, firewall, Artifact Registry and the Docker Hub mirror.
# ---------------------------------------------------------------------------------------------

module "project_base" {
  source = "../../modules/project-base"

  project_id  = var.project_id
  region      = var.region
  name_prefix = var.name_prefix
  labels      = local.labels

  # Required for the edge VM: the firewall rules target this account (stronger than a tag), and
  # it gets roles/artifactregistry.reader on the Docker Hub mirror, without which the VM cannot
  # pull MariaDB or OpenEMR (project-base and edge-vm READMEs).
  edge_vm_service_account_email = module.identity.service_account_emails["edge-vm"]
  # A plain bool decides whether the mirror grant exists, so its count is known on the first plan
  # even though the email is not (it is unknown until module identity creates the account).
  grant_edge_vm_mirror_reader = true

  # The VM has no internet route; MariaDB and OpenEMR come through this pull-through cache.
  # edge-vm and openemr need a non-null mirror URL, so this is not a variable.
  create_dockerhub_mirror = true

  mllp_port          = var.mllp_port
  openemr_https_port = var.openemr_https_port
  enable_iap_ssh     = var.enable_iap_ssh
  enable_cloud_nat   = var.enable_cloud_nat

  # T26: the build account pushes to the services repository and pulls through the mirror; the
  # optional trigger runs cloudbuild.yaml as it on release tags. As with the edge VM, a plain bool
  # decides the grants, because the email is unknown until module identity creates the account.
  build_service_account_email         = lookup(module.identity.service_account_emails, "build", null)
  grant_build_account_registry_access = var.create_build_service_account
  build_trigger_repository            = var.build_trigger_repository
}

# ---------------------------------------------------------------------------------------------
# healthcare: dataset, FHIR store, audit FHIR store, HL7v2 store, ingest topic and subscription.
# ---------------------------------------------------------------------------------------------

module "healthcare" {
  source = "../../modules/healthcare"

  # The same project as module ingestion: Pub/Sub push counts as internal traffic only from the
  # same project, and v2-to-fhir is internal-only (ingestion README, "Ingress").
  project_id  = var.project_id
  location    = var.region
  name_prefix = var.name_prefix
  labels      = local.labels

  # Real data must survive a stray destroy in prod; dev is destroyed nightly.
  deletion_policy = var.protect_from_destroy ? "PREVENT" : "DELETE"

  # One switch (ingest_enabled) turns on both the Scheduler job in ingestion and push here. The
  # endpoint output waits for v2-to-fhir's invoker grant, so push never starts before its
  # permission exists (ingestion README, "For T25").
  push_enabled               = module.ingestion.ingest_enabled
  push_endpoint              = module.ingestion.v2_to_fhir_push_endpoint
  push_service_account_email = module.identity.service_account_emails["pubsub-push"]

  # Module identity's map as it is; healthcare uses the keys it knows.
  store_iam_service_accounts = module.identity.service_account_emails

  # The Healthcare and Pub/Sub APIs are enabled by project-base; on a fresh project the dataset
  # would otherwise race the API enablement.
  depends_on = [module.project_base]
}

# ---------------------------------------------------------------------------------------------
# ingestion: v2-to-fhir always; health-sync (real data) or the seeder job (synthetic); Scheduler
# jobs only with ingest_enabled; log-based alerts.
# ---------------------------------------------------------------------------------------------

module "ingestion" {
  source = "../../modules/ingestion"

  project_id  = var.project_id
  region      = var.region
  name_prefix = var.name_prefix
  labels      = local.labels

  use_synthetic_data = var.use_synthetic_data
  ingest_enabled     = var.ingest_enabled

  # By digest, as the image build prints them (services/README.md, "Container images").
  v2_to_fhir_image  = var.v2_to_fhir_image
  health_sync_image = var.health_sync_image
  seed_image        = var.seed_image

  healthcare_project_id = module.healthcare.project_id
  healthcare_location   = module.healthcare.location
  healthcare_dataset_id = module.healthcare.dataset_name
  fhir_store_id         = module.healthcare.fhir_store_name
  hl7v2_store_id        = module.healthcare.hl7v2_store_name

  patient_id                  = local.ingestion_patient.id
  patient_assigning_authority = local.ingestion_patient.assigning_authority
  time_zone                   = var.time_zone

  service_account_emails = module.identity.service_account_emails
  secret_ids             = module.identity.secret_ids

  # At least one is required at the root, so a failed sync and "reconnect needed" reach someone.
  # alert_email is sensitive (T26: CI posts plans on pull requests), so whether the channel exists
  # is a separate bool. "Is an address set?" reveals nothing about the address, so it is unwrapped
  # with nonsensitive(); a sensitive value is refused in the module's count.
  alert_email                = var.alert_email
  create_alert_email_channel = nonsensitive(var.alert_email != null)
  notification_channels      = var.alert_notification_channels

  v2_to_fhir_ingress  = var.v2_to_fhir_ingress
  health_sync_ingress = var.health_sync_ingress

  deletion_protection = var.protect_from_destroy

  # health-sync mounts its secrets: the accessor grants (module identity) must exist before it
  # deploys. The Cloud Run, Scheduler and Monitoring APIs come from project-base. Neither module
  # depends on ingestion, so there is no cycle.
  depends_on = [module.identity, module.project_base]
}

# ---------------------------------------------------------------------------------------------
# edge-vm: the e2-micro with the MLLP adapter and MariaDB, plus OpenEMR's boot hooks.
# ---------------------------------------------------------------------------------------------

module "edge_vm" {
  source = "../../modules/edge-vm"

  project_id  = var.project_id
  zone        = var.zone
  name_prefix = var.name_prefix # equal to project-base's (edge-vm README)
  labels      = local.labels

  edge_subnet_id        = module.project_base.edge_subnet_id
  network_tag           = module.project_base.edge_vm_network_tag
  mllp_port             = module.project_base.mllp_port # the port project-base's firewall opens
  dockerhub_mirror_url  = module.project_base.dockerhub_mirror_url
  service_account_email = module.identity.service_account_emails["edge-vm"]
  hl7v2_store_flags     = module.healthcare.mllp_adapter_store_flags

  # MariaDB's root password (edge-vm) plus OpenEMR's two passwords (openemr). Secret IDs only:
  # the VM reads the values itself at boot.
  secret_files = merge(
    { "mariadb-root-password" = module.identity.secret_resource_ids["db-root-password"] },
    module.openemr.edge_vm_secret_files,
  )

  # OpenEMR's container and OAuth client registration, and the 40 minutes its first boot needs
  # (edge-vm's default 30 is too short).
  hooks                = module.openemr.edge_vm_hooks
  hook_timeout_minutes = module.openemr.hook_timeout_minutes

  # prod: a daily snapshot of the OpenEMR database, kept 7 days (kept snapshots outlive a destroy
  # and are billed until deleted; infra/envs/README.md, "What destroy leaves behind").
  data_disk_snapshot_days = var.protect_from_destroy ? 7 : 0
}

# ---------------------------------------------------------------------------------------------
# openemr: no resources; renders the hooks and the connection settings.
# ---------------------------------------------------------------------------------------------

module "openemr" {
  source = "../../modules/openemr"

  # From edge-vm's reserved ADDRESS resource, not the instance, so there is no cycle with the
  # hooks, which go into the instance's metadata (openemr README, "What this module is").
  internal_ip          = module.edge_vm.internal_ip
  https_port           = module.project_base.openemr_https_port
  dockerhub_mirror_url = module.project_base.dockerhub_mirror_url

  secret_ids = {
    db_password        = module.identity.secret_resource_ids["openemr-db-password"]
    admin_password     = module.identity.secret_resource_ids["openemr-admin-password"]
    client_private_key = module.identity.secret_resource_ids["openemr-client-private-key"]
  }

  # null keeps the module's default, spike S1's six SMART v1 scopes (its scopes variable is not
  # nullable); the narrower v2 set is a first-boot experiment (openemr README, "Scopes").
  scopes = var.openemr_scopes
}

# ---------------------------------------------------------------------------------------------
# phr: the Hosting site, fhir-proxy (the one public service) and cuff-sim.
# ---------------------------------------------------------------------------------------------

module "phr" {
  source = "../../modules/phr"

  project_id  = var.project_id
  region      = var.region # Hosting can rewrite only to some regions; phr validates it
  name_prefix = var.name_prefix
  labels      = local.labels

  use_synthetic_data = var.use_synthetic_data

  fhir_proxy_image = var.fhir_proxy_image
  cuff_sim_image   = var.cuff_sim_image

  healthcare_project_id = module.healthcare.project_id
  healthcare_location   = module.healthcare.location
  healthcare_dataset_id = module.healthcare.dataset_name
  fhir_store_id         = module.healthcare.fhir_store_name
  audit_fhir_store_id   = module.healthcare.audit_fhir_store_name
  hl7v2_store_id        = module.healthcare.hl7v2_store_name

  patient_id                  = local.phr_patient.id
  patient_assigning_authority = local.phr_patient.assigning_authority
  # With synthetic data phr sends the seeder's synthetic name (Synthetic / Demo) and refuses any
  # other; passing null makes that the only possibility (the root refuses a name with synthetic
  # data rather than ignoring it).
  patient_family_name = var.use_synthetic_data ? null : var.patient_family_name
  patient_given_name  = var.use_synthetic_data ? null : var.patient_given_name

  service_account_emails = module.identity.service_account_emails
  secret_ids             = module.identity.secret_ids

  network_name           = module.project_base.network_name
  serverless_subnet_name = module.project_base.serverless_subnet_name
  mllp_host              = module.edge_vm.mllp_host
  mllp_port              = module.edge_vm.mllp_port

  health_sync_url = module.ingestion.health_sync_url # null with synthetic data

  openemr = {
    site_address  = module.openemr.site_address
    token_url     = module.openemr.token_url
    fhir_base_url = module.openemr.fhir_base_url
    scopes        = module.openemr.scopes
    # The short secret ID phr mounts (client_secret_id is the full resource path).
    client_secret_id = module.openemr.client_secret_name
  }

  trusted_proxy_hops    = var.trusted_proxy_hops
  public_origin         = var.public_origin
  hosting_site_id       = var.hosting_site_id
  extra_allowed_origins = var.extra_allowed_origins

  # bootstrap added Firebase with the owner's credentials (add_firebase); the deployer holds only
  # roles/firebasehosting.admin.
  manage_firebase_project = false

  deletion_protection = var.protect_from_destroy

  # The APIs (Cloud Run, Firebase Hosting) and the secret accessor grants must exist first:
  # Cloud Run checks at deploy time that the account may read every mounted secret.
  depends_on = [module.project_base, module.identity]
}
