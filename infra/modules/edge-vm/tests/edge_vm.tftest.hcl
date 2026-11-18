# Behaviour tests for edge-vm. They run with `terraform test` from the module directory.
#
# mock_provider replaces the real google provider: no credentials are read and no API is called.
# Every run uses `command = plan`, so nothing is created even in the mock. (`make tf-test` also
# removes Google credentials from the environment, because a mock is not a guarantee.)
#
# The override_resource blocks give the data disk and the address known values at plan time, so
# the tests can check that the instance is wired to THIS disk and THIS address.

mock_provider "google" {}

override_resource {
  target          = google_compute_disk.data
  override_during = plan
  values = {
    id        = "projects/example-project-123/zones/us-central1-a/disks/pulsebridge-edge-data"
    self_link = "https://www.googleapis.com/compute/v1/projects/example-project-123/zones/us-central1-a/disks/pulsebridge-edge-data"
  }
}

override_resource {
  target          = google_compute_address.internal
  override_during = plan
  values = {
    address = "10.10.0.10"
  }
}

variables {
  # Synthetic values only: no real project, account or store is involved.
  project_id            = "example-project-123"
  zone                  = "us-central1-a"
  edge_subnet_id        = "projects/example-project-123/regions/us-central1/subnetworks/pulsebridge-edge"
  network_tag           = "pulsebridge-edge-vm"
  service_account_email = "pulsebridge-edge-vm@example-project-123.iam.gserviceaccount.com"
  dockerhub_mirror_url  = "us-central1-docker.pkg.dev/example-project-123/pulsebridge-dockerhub"
  hl7v2_store_flags = {
    hl7_v2_project_id  = "example-project-123"
    hl7_v2_location_id = "us-central1"
    hl7_v2_dataset_id  = "example-dataset"
    hl7_v2_store_id    = "example-hl7v2-store"
  }
  secret_files = {
    "mariadb-root-password" = "projects/example-project-123/secrets/pulsebridge-db-root-password"
  }
}

# --- Network exposure -----------------------------------------------------------------------------

run "no_external_ip_and_private_address" {
  command = plan

  assert {
    # An access_config is what gives an instance an external IP (D13, D14).
    condition     = length(google_compute_instance.this.network_interface[0].access_config) == 0
    error_message = "The edge VM must have no access_config, i.e. no external IP."
  }

  assert {
    condition     = length(google_compute_instance.this.network_interface[0].ipv6_access_config) == 0
    error_message = "The edge VM must have no external IPv6 access either."
  }

  assert {
    condition     = length(google_compute_instance.this.network_interface) == 1
    error_message = "The edge VM must have exactly one network interface."
  }

  assert {
    condition     = google_compute_instance.this.network_interface[0].subnetwork == var.edge_subnet_id
    error_message = "The VM must sit on the edge subnet passed in."
  }

  assert {
    condition     = google_compute_instance.this.network_interface[0].network_ip == "10.10.0.10"
    error_message = "The VM must use the reserved internal address."
  }

  assert {
    condition     = google_compute_address.internal.address_type == "INTERNAL" && google_compute_address.internal.subnetwork == var.edge_subnet_id
    error_message = "The reserved address must be INTERNAL and in the edge subnet."
  }

  assert {
    condition     = output.mllp_host == "10.10.0.10" && output.internal_ip == "10.10.0.10" && output.mllp_port == 2575
    error_message = "mllp_host/internal_ip must be the reserved address and mllp_port 2575 (T22's MLLP_HOST)."
  }

  assert {
    condition     = google_compute_instance.this.tags == toset(["pulsebridge-edge-vm"])
    error_message = "The VM must carry exactly the project-base network tag."
  }
}

# --- Machine and identity --------------------------------------------------------------------------

run "machine_type_defaults_to_free_tier" {
  command = plan

  assert {
    condition     = google_compute_instance.this.machine_type == "e2-micro"
    error_message = "machine_type must default to e2-micro (free tier)."
  }

  assert {
    condition     = google_compute_instance.this.scheduling[0].preemptible == false && google_compute_instance.this.scheduling[0].automatic_restart == true
    error_message = "The VM must be non-preemptible (free tier) and restart automatically."
  }
}

run "machine_type_is_a_variable" {
  command = plan

  variables {
    machine_type = "e2-small"
  }

  assert {
    condition     = google_compute_instance.this.machine_type == "e2-small"
    error_message = "machine_type must follow the variable."
  }
}

run "dedicated_service_account_with_cloud_platform_scope_only" {
  command = plan

  assert {
    condition     = google_compute_instance.this.service_account[0].email == "pulsebridge-edge-vm@example-project-123.iam.gserviceaccount.com"
    error_message = "The VM must run as the dedicated account passed in."
  }

  assert {
    condition     = !endswith(google_compute_instance.this.service_account[0].email, "-compute@developer.gserviceaccount.com")
    error_message = "The VM must not run as the Compute Engine default account."
  }

  assert {
    condition     = google_compute_instance.this.service_account[0].scopes == toset(["https://www.googleapis.com/auth/cloud-platform"])
    error_message = "Scopes must be exactly cloud-platform (required by Secret Manager); IAM narrows the account."
  }

  # The mirror read grant moved to project-base, which owns the repository: this module creates
  # no IAM resource at all (tests/static_checks.sh also checks that).
}

run "shielded_vm" {
  command = plan

  assert {
    condition = (
      google_compute_instance.this.shielded_instance_config[0].enable_secure_boot &&
      google_compute_instance.this.shielded_instance_config[0].enable_vtpm &&
      google_compute_instance.this.shielded_instance_config[0].enable_integrity_monitoring
    )
    error_message = "Secure Boot, vTPM and integrity monitoring must all be on."
  }

  assert {
    condition     = google_compute_instance.this.allow_stopping_for_update == true && google_compute_instance.this.deletion_protection == false
    error_message = "allow_stopping_for_update must be on and deletion protection off (destroy must work)."
  }

  assert {
    condition     = startswith(google_compute_instance.this.boot_disk[0].initialize_params[0].image, "projects/cos-cloud/")
    error_message = "The boot image must be Container-Optimized OS."
  }
}

# --- Disks -------------------------------------------------------------------------------------------

run "separate_data_disk_attached_by_device_name" {
  command = plan

  assert {
    condition     = length(google_compute_instance.this.attached_disk) == 1
    error_message = "Exactly one data disk must be attached."
  }

  assert {
    condition     = google_compute_instance.this.attached_disk[0].source == "https://www.googleapis.com/compute/v1/projects/example-project-123/zones/us-central1-a/disks/pulsebridge-edge-data"
    error_message = "The attached disk must be the module's own google_compute_disk.data."
  }

  assert {
    condition     = google_compute_instance.this.attached_disk[0].device_name == "edge-data" && google_compute_instance.this.attached_disk[0].mode == "READ_WRITE"
    error_message = "The data disk must be attached read-write as device edge-data (stable /dev/disk/by-id/google-edge-data)."
  }

  assert {
    condition     = strcontains(google_compute_instance.this.metadata["startup-script"], "EDGE_DATA_DEVICE='/dev/disk/by-id/google-edge-data'")
    error_message = "The startup script must mount the disk by the same stable device name."
  }

  assert {
    condition     = google_compute_disk.data.zone == "us-central1-a" && google_compute_disk.data.type == "pd-standard" && google_compute_disk.data.size == 10
    error_message = "The data disk must be a 10 GB pd-standard disk in the VM's zone by default."
  }

  assert {
    condition     = google_compute_instance.this.boot_disk[0].initialize_params[0].size + google_compute_disk.data.size <= 30
    error_message = "Default boot plus data disk must fit the free tier's 30 GB of standard persistent disk."
  }

  assert {
    condition     = google_compute_instance.this.boot_disk[0].auto_delete == true
    error_message = "The boot disk is disposable and must be deleted with the VM."
  }

  assert {
    condition     = output.data_mount_dir == "/mnt/disks/edge" && output.mariadb_data_dir == "/mnt/disks/edge/mariadb" && output.openemr_data_dir == "/mnt/disks/edge/openemr" && output.secrets_dir == "/mnt/disks/edge/secrets"
    error_message = "The data paths T23 relies on must be under the data disk mount."
  }
}

# --- Metadata: no SSH keys, no secrets ---------------------------------------------------------------

run "metadata_has_no_ssh_keys_or_secrets" {
  command = plan

  assert {
    condition     = length([for k in keys(google_compute_instance.this.metadata) : k if can(regex("(?i)(ssh-?keys|sshkeys|password|passwd|secret|token|credential|private-?key|api-?key)", replace(k, "block-project-ssh-keys", "")))]) == 0
    error_message = "No metadata key may hold SSH keys, a password, a token, a credential or a key."
  }

  assert {
    condition     = google_compute_instance.this.metadata["enable-oslogin"] == "TRUE" && google_compute_instance.this.metadata["block-project-ssh-keys"] == "TRUE"
    error_message = "OS Login must be on and project-wide SSH keys blocked."
  }

  assert {
    condition     = toset(keys(google_compute_instance.this.metadata)) == toset(["startup-script", "enable-oslogin", "block-project-ssh-keys", "google-logging-enabled", "cos-update-strategy"])
    error_message = "With no hooks, metadata must hold exactly the five expected keys."
  }

  assert {
    # Set explicitly: COS in-place updates are not supported with Secure Boot, and the default
    # changed at milestone 117; patching is by replacing the VM.
    condition     = google_compute_instance.this.metadata["cos-update-strategy"] == "update_disabled"
    error_message = "cos-update-strategy must be set explicitly to update_disabled."
  }

  assert {
    # MariaDB gets its password as a FILE; the variable that takes the value itself
    # (MARIADB_ROOT_PASSWORD=..., which would put it in `docker inspect`) is never used.
    condition = (
      !strcontains(google_compute_instance.this.metadata["startup-script"], "MARIADB_ROOT_PASSWORD=") &&
      strcontains(google_compute_instance.this.metadata["startup-script"], "--env MARIADB_ROOT_PASSWORD_FILE=/run/secrets/mariadb-root-password")
    )
    error_message = "MariaDB must read its root password from a file, never from an environment value."
  }

  assert {
    condition     = strcontains(google_compute_instance.this.metadata["startup-script"], "'mariadb-root-password=projects/example-project-123/secrets/pulsebridge-db-root-password'")
    error_message = "The script must name the Secret Manager secret to read at boot."
  }
}

# --- The MLLP adapter --------------------------------------------------------------------------------

run "adapter_command_is_s4_flags_with_error_logging_off" {
  command = plan

  assert {
    condition = output.mllp_adapter_command == [
      "/usr/mllp_adapter/mllp_adapter",
      "--port=2575",
      "--receiver_ip=0.0.0.0",
      "--hl7_v2_project_id=example-project-123",
      "--hl7_v2_location_id=us-central1",
      "--hl7_v2_dataset_id=example-dataset",
      "--hl7_v2_store_id=example-hl7v2-store",
      "--log_error_msg=false",
      "--export_stats=false",
    ]
    error_message = "The adapter command must be the binary path and exactly the flags spike S4 recorded."
  }

  assert {
    # The rendered script passes the same list, one quoted word per line, in this order.
    condition     = strcontains(google_compute_instance.this.metadata["startup-script"], "EDGE_MLLP_ADAPTER_COMMAND=(\n  '/usr/mllp_adapter/mllp_adapter'\n  '--port=2575'\n  '--receiver_ip=0.0.0.0'\n  '--hl7_v2_project_id=example-project-123'\n  '--hl7_v2_location_id=us-central1'\n  '--hl7_v2_dataset_id=example-dataset'\n  '--hl7_v2_store_id=example-hl7v2-store'\n  '--log_error_msg=false'\n  '--export_stats=false'\n)")
    error_message = "The startup script must carry the adapter command exactly."
  }

  assert {
    condition     = !can(regex("--log_ack|--log_nacked_msg|--log_input_msg_in_base64|--logtostderr|--hl7_v2_hl7_v2_store_id|--credentials", google_compute_instance.this.metadata["startup-script"]))
    error_message = "Content-logging flags, the flags that crash-looped in S4 and --credentials must not be passed."
  }

  assert {
    condition     = strcontains(google_compute_instance.this.metadata["startup-script"], "EDGE_MLLP_ADAPTER_IMAGE='gcr.io/cloud-healthcare-containers/mllp-adapter@sha256:92eaa0fc5fcd0eb7ff62bd28c9140dcd7582bfec20790e5b203ca8dc75c4a90f'")
    error_message = "The adapter image must be pinned by the S4 digest by default."
  }

  assert {
    condition     = strcontains(google_compute_instance.this.metadata["startup-script"], "--restart unless-stopped")
    error_message = "The containers must use --restart unless-stopped."
  }

  assert {
    condition     = strcontains(google_compute_instance.this.metadata["startup-script"], "EDGE_MARIADB_IMAGE='us-central1-docker.pkg.dev/example-project-123/pulsebridge-dockerhub/library/mariadb:11.8@sha256:d36f18d65a0459510b8b0f643b226a6d299e3206486e2b64ff20754a6d291707'")
    error_message = "MariaDB must come through the Docker Hub mirror, pinned by digest."
  }

  assert {
    condition     = strcontains(google_compute_instance.this.metadata["startup-script"], "EDGE_MIRROR_HOST='us-central1-docker.pkg.dev'")
    error_message = "docker-credential-gcr must be configured for the mirror's host."
  }

  assert {
    condition     = strcontains(google_compute_instance.this.metadata["startup-script"], "'--innodb-buffer-pool-size=64M'") && strcontains(google_compute_instance.this.metadata["startup-script"], "'--max-connections=30'") && strcontains(google_compute_instance.this.metadata["startup-script"], "'--performance-schema=OFF'")
    error_message = "MariaDB must get spike S2's low-memory settings."
  }

  assert {
    condition     = startswith(google_compute_instance.this.metadata["startup-script"], "#!/bin/bash\n") && strcontains(google_compute_instance.this.metadata["startup-script"], "\nmain \"$@\"\n")
    error_message = "The startup script must be the rendered configuration followed by scripts/edge-startup.sh."
  }

  assert {
    condition     = strcontains(google_compute_instance.this.metadata["startup-script"], "EDGE_BOOT_WAIT_SECONDS='900'") && strcontains(google_compute_instance.this.metadata["startup-script"], "EDGE_SWAP_SIZE_MB='2048'")
    error_message = "Waits must default to 15 minutes and swap to 2 GB."
  }
}

# --- MariaDB's first initialisation (T42) -------------------------------------------------------------

run "mariadb_entrypoint_wrapper_in_the_script" {
  command = plan

  assert {
    # The template's here-document must carry the wrapper file verbatim; tests/startup_script.sh
    # writes the same block and checks the container gets exactly the file's text.
    condition     = strcontains(google_compute_instance.this.metadata["startup-script"], "EDGE_MARIADB_ENTRYPOINT_WRAPPER=''\nIFS= read -r -d '' EDGE_MARIADB_ENTRYPOINT_WRAPPER << 'PULSEBRIDGE_WRAPPER_END' || true\n${file("${path.module}/scripts/mariadb-entrypoint-wrapper.sh")}PULSEBRIDGE_WRAPPER_END\n")
    error_message = "The startup script must carry scripts/mariadb-entrypoint-wrapper.sh verbatim in its quoted here-document."
  }

  assert {
    condition     = strcontains(google_compute_instance.this.metadata["startup-script"], "EDGE_MARIADB_INIT_WAIT_SECONDS='300'")
    error_message = "MariaDB's first initialisation must wait 300 s for its temporary server by default."
  }
}

run "mariadb_init_wait_is_a_variable" {
  command = plan

  variables {
    boot_wait_minutes         = 20
    mariadb_init_wait_seconds = 600
  }

  assert {
    condition     = strcontains(google_compute_instance.this.metadata["startup-script"], "EDGE_MARIADB_INIT_WAIT_SECONDS='600'")
    error_message = "mariadb_init_wait_seconds must reach the startup script."
  }
}

# --- The seam for T23 --------------------------------------------------------------------------------

run "hooks_are_metadata_keys_listed_in_the_script" {
  command = plan

  variables {
    hooks = {
      "openemr"  = "echo openemr hook"
      "a-second" = "echo second hook"
    }
    secret_files = {
      "mariadb-root-password" = "projects/example-project-123/secrets/pulsebridge-db-root-password"
      "openemr-db-password"   = "projects/example-project-123/secrets/pulsebridge-openemr-db-password"
    }
  }

  assert {
    condition     = google_compute_instance.this.metadata["pulsebridge-hook-openemr"] == "echo openemr hook"
    error_message = "Each hook must be stored under pulsebridge-hook-<name>."
  }

  assert {
    condition     = strcontains(google_compute_instance.this.metadata["startup-script"], "EDGE_HOOK_NAMES=(\n  'a-second'\n  'openemr'\n)")
    error_message = "The script must list the hooks in name order."
  }

  assert {
    condition     = output.hook_metadata_keys == ["pulsebridge-hook-a-second", "pulsebridge-hook-openemr"]
    error_message = "hook_metadata_keys must list the keys in run order."
  }

  assert {
    condition     = strcontains(google_compute_instance.this.metadata["startup-script"], "'openemr-db-password=projects/example-project-123/secrets/pulsebridge-openemr-db-password'")
    error_message = "Extra secret_files entries must reach the script."
  }
}

run "adapter_error_message_logging_is_opt_in" {
  command = plan

  variables {
    mllp_adapter_log_error_messages = true
  }

  assert {
    condition     = contains(output.mllp_adapter_command, "--log_error_msg=true") && !contains(output.mllp_adapter_command, "--log_error_msg=false")
    error_message = "mllp_adapter_log_error_messages = true must pass --log_error_msg=true."
  }
}

run "boot_image_output_and_patching" {
  command = plan

  variables {
    boot_image = "projects/cos-cloud/global/images/family/cos-125-lts"
  }

  assert {
    condition     = output.boot_image == "projects/cos-cloud/global/images/family/cos-125-lts"
    error_message = "boot_image must report the image the VM is created from."
  }
}

run "metadata_size_limit_counts_bytes" {
  command = plan

  variables {
    # 1,000 x 100 three-byte characters (the euro sign): 100,000 characters but 300,000 bytes, over the 256 KB
    # per-value limit only when bytes (not characters) are counted.
    hooks = { "big" = join("", [for i in range(1000) : "€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€"]) }
  }

  expect_failures = [google_compute_instance.this]
}

run "metadata_under_the_limit_plans" {
  command = plan

  variables {
    hooks = { "small" = join("", [for i in range(500) : "€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€"]) }
  }

  assert {
    condition     = length(google_compute_instance.this.metadata["pulsebridge-hook-small"]) == 50000
    error_message = "A hook under the limit must plan."
  }
}

run "logging_agent_can_be_switched_off" {
  command = plan

  variables {
    enable_cloud_logging = false
  }

  assert {
    condition     = google_compute_instance.this.metadata["google-logging-enabled"] == "false"
    error_message = "enable_cloud_logging = false must turn the COS logging agent off."
  }
}

# ---- data disk snapshots ----------------------------------------------------------------------

run "no_snapshot_schedule_by_default" {
  command = plan

  assert {
    condition     = length(google_compute_resource_policy.data_snapshots) == 0 && length(google_compute_disk_resource_policy_attachment.data_snapshots) == 0
    error_message = "data_disk_snapshot_days = 0 (default) must create no snapshot schedule."
  }
}

run "seven_day_snapshot_schedule_on_the_data_disk" {
  command = plan

  variables {
    data_disk_snapshot_days = 7
  }

  assert {
    condition = (
      length(google_compute_resource_policy.data_snapshots) == 1 &&
      google_compute_resource_policy.data_snapshots[0].region == "us-central1" &&
      google_compute_resource_policy.data_snapshots[0].snapshot_schedule_policy[0].schedule[0].daily_schedule[0].days_in_cycle == 1 &&
      google_compute_resource_policy.data_snapshots[0].snapshot_schedule_policy[0].retention_policy[0].max_retention_days == 7 &&
      google_compute_resource_policy.data_snapshots[0].snapshot_schedule_policy[0].retention_policy[0].on_source_disk_delete == "KEEP_AUTO_SNAPSHOTS"
    )
    error_message = "data_disk_snapshot_days = 7 must create one daily schedule in the VM's region, keeping 7 days and keeping snapshots when the disk is deleted."
  }
  assert {
    condition = (
      length(google_compute_disk_resource_policy_attachment.data_snapshots) == 1 &&
      google_compute_disk_resource_policy_attachment.data_snapshots[0].disk == google_compute_disk.data.name &&
      google_compute_disk_resource_policy_attachment.data_snapshots[0].name == google_compute_resource_policy.data_snapshots[0].name &&
      google_compute_disk_resource_policy_attachment.data_snapshots[0].zone == "us-central1-a"
    )
    error_message = "The schedule must be attached to the module's data disk, in its zone."
  }
}
