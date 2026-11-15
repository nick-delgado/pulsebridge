# Module `openemr` (task T23): OpenEMR on the edge VM, its database connection and its OAuth2 client.
#
# This module creates NO resources. The VM, its disk and its metadata belong to edge-vm, which takes
# boot hooks and extra secret files as inputs (its README, "The seam with T23"). A module here that
# created resources on the VM would need edge-vm's instance while edge-vm needs its hooks: a cycle.
# So this module only renders values for the env root (T25) to pass on:
#
#   hooks            -> edge-vm `hooks`         two bash scripts, run on every boot after MariaDB is healthy
#   secret files     -> edge-vm `secret_files`  the two OpenEMR passwords, copied from Secret Manager
#   timeout          -> edge-vm `hook_timeout_minutes`
#   connection       -> phr (T22) / fhir-proxy  FHIR base URL, token URL, site_addr_oath, scopes,
#                                               and the secret that holds the client's key
#
# Each hook is four parts joined, like edge-vm's startup script: a configuration block rendered
# from non-secret values (templates/openemr-config.sh.tftpl), a function that writes the PHP helper
# (templates/client-tools.sh.tftpl around scripts/client-tools.php), shared bash functions
# (scripts/openemr-lib.sh) and the hook's own steps (scripts/openemr-*.sh). The bash files contain no
# Terraform interpolation, so they can be linted and tested on their own; tests/hooks.sh renders the
# real hooks with `terraform console` and runs them in a sandbox.

locals {
  # The URL fhir-proxy calls, and the value of OpenEMR's site_addr_oath setting. They must be the
  # same: OpenEMR derives its OAuth2 issuer and the audience it expects in a client assertion from
  # site_addr_oath (spike S1: an assertion whose `aud` is not OpenEMR's own token URL is refused).
  site_address  = var.https_port == 443 ? "https://${var.internal_ip}" : "https://${var.internal_ip}:${var.https_port}"
  fhir_base_url = "${local.site_address}/apis/default/fhir"
  token_url     = "${local.site_address}/oauth2/default/token"

  image = "${var.dockerhub_mirror_url}/${var.openemr_image}"

  # Exactly what fhir-proxy's share action (T15) needs, as spike S1 registered and tested them
  # (SMART v1 "system/<Resource>.<read|write>" scopes, which OpenEMR 8.5.0 offers only with
  # rest_system_scopes_api on):
  #   Patient.read      search by identifier before creating (S1: without it the search got 401),
  #                     and the read-back by id for the "As stored in OpenEMR" panel (D13);
  #   Patient.write     create the Patient when the search finds none;
  #   Encounter.write   create the Encounter each Observation must reference;
  #   Encounter.read    reuse an Encounter (search) and the read-back;
  #   Observation.write create each Observation;
  #   Observation.read  the read-back by id.
  # Nothing else: no user/ or patient/ scopes, no openid, no api:oemr (the standard REST API).
  scopes = [
    "system/Patient.read",
    "system/Patient.write",
    "system/Encounter.read",
    "system/Encounter.write",
    "system/Observation.read",
    "system/Observation.write",
  ]

  # OpenEMR settings (rows of its `globals` table) set through OPENEMR_SETTING_<name> variables on
  # the container (spike S1 verified the image applies them on every start), and checked in the
  # database after start, with a database write as the fallback (spike S2 found the variables had
  # no effect there). rest_api, the standard (non-FHIR) REST API, is not needed for the FHIR path
  # and is kept off: it is never set to 1, and the check sets it back to 0 if it is on.
  settings = {
    rest_fhir_api          = "1"
    rest_system_scopes_api = "1"
    site_addr_oath         = local.site_address
  }
  settings_kept_off = ["rest_api"]

  # File names under edge-vm's secrets directory. mariadb-root-password is edge-vm's own (required
  # there); the two OpenEMR passwords are added to edge-vm's secret_files by the env root.
  secret_files = {
    "openemr-db-password"    = var.secret_ids.db_password
    "openemr-admin-password" = var.secret_ids.admin_password
  }

  # Hook names: edge-vm runs hooks in name order, so "openemr" (the container) runs before
  # "openemr-oauth-client" (the registration). Each has its own time limit.
  hook_container = "openemr"
  hook_client    = "openemr-oauth-client"

  config = {
    image             = local.image
    internal_ip       = var.internal_ip
    https_port        = tostring(var.https_port)
    site_address      = local.site_address
    db_user           = var.db_user
    client_name       = var.client_name
    scopes            = join(" ", local.scopes)
    client_key_secret = var.secret_ids.client_private_key
    block_metadata    = var.block_metadata_server ? "true" : "false"
    tls_days          = tostring(var.tls_certificate_days)
    tls_cert_target   = var.tls_mount_targets.certificate
    tls_key_target    = var.tls_mount_targets.key
    settings          = [for k in sort(keys(local.settings)) : "${k}=${local.settings[k]}"]
    settings_kept_off = local.settings_kept_off
  }

  # Every value written into the configuration block, for the quote precondition in outputs.tf.
  config_values = flatten([
    [for k, v in local.config : v if can(tostring(v))],
    local.config.settings,
    local.config.settings_kept_off,
  ])

  # The PHP helper is embedded as a quoted heredoc ("<< 'PULSEBRIDGE_PHP_END'": no shell
  # expansion inside). The precondition checks the delimiter cannot occur in the file.
  php_tools          = file("${path.module}/scripts/client-tools.php")
  php_tools_function = templatefile("${path.module}/templates/client-tools.sh.tftpl", { php = local.php_tools })
  lib                = file("${path.module}/scripts/openemr-lib.sh")

  hooks = {
    (local.hook_container) = join("", [
      templatefile("${path.module}/templates/openemr-config.sh.tftpl", merge(local.config, { hook_name = local.hook_container })),
      local.php_tools_function,
      local.lib,
      file("${path.module}/scripts/openemr-container.sh"),
    ])
    (local.hook_client) = join("", [
      templatefile("${path.module}/templates/openemr-config.sh.tftpl", merge(local.config, { hook_name = local.hook_client })),
      local.php_tools_function,
      local.lib,
      file("${path.module}/scripts/openemr-oauth-client.sh"),
    ])
  }

  # Instance metadata limits are in bytes (256 KB per value, 512 KB in total, shared with edge-vm's
  # startup script). base64 encodes the UTF-8 bytes, so 3/4 of its length bounds the byte size.
  hook_bytes = { for k, v in local.hooks : k => floor(length(base64encode(v)) * 3 / 4) }
}
