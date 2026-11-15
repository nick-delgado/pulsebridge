# Outputs are the module's whole contract (it has no resources):
#   - for edge-vm (wired by T25): edge_vm_hooks, edge_vm_secret_files, hook_timeout_minutes;
#   - for fhir-proxy (wired by T22, used by T15): the URLs, the scopes and the key secret;
#   - for people and checks: the image, the settings and the hook metadata keys.

output "edge_vm_hooks" {
  description = "Pass to edge-vm `hooks`: { openemr = <script>, openemr-oauth-client = <script> }. edge-vm runs them in that (name) order on every boot after MariaDB is healthy: the first starts OpenEMR, the second registers fhir-proxy's OAuth2 client."
  value       = local.hooks

  precondition {
    # Every input that reaches the configuration block is validated to exclude quotes, so this
    # cannot fail today; it stays in case a validation is loosened, because a single quote would
    # break out of the script's quoting.
    condition     = alltrue([for v in local.config_values : !strcontains(v, "'")])
    error_message = "A hook configuration value contains a single quote."
  }

  precondition {
    condition     = !strcontains(local.php_tools, "PULSEBRIDGE_PHP_END")
    error_message = "scripts/client-tools.php must not contain the heredoc delimiter PULSEBRIDGE_PHP_END."
  }

  precondition {
    # Instance metadata: 256 KB per value, 512 KB in total, shared with edge-vm's startup script
    # (about 40 KB) and its other keys. Each hook stays under 64 KB, both under 160 KB.
    condition     = alltrue([for b in values(local.hook_bytes) : b <= 65536]) && sum(values(local.hook_bytes)) <= 163840
    error_message = "An OpenEMR hook is over 64 KB, or both together over 160 KB (instance metadata limits, shared with edge-vm)."
  }
}

output "edge_vm_secret_files" {
  description = "Merge into edge-vm `secret_files`: { openemr-db-password = <secret ID>, openemr-admin-password = <secret ID> }. edge-vm copies them to mode-600 files after MariaDB is up; a missing one never stops MariaDB, and the openemr hook then refuses to start OpenEMR and names it."
  value       = local.secret_files
}

output "hook_timeout_minutes" {
  description = "Pass to edge-vm `hook_timeout_minutes`. Each OpenEMR hook needs this long on first boot (image pull plus first-time setup, spike S2) and stops itself a minute earlier with a clear error."
  value       = var.hook_timeout_minutes
}

output "hook_metadata_keys" {
  description = "Instance metadata keys edge-vm stores the hooks under, in the order they run."
  value       = [for name in sort(keys(local.hooks)) : "pulsebridge-hook-${name}"]
}

output "site_address" {
  description = "OpenEMR's site_addr_oath setting and the base URL fhir-proxy calls: https://<VM internal address>[:port]. The assertion's `aud` is token_url, derived from it (spike S1)."
  value       = local.site_address
}

output "fhir_base_url" {
  description = "OpenEMR's FHIR R4 base URL on the VM's internal address, for fhir-proxy (reached through Direct VPC egress and the project-base firewall rule)."
  value       = local.fhir_base_url
}

output "token_url" {
  description = "OpenEMR's OAuth2 token endpoint (client credentials grant). Also the `aud` claim of fhir-proxy's signed client assertion: OpenEMR refuses any other audience (spike S1)."
  value       = local.token_url
}

output "scopes" {
  description = "The scopes the client is registered with and fhir-proxy should request, space-separated (spike S1's six system/ scopes). A token request for anything else gets it silently left out."
  value       = join(" ", local.scopes)
}

output "client_secret_id" {
  description = "Secret Manager secret holding the client document fhir-proxy needs (README, \"What fhir-proxy gets\"): the private key, client ID, key ID and OpenEMR's certificate. Mount version `latest` as a file in fhir-proxy; the VM adds a new version whenever it registers a new client."
  value       = var.secret_ids.client_private_key
}

output "image" {
  description = "Full OpenEMR image reference the VM pulls (through the Docker Hub mirror, by digest)."
  value       = local.image
}

output "openemr_settings" {
  description = "OpenEMR settings (globals) the hook passes as OPENEMR_SETTING_* and enforces in the database; rest_api is kept at 0."
  value       = merge(local.settings, { for k in local.settings_kept_off : k => "0" })
}

output "container_name" {
  description = "Name of the OpenEMR container on the VM (for `sudo docker logs openemr`)."
  value       = "openemr"
}
