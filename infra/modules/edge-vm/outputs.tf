# Outputs are the module's contract with its consumers:
#   - phr (T22): the cuff simulator's MLLP_HOST and port;
#   - openemr (T23): the paths, network and container names its hook script builds on, and the
#     address for OpenEMR's site_addr_oath;
#   - the env roots (T25): instance name and zone for `gcloud compute ...` commands and checks.

output "instance_name" {
  description = "Name of the edge VM."
  value       = google_compute_instance.this.name
}

output "instance_id" {
  description = "ID of the edge VM (projects/<project>/zones/<zone>/instances/<name>)."
  value       = google_compute_instance.this.id
}

output "zone" {
  description = "Zone of the edge VM and its data disk."
  value       = google_compute_instance.this.zone
}

output "internal_ip" {
  description = "Reserved internal IPv4 address of the edge VM. It survives VM replacement. This is the cuff simulator's MLLP_HOST (T22) and the host part of OpenEMR's site address (T23)."
  value       = google_compute_address.internal.address
}

output "mllp_host" {
  description = "Host the cuff simulator sends MLLP to (same as internal_ip; named for T22's MLLP_HOST)."
  value       = google_compute_address.internal.address
}

output "mllp_port" {
  description = "Port the MLLP adapter is published on at mllp_host."
  value       = var.mllp_port
}

output "service_account_email" {
  description = "Service account the VM runs as (passed in from the identity module)."
  value       = var.service_account_email
}

output "data_disk_name" {
  description = "Name of the persistent data disk (MariaDB data, OpenEMR site files, copied secrets)."
  value       = google_compute_disk.data.name
}

output "data_disk_device" {
  description = "Stable device path of the data disk on the VM."
  value       = "/dev/disk/by-id/google-${local.data_device_name}"
}

output "data_mount_dir" {
  description = "Where the data disk is mounted on the VM."
  value       = local.data_mount_dir
}

output "mariadb_data_dir" {
  description = "MariaDB's data directory on the data disk (bind-mounted at /var/lib/mysql)."
  value       = "${local.data_mount_dir}/mariadb"
}

output "openemr_data_dir" {
  description = "Directory on the data disk reserved for T23: OpenEMR's sites directory (/var/www/localhost/htdocs/openemr/sites in the container)."
  value       = "${local.data_mount_dir}/openemr"
}

output "secrets_dir" {
  description = "Mode-700 directory on the data disk with one mode-600 file per entry of secret_files (for example mariadb-root-password)."
  value       = "${local.data_mount_dir}/secrets"
}

output "state_dir" {
  description = "Boot-disk directory for the startup script's state: docker client configuration (.docker), the run lock and the fetched hook scripts (hooks/). This is the units directory: there is no compose file on COS."
  value       = local.state_dir
}

output "docker_network" {
  description = "User-defined Docker bridge network MariaDB is on. T23's OpenEMR container joins it and reaches the database as host `mariadb`."
  value       = "pulsebridge-edge"
}

output "mariadb_container" {
  description = "Name (and DNS name on docker_network) of the MariaDB container."
  value       = "mariadb"
}

output "mllp_adapter_container" {
  description = "Name of the MLLP adapter container (for `docker logs mllp-adapter` on the VM)."
  value       = "mllp-adapter"
}

output "mariadb_image" {
  description = "Full MariaDB image reference the VM pulls (through the Docker Hub mirror, by digest)."
  value       = local.mariadb_image
}

output "mllp_adapter_command" {
  description = "The exact command the MLLP adapter container runs (binary path plus the flags recorded in spike S4)."
  value       = local.mllp_adapter_command
}

output "hook_metadata_keys" {
  description = "Instance metadata keys holding the hook scripts, in the order they run."
  value       = [for name in local.hook_names : "pulsebridge-hook-${name}"]
}
