# Module `identity` (task T04): who each PulseBridge workload is, and which secrets it may read.
#
# Design (see README.md, "Cross-module bindings"):
#   - This module creates every service account and every Secret Manager secret, and the IAM
#     bindings on resources it owns itself: the secrets, and its own service accounts.
#   - Bindings on resources owned by other modules (FHIR store, HL7v2 store, Cloud Run services)
#     are created by the owning module, which takes the emails from `service_account_emails`.
#     That keeps every binding next to the resource it protects, scoped to that one resource,
#     and keeps the module graph one-way: identity -> healthcare / ingestion / phr / edge-vm.
#   - The one project-level grant is Cloud Logging for the edge VM, because log writing has no
#     narrower resource to bind to.

locals {
  # One service account per workload. The key is the workload name used by the other modules
  # (module.identity.service_account_emails["fhir-proxy"]); the account ID is "<prefix>-<key>".
  workloads = merge(
    {
      "health-sync" = {
        display_name = "PulseBridge health-sync"
        description  = "Cloud Run service health-sync (T10): pulls Google Health heart-rate roll-ups and writes FHIR Observations."
      }
      "v2-to-fhir" = {
        display_name = "PulseBridge v2-to-fhir"
        description  = "Cloud Run service v2-to-fhir (T12): reads ingested HL7v2 messages and writes FHIR Observations."
      }
      "cuff-sim" = {
        display_name = "PulseBridge cuff simulator"
        description  = "Cloud Run service cuff-sim (T13): builds ORU^R01 messages and sends them by MLLP or HTTPS ingest."
      }
      "fhir-proxy" = {
        display_name = "PulseBridge fhir-proxy"
        description  = "Cloud Run service fhir-proxy (T14, T15): the public front door; reviewer login, FHIR reads, cuff forwarding, share to OpenEMR."
      }
      "edge-vm" = {
        display_name = "PulseBridge edge VM"
        description  = "Compute Engine edge VM (T21, T23): MLLP adapter, MariaDB, OpenEMR. Replaces the default Compute account, which has project-wide Editor."
      }
      "scheduler-invoker" = {
        display_name = "PulseBridge scheduler invoker"
        description  = "Identity in the OIDC token Cloud Scheduler sends to health-sync (T20). Can only invoke health-sync."
      }
      "pubsub-push" = {
        display_name = "PulseBridge Pub/Sub push"
        description  = "Identity in the OIDC token the Pub/Sub push subscription sends to v2-to-fhir (T05, T20). Can only invoke v2-to-fhir."
      }
    },
    var.create_seeder_account ? {
      "seeder" = {
        display_name = "PulseBridge synthetic-data seeder"
        description  = "Synthetic-data seeder (T09): writes synthetic heart-rate Observations when use_synthetic_data is on."
      }
    } : {}
  )

  # Project-level roles. Kept to the minimum: only roles with no narrower resource to bind to.
  # Cloud Run services need none here; Cloud Run collects their stdout logs itself.
  project_roles = {
    # The guest agent forwards the startup script's output (including the adapter crash-loop
    # check, T21) to Cloud Logging; writing log entries is a project-level permission.
    "edge-vm" = ["roles/logging.logWriter"]
  }

  project_role_bindings = {
    for pair in flatten([
      for workload, roles in local.project_roles : [
        for role in roles : { workload = workload, role = role }
      ]
    ]) : "${pair.workload}/${pair.role}" => pair
  }

  # Secret Manager secrets, created WITHOUT values. `readers` are the only workloads that get
  # roles/secretmanager.secretAccessor on that one secret. `written_by` records who adds the
  # versions (documentation only; none of them is a service account of this module).
  secrets = {
    # Decision D2: three Google Health values; health-sync needs exactly these three.
    "google-health-client-id" = {
      readers    = ["health-sync"]
      written_by = "scripts/connect-google-health (T11), run by the deployer"
      purpose    = "OAuth client ID of the deployer's own Google Health OAuth app."
    }
    "google-health-client-secret" = {
      readers    = ["health-sync"]
      written_by = "scripts/connect-google-health (T11), run by the deployer"
      purpose    = "OAuth client secret of the deployer's own Google Health OAuth app."
    }
    "google-health-refresh-token" = {
      readers    = ["health-sync"]
      written_by = "scripts/connect-google-health (T11), run by the deployer"
      purpose    = "OAuth refresh token for the deployer's Google Health account."
    }
    # Spike S1: the OpenEMR client authenticates with a signed JWT (private_key_jwt), so the secret
    # is the private key. The client ID is plain configuration, not a secret.
    "openemr-client-private-key" = {
      readers    = ["fhir-proxy"]
      written_by = "OpenEMR client registration script (T23), run by the deployer"
      purpose    = "Private key (PEM) that fhir-proxy signs its OpenEMR client assertion with."
    }
    # Decision D9: shared reviewer password, checked by fhir-proxy only.
    "reviewer-password" = {
      readers    = ["fhir-proxy"]
      written_by = "scripts/secrets/set-reviewer-password (T27), or in dev the optional write-only version below"
      purpose    = "Shared reviewer password for the dashboard login; fhir-proxy also derives its token-signing key from it."
    }
    # Spike S2: the edge VM's compose file needs three passwords; the startup script reads them.
    "db-root-password" = {
      readers    = ["edge-vm"]
      written_by = "scripts/secrets (T27), run by the deployer"
      purpose    = "MariaDB root password on the edge VM."
    }
    "openemr-db-password" = {
      readers    = ["edge-vm"]
      written_by = "scripts/secrets (T27), run by the deployer"
      purpose    = "Password of OpenEMR's own MariaDB user on the edge VM."
    }
    "openemr-admin-password" = {
      readers    = ["edge-vm"]
      written_by = "scripts/secrets (T27), run by the deployer"
      purpose    = "Password of the OpenEMR admin user created on first boot."
    }
  }

  # One accessor binding per (secret, reader) pair, keyed "<secret>/<workload>".
  secret_accessors = {
    for pair in flatten([
      for secret, cfg in local.secrets : [
        for reader in cfg.readers : { secret = secret, workload = reader }
      ]
    ]) : "${pair.secret}/${pair.workload}" => pair
  }
}
