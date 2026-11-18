# Module `edge-vm` (task T21): the one always-on machine in PulseBridge. An e2-micro running
# Container-Optimized OS (COS) on the private edge subnet, with
#   - no external IP and no firewall rule of its own (decisions D13, D14: project-base owns the one
#     rule that lets Cloud Run's serverless subnet reach MLLP and OpenEMR HTTPS);
#   - a dedicated service account (never the Compute Engine default account);
#   - a separate persistent disk for MariaDB and OpenEMR site data, so replacing the VM keeps the
#     clinic data, while `terraform destroy` still removes it;
#   - a startup script, run by COS on every boot, that mounts the disk, turns on swap, and starts
#     Google's MLLP adapter and MariaDB with `docker run` (see README.md, "How the stack starts").
#
# Nothing secret is in this file, in the startup script or in metadata: the MariaDB root password is
# read from Secret Manager by the VM's own account at boot.

locals {
  region = regex("^(.+)-[a-z]$", var.zone)[0]

  # The subnet ID carries its region: projects/<p>/regions/<region>/subnetworks/<name>.
  subnet_region = regex("/regions/([^/]+)/", var.edge_subnet_id)[0]

  instance_name = "${var.name_prefix}-edge-vm"
  disk_name     = "${var.name_prefix}-edge-data"
  address_name  = "${var.name_prefix}-edge-internal"

  # The disk appears on the VM as /dev/disk/by-id/google-<device_name>. Compute Engine creates that
  # symlink from the name given here, so the script never has to guess /dev/sdb.
  data_device_name = "edge-data"

  # Paths on the VM. COS layout (docs: "Disks and file system overview"): / is read-only; /etc,
  # /mnt/disks and /tmp are tmpfs (emptied on every boot); /var and /home are stateful but
  # non-executable; /mnt/stateful_partition is the writable stateful partition of the boot disk.
  data_mount_dir = "/mnt/disks/edge"           # the data disk, mounted on every boot
  state_dir      = "/var/lib/pulsebridge-edge" # boot-disk state: docker config, hooks
  swap_file      = "/mnt/stateful_partition/pulsebridge-swapfile"

  # Docker Hub mirror <location>-docker.pkg.dev/<project>/<repository>: the credential helper is
  # registered for its host. Read access to it is granted by project-base, which owns it.
  mirror_host = split("/", var.dockerhub_mirror_url)[0]

  mariadb_image = "${var.dockerhub_mirror_url}/${var.mariadb_image}"

  # MariaDB server options from spike S2's compose file (the low-memory "safety ceiling"): a 64 MB
  # InnoDB buffer pool instead of 128 MB, no performance schema, at most 30 connections and small
  # per-connection buffers. They are passed to the `mariadbd` command exactly as S2 ran them.
  mariadb_command = [
    "mariadbd",
    "--character-set-server=utf8mb4",
    "--innodb-buffer-pool-size=64M",
    "--innodb-log-buffer-size=8M",
    "--performance-schema=OFF",
    "--max-connections=30",
    "--table-open-cache=400",
    "--key-buffer-size=8M",
    "--tmp-table-size=16M",
    "--max-heap-table-size=16M",
    "--thread-cache-size=4",
  ]

  # The MLLP adapter command: spike S4's flags, with error-message logging off by default:
  #   - the image has no entrypoint, so the binary path is the first word;
  #   - --port is the listen port inside the container; --receiver_ip=0.0.0.0 listens on every
  #     interface of the container, and Docker's -p decides what the VM exposes;
  #   - the store is given as four short names (--hl7_v2_store_id=<store>, not a resource path;
  #     S4's first attempt, with --hl7_v2_hl7_v2_store_id and --logtostderr, crash-looped);
  #   - --log_error_msg: S4 ran with true. Upstream's help says "These error logs will contain
  #     sensitive data", so it follows var.mllp_adapter_log_error_messages (default false);
  #   - --export_stats=false: the default sends metrics to Cloud Monitoring, which the VM's account
  #     is not allowed to write;
  #   - the content-logging flags (--log_ack, --log_nacked_msg, --log_input_msg_in_base64) are left
  #     at their default, off: message content is patient data.
  #   - no --credentials: the adapter gets tokens for the VM's account from the metadata server.
  mllp_adapter_command = [
    "/usr/mllp_adapter/mllp_adapter",
    "--port=2575",
    "--receiver_ip=0.0.0.0",
    "--hl7_v2_project_id=${var.hl7v2_store_flags.hl7_v2_project_id}",
    "--hl7_v2_location_id=${var.hl7v2_store_flags.hl7_v2_location_id}",
    "--hl7_v2_dataset_id=${var.hl7v2_store_flags.hl7_v2_dataset_id}",
    "--hl7_v2_store_id=${var.hl7v2_store_flags.hl7_v2_store_id}",
    "--log_error_msg=${var.mllp_adapter_log_error_messages}",
    "--export_stats=false",
  ]

  hook_names = sort(keys(var.hooks))

  # Values written into the startup script's configuration block. All are non-secret; the
  # precondition on the instance checks none contains a single quote, so single-quoting them in
  # the template is safe.
  startup_config = {
    data_device          = "/dev/disk/by-id/google-${local.data_device_name}"
    data_mount_dir       = local.data_mount_dir
    state_dir            = local.state_dir
    swap_file            = local.swap_file
    swap_size_mb         = tostring(var.swap_size_mb)
    mirror_host          = local.mirror_host
    mirror_url           = var.dockerhub_mirror_url
    mariadb_image        = local.mariadb_image
    mllp_adapter_image   = var.mllp_adapter_image
    mllp_port            = tostring(var.mllp_port)
    boot_wait_seconds    = tostring(var.boot_wait_minutes * 60)
    hook_timeout_seconds = tostring(var.hook_timeout_minutes * 60)
    # T42: how long a first initialisation's temporary server may take to start.
    mariadb_init_wait_seconds = tostring(var.mariadb_init_wait_seconds)
    mariadb_command           = local.mariadb_command
    mllp_adapter_command      = local.mllp_adapter_command
    secret_names              = sort(keys(var.secret_files))
    secret_files              = var.secret_files
    hook_names                = local.hook_names
  }

  startup_config_values = flatten([
    [for k, v in local.startup_config : v if can(tostring(v))],
    local.mariadb_command,
    local.mllp_adapter_command,
    keys(var.secret_files),
    values(var.secret_files),
    local.hook_names,
  ])

  # The script is two parts joined: a configuration block rendered by templatefile() with the
  # values above, and the logic, which is a plain bash file (no Terraform interpolation, so it can
  # be shellchecked and tested on its own; see tests/startup_script.sh).
  # T42: the MariaDB entrypoint wrapper is a third file, placed in the configuration block inside a
  # quoted here-document (it contains quotes, so it is not one of the single-quoted values above).
  mariadb_entrypoint_wrapper = chomp(file("${path.module}/scripts/mariadb-entrypoint-wrapper.sh"))

  startup_script = join("", [
    templatefile("${path.module}/templates/startup-config.sh.tftpl", merge(local.startup_config, {
      mariadb_entrypoint_wrapper = local.mariadb_entrypoint_wrapper
    })),
    file("${path.module}/scripts/edge-startup.sh"),
  ])

  # Instance metadata. Keys only from Google's predefined set plus the hook scripts:
  #   - enable-oslogin: SSH access, if ever opened, is by IAM (OS Login), not by keys in metadata;
  #   - block-project-ssh-keys: project-wide SSH keys are not accepted on this VM;
  #   - google-logging-enabled: COS's logging agent ships logs to Cloud Logging;
  #   - cos-update-strategy: set explicitly to update_disabled. COS's in-place updates "are not
  #     supported on Container-Optimized OS VMs with UEFI Secure Boot enabled" and are off by
  #     default from milestone 117 anyway; patching is done by replacing the VM (README,
  #     "Patching the VM"), so the behaviour should not depend on a milestone's default.
  # No ssh-keys key, and no key holding a password or token (tests/static_checks.sh and the tests
  # check both).
  instance_metadata = merge(
    {
      "startup-script"         = local.startup_script
      "enable-oslogin"         = "TRUE"
      "block-project-ssh-keys" = "TRUE"
      "google-logging-enabled" = var.enable_cloud_logging ? "true" : "false"
      "cos-update-strategy"    = "update_disabled"
    },
    { for name, script in var.hooks : "pulsebridge-hook-${name}" => script }
  )

  # Metadata limits are in bytes; length() counts characters. base64 encodes the UTF-8 bytes, so
  # 3/4 of the base64 length is an upper bound on each value's size in bytes.
  metadata_value_bytes = { for k, v in local.instance_metadata : k => floor(length(base64encode(v)) * 3 / 4) }
}

# ---------------------------------------------------------------------------------------------
# Internal address
# ---------------------------------------------------------------------------------------------

# A reserved internal address, so the VM keeps the same IP when it is replaced. The cuff simulator's
# MLLP_HOST (T22) and OpenEMR's site address (T23) point at it. Internal addresses, static or
# ephemeral, are free (VPC pricing: "There is no charge for static or ephemeral internal IP
# addresses"). It is INTERNAL: nothing here is reachable from the internet.
resource "google_compute_address" "internal" {
  project      = var.project_id
  name         = local.address_name
  description  = "Internal address of the PulseBridge edge VM (MLLP adapter, OpenEMR)."
  region       = local.region
  address_type = "INTERNAL"
  purpose      = "GCE_ENDPOINT"
  subnetwork   = var.edge_subnet_id
  address      = var.internal_ip
  labels       = var.labels
}

# ---------------------------------------------------------------------------------------------
# Data disk
# ---------------------------------------------------------------------------------------------

# Its own resource, not part of the instance: when the VM is replaced (new image, new machine
# type), Terraform detaches this disk from the old VM and attaches it to the new one, so the
# MariaDB database and OpenEMR site files survive. No `prevent_destroy`: `terraform destroy` must
# leave nothing billable, so it deletes this disk and the clinic data with it (synthetic in dev).
resource "google_compute_disk" "data" {
  project     = var.project_id
  name        = local.disk_name
  description = "PulseBridge edge VM data: MariaDB data directory and OpenEMR site files."
  zone        = var.zone
  type        = var.disk_type
  size        = var.data_disk_size_gb
  labels      = var.labels
}

# Optional daily snapshots of the data disk (data_disk_snapshot_days > 0; prod in the env roots).
# A snapshot schedule is a Compute Engine resource policy attached to the disk. Snapshots are
# incremental and billed as snapshot storage. on_source_disk_delete = KEEP_AUTO_SNAPSHOTS keeps
# the snapshots when the disk is deleted (VM replacement keeps the disk anyway; a `terraform
# destroy` deletes it), so the clinic database can be restored after an accidental destroy; the
# cost of that is that those snapshots OUTLIVE a destroy and are billed until someone deletes
# them (README, "Snapshots of the data disk").
resource "google_compute_resource_policy" "data_snapshots" {
  count = var.data_disk_snapshot_days > 0 ? 1 : 0

  project     = var.project_id
  name        = "${var.name_prefix}-edge-data-daily"
  region      = local.region
  description = "Daily snapshots of the PulseBridge edge VM data disk."

  snapshot_schedule_policy {
    schedule {
      daily_schedule {
        days_in_cycle = 1
        start_time    = "04:00" # UTC; Compute Engine requires a full hour
      }
    }
    retention_policy {
      max_retention_days    = var.data_disk_snapshot_days
      on_source_disk_delete = "KEEP_AUTO_SNAPSHOTS"
    }
    snapshot_properties {
      labels            = var.labels
      storage_locations = [local.region]
    }
  }
}

resource "google_compute_disk_resource_policy_attachment" "data_snapshots" {
  count = var.data_disk_snapshot_days > 0 ? 1 : 0

  project = var.project_id
  zone    = var.zone
  disk    = google_compute_disk.data.name
  name    = google_compute_resource_policy.data_snapshots[0].name
}

# ---------------------------------------------------------------------------------------------
# The VM
# ---------------------------------------------------------------------------------------------

resource "google_compute_instance" "this" {
  project      = var.project_id
  name         = local.instance_name
  description  = "PulseBridge edge VM: MLLP adapter, MariaDB (and OpenEMR, T23). No external IP."
  zone         = var.zone
  machine_type = var.machine_type
  tags         = [var.network_tag]
  labels       = var.labels

  # Changing the machine type, service account, scopes or Shielded VM settings needs the VM
  # stopped. With this on, Terraform stops and restarts it (a reboot: the startup script runs
  # again); without it such a change fails. Replacement is never needed for them.
  allow_stopping_for_update = true
  deletion_protection       = false # `terraform destroy` must be able to remove it

  boot_disk {
    auto_delete = true # the boot disk holds nothing that must survive (images, swap, docker config)

    initialize_params {
      image  = var.boot_image
      size   = var.boot_disk_size_gb
      type   = var.disk_type
      labels = var.labels
    }
  }

  attached_disk {
    source      = google_compute_disk.data.self_link
    device_name = local.data_device_name # -> /dev/disk/by-id/google-edge-data on the VM
    mode        = "READ_WRITE"
  }

  network_interface {
    subnetwork = var.edge_subnet_id
    network_ip = google_compute_address.internal.address
    # No access_config block: an access_config is what gives an instance an external IP. Without
    # one the VM has only its internal address, reaches Google APIs through Private Google Access
    # on the edge subnet, and cannot be reached from the internet at all.
  }

  service_account {
    email = var.service_account_email
    # Access scopes are an older, coarse limit on what the VM's tokens may call. Secret Manager
    # requires the cloud-platform scope on Compute Engine (Secret Manager access-control docs), so
    # no narrower set works; Google's guidance is to set cloud-platform and limit the account with
    # IAM, which the identity and healthcare modules do (HL7v2 ingest on one store, three secrets,
    # log writing, and the mirror read granted above).
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }

  # Shielded VM: Secure Boot only runs a signed boot chain, the virtual TPM measures it, and
  # integrity monitoring compares each boot with a known-good baseline. COS images support all
  # three; none costs anything.
  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  scheduling {
    # Unattended recovery: live-migrate during host maintenance and restart after a host failure.
    on_host_maintenance = "MIGRATE"
    automatic_restart   = true
    preemptible         = false # the free tier covers only non-preemptible e2-micro
  }

  # The startup script goes in metadata["startup-script"], not the `metadata_startup_script`
  # argument: a change to that argument replaces the VM, while a metadata change is applied in
  # place and runs on the next boot (or `sudo google_metadata_script_runner startup`).
  metadata = local.instance_metadata

  lifecycle {
    # An image family such as cos-stable moves to a new image every few weeks. Ignoring it keeps
    # an unrelated apply from replacing the VM. The cost: the VM stays on the image it was created
    # from, and a change to var.boot_image takes effect only when the VM is replaced on purpose:
    # `terraform apply -replace=module.<name>.google_compute_instance.this` (README, "Patching the
    # VM"; the data disk and the reserved address survive).
    ignore_changes = [boot_disk[0].initialize_params[0].image]

    precondition {
      condition     = local.subnet_region == local.region
      error_message = "zone must be in the edge subnet's region (${local.subnet_region})."
    }

    precondition {
      # Defence in depth: every input that reaches the configuration block is already limited by
      # a validation to characters without quotes, so this cannot fail today. It stays in case a
      # validation is loosened later, because a quote would break out of the script's quoting.
      condition     = alltrue([for v in local.startup_config_values : !strcontains(v, "'")])
      error_message = "Startup-script configuration values must not contain a single quote."
    }

    precondition {
      # The wrapper sits in a here-document ended by this word on a line of its own; a line equal
      # to it inside the wrapper would end the here-document early and run the rest as script.
      condition     = !contains(split("\n", local.mariadb_entrypoint_wrapper), "PULSEBRIDGE_WRAPPER_END")
      error_message = "scripts/mariadb-entrypoint-wrapper.sh must not contain a line PULSEBRIDGE_WRAPPER_END."
    }

    precondition {
      # Instance metadata allows 256 KB per value and 512 KB in total (bytes).
      condition     = alltrue([for b in values(local.metadata_value_bytes) : b <= 262144]) && sum(values(local.metadata_value_bytes)) <= 524288
      error_message = "The startup script or a hook exceeds the instance metadata size limits (256 KB per value, 512 KB in total)."
    }
  }
}
