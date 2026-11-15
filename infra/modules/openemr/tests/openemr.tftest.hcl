# Tests of what this module renders. They run with `terraform test` from the module directory.
#
# The module declares no provider and creates no resource, so there is nothing to mock: a plan here
# only evaluates variables, locals and outputs, and cannot call a Google API (`make tf-test` also
# removes Google credentials from the environment). The behaviour of the rendered hooks is tested
# by tests/hooks.sh, which runs them in a sandbox.

variables {
  # Synthetic values only.
  internal_ip          = "10.10.0.10"
  dockerhub_mirror_url = "us-central1-docker.pkg.dev/example-project-123/pulsebridge-dockerhub"
  secret_ids = {
    db_password        = "projects/example-project-123/secrets/pb-openemr-db-password"
    admin_password     = "projects/example-project-123/secrets/pb-openemr-admin-password"
    client_private_key = "projects/example-project-123/secrets/pb-openemr-client-private-key"
  }
}

run "image_pinned_by_digest_through_the_mirror" {
  command = plan

  assert {
    condition     = output.image == "us-central1-docker.pkg.dev/example-project-123/pulsebridge-dockerhub/openemr/openemr:8.5.0-2026-11-12@sha256:f9536d0bcad7956d21471e73b91690215ad00ee4aed9448770e5012df15318cd"
    error_message = "The default image must be spikes S1/S2's OpenEMR 8.5.0 digest, through the Docker Hub mirror."
  }

  assert {
    condition     = alltrue([for h in values(output.edge_vm_hooks) : strcontains(h, "OPENEMR_IMAGE='${output.image}'")])
    error_message = "Both hooks must carry the digest-pinned image reference."
  }
}

run "connection_settings_for_fhir_proxy" {
  command = plan

  assert {
    condition     = output.site_address == "https://10.10.0.10"
    error_message = "site_addr_oath must be the URL fhir-proxy calls: https://<internal_ip>."
  }

  assert {
    condition     = output.token_url == "https://10.10.0.10/oauth2/default/token" && output.fhir_base_url == "https://10.10.0.10/apis/default/fhir"
    error_message = "Token and FHIR URLs must be on the site address (S1: the assertion's aud is the token URL)."
  }

  assert {
    condition     = output.scopes == "system/Patient.read system/Patient.write system/Encounter.read system/Encounter.write system/Observation.read system/Observation.write"
    error_message = "The client's scopes must be exactly spike S1's six system/ scopes."
  }

  assert {
    condition     = alltrue([for h in values(output.edge_vm_hooks) : strcontains(h, "OPENEMR_CLIENT_SCOPES='${output.scopes}'")])
    error_message = "The hooks must register exactly the scopes in the output."
  }

  assert {
    condition     = output.client_secret_id == "projects/example-project-123/secrets/pb-openemr-client-private-key"
    error_message = "fhir-proxy must be pointed at the client key secret."
  }

  assert {
    condition     = output.client_secret_name == "pb-openemr-client-private-key"
    error_message = "client_secret_name must be the short secret ID that module phr and a Cloud Run secret volume take."
  }
}

run "null_scopes_mean_the_default" {
  command = plan

  variables {
    scopes = null
  }

  assert {
    condition     = output.scopes == "system/Patient.read system/Patient.write system/Encounter.read system/Encounter.write system/Observation.read system/Observation.write"
    error_message = "scopes = null must give spike S1's six default scopes (nullable = false), not null."
  }
}

run "non_default_https_port" {
  command = plan

  variables {
    https_port = 8443
  }

  assert {
    condition     = output.site_address == "https://10.10.0.10:8443" && output.token_url == "https://10.10.0.10:8443/oauth2/default/token"
    error_message = "A non-default port must be part of the site address and the token URL."
  }

  assert {
    condition     = strcontains(output.edge_vm_hooks["openemr"], "OPENEMR_HTTPS_PORT='8443'")
    error_message = "The container hook must publish the configured port."
  }
}

run "only_https_is_published" {
  command = plan

  assert {
    # One --publish on a code line (comments are left out), mapping the configured port to the
    # container's 443; nothing for port 80.
    condition     = length(regexall("--publish", join("\n", [for l in split("\n", output.edge_vm_hooks["openemr"]) : l if !startswith(trimspace(l), "#")]))) == 1 && strcontains(output.edge_vm_hooks["openemr"], "--publish \"$OPENEMR_HTTPS_PORT:443\"") && strcontains(output.edge_vm_hooks["openemr"], "OPENEMR_HTTPS_PORT='443'")
    error_message = "The OpenEMR container must publish exactly one port: <https_port>:443."
  }

  assert {
    condition     = length(regexall("(^|[^0-9])80:", output.edge_vm_hooks["openemr"])) == 0
    error_message = "Port 80 must not be published."
  }

  assert {
    condition     = length(regexall("--publish", output.edge_vm_hooks["openemr-oauth-client"])) == 0
    error_message = "The registration hook must not publish anything."
  }
}

run "openemr_settings" {
  command = plan

  assert {
    condition = alltrue([
      for s in ["'rest_fhir_api=1'", "'rest_system_scopes_api=1'", "'site_addr_oath=https://10.10.0.10'"] :
      strcontains(output.edge_vm_hooks["openemr"], s)
    ])
    error_message = "The container hook must set rest_fhir_api, rest_system_scopes_api and site_addr_oath."
  }

  assert {
    condition     = strcontains(output.edge_vm_hooks["openemr"], "env_args+=(--env \"OPENEMR_SETTING_$setting\")")
    error_message = "The settings must reach the container as OPENEMR_SETTING_<name> variables."
  }

  assert {
    # The standard REST API is never switched on: not passed as a setting, and enforced as 0.
    condition     = length(regexall("rest_api=1", output.edge_vm_hooks["openemr"])) == 0 && output.openemr_settings["rest_api"] == "0"
    error_message = "The standard rest_api must stay off."
  }

  assert {
    condition     = output.openemr_settings == { rest_fhir_api = "1", rest_system_scopes_api = "1", site_addr_oath = "https://10.10.0.10", rest_api = "0" }
    error_message = "openemr_settings must list exactly the three settings plus rest_api kept at 0."
  }
}

run "seam_with_edge_vm" {
  command = plan

  assert {
    condition     = keys(output.edge_vm_hooks) == ["openemr", "openemr-oauth-client"] && output.hook_metadata_keys == ["pulsebridge-hook-openemr", "pulsebridge-hook-openemr-oauth-client"]
    error_message = "Two hooks, the container first (edge-vm runs hooks in name order)."
  }

  assert {
    condition = output.edge_vm_secret_files == {
      "openemr-db-password"    = "projects/example-project-123/secrets/pb-openemr-db-password"
      "openemr-admin-password" = "projects/example-project-123/secrets/pb-openemr-admin-password"
    }
    error_message = "edge_vm_secret_files must name the two OpenEMR password secrets by resource ID, without versions."
  }

  assert {
    # edge-vm's secret_files keys must be lower-case file names; hooks keys 1-40 such characters.
    condition     = alltrue([for k in keys(output.edge_vm_secret_files) : can(regex("^[a-z0-9][a-z0-9-]{0,62}$", k))]) && alltrue([for k in keys(output.edge_vm_hooks) : can(regex("^[a-z0-9][a-z0-9-]{0,39}$", k))])
    error_message = "Output keys must satisfy edge-vm's validations."
  }

  assert {
    condition     = output.hook_timeout_minutes == 40
    error_message = "The hooks need a 40-minute limit by default (first boot: pull plus setup)."
  }

  assert {
    # Metadata values are limited in bytes; each hook stays well under edge-vm's 256 KB per value.
    condition     = alltrue([for h in values(output.edge_vm_hooks) : floor(length(base64encode(h)) * 3 / 4) <= 65536])
    error_message = "Each hook must stay under 64 KB."
  }

  assert {
    condition     = alltrue([for h in values(output.edge_vm_hooks) : startswith(h, "#!/bin/bash\n")])
    error_message = "Each hook must start with a bash shebang."
  }
}

run "no_secret_in_rendered_text" {
  command = plan

  assert {
    # Passwords go to the container by name only (--env NAME), never NAME=value in the script.
    condition     = alltrue([for h in values(output.edge_vm_hooks) : length(regexall("--env \"?(MYSQL_ROOT_PASS|MYSQL_PASS|OE_PASS|MYSQL_PWD)=", h)) == 0])
    error_message = "A password variable is given a value on a docker command line."
  }

  assert {
    # Code lines only: the comments say that these are not used.
    condition     = alltrue([for h in values(output.edge_vm_hooks) : length(regexall("PRIVATE KEY-----|set -[a-z]*x|--insecure|curl -k", join("\n", [for l in split("\n", h) : l if !startswith(trimspace(l), "#")]))) == 0])
    error_message = "A hook contains key material, tracing or switched-off TLS verification."
  }

  assert {
    # Secret Manager secrets are named by resource ID only, never with a version or a value.
    condition     = alltrue([for h in values(output.edge_vm_hooks) : length(regexall("/versions/[0-9]+", h)) == 0])
    error_message = "A hook names a specific secret version."
  }
}

run "metadata_block_switch" {
  command = plan

  assert {
    condition     = strcontains(output.edge_vm_hooks["openemr"], "OPENEMR_BLOCK_METADATA='true'")
    error_message = "The metadata-server block must be on by default."
  }
}

run "metadata_block_off" {
  command = plan

  variables {
    block_metadata_server = false
  }

  assert {
    condition     = strcontains(output.edge_vm_hooks["openemr"], "OPENEMR_BLOCK_METADATA='false'")
    error_message = "block_metadata_server = false must reach the hook."
  }
}

# --- Validations ----------------------------------------------------------------------------------

run "rejects_image_tag_without_digest" {
  command = plan
  variables {
    openemr_image = "openemr/openemr:8.5.0"
  }
  expect_failures = [var.openemr_image]
}

run "rejects_image_with_short_digest" {
  command = plan
  variables {
    openemr_image = "openemr/openemr@sha256:f9536d0b"
  }
  expect_failures = [var.openemr_image]
}

run "rejects_hostname_instead_of_ip" {
  command = plan
  variables {
    internal_ip = "openemr.example.internal"
  }
  expect_failures = [var.internal_ip]
}

run "rejects_bad_ip" {
  command = plan
  variables {
    internal_ip = "10.10.0.300"
  }
  expect_failures = [var.internal_ip]
}

run "rejects_bad_port" {
  command = plan
  variables {
    https_port = 0
  }
  expect_failures = [var.https_port]
}

run "rejects_secret_version" {
  command = plan
  variables {
    secret_ids = {
      db_password        = "projects/example-project-123/secrets/pb-openemr-db-password/versions/1"
      admin_password     = "projects/example-project-123/secrets/pb-openemr-admin-password"
      client_private_key = "projects/example-project-123/secrets/pb-openemr-client-private-key"
    }
  }
  expect_failures = [var.secret_ids]
}

run "rejects_bad_mirror" {
  command = plan
  variables {
    dockerhub_mirror_url = "docker.io/library"
  }
  expect_failures = [var.dockerhub_mirror_url]
}

run "rejects_quote_in_client_name" {
  command = plan
  variables {
    client_name = "pulse'bridge"
  }
  expect_failures = [var.client_name]
}

run "rejects_bad_db_user" {
  command = plan
  variables {
    db_user = "root; drop"
  }
  expect_failures = [var.db_user]
}

run "rejects_short_hook_timeout" {
  command = plan
  variables {
    hook_timeout_minutes = 20
  }
  expect_failures = [var.hook_timeout_minutes]
}

run "rejects_long_certificate" {
  command = plan
  variables {
    tls_certificate_days = 10000
  }
  expect_failures = [var.tls_certificate_days]
}

run "rejects_quote_in_tls_path" {
  command = plan
  variables {
    tls_mount_targets = {
      certificate = "/etc/ssl/certs/x'.pem"
      key         = "/etc/ssl/private/selfsigned.key.pem"
    }
  }
  expect_failures = [var.tls_mount_targets]
}

# --- Scopes (review F1) ---------------------------------------------------------------------------

run "narrower_smart_v2_scopes" {
  command = plan

  variables {
    scopes = ["system/Patient.crs", "system/Encounter.crs", "system/Observation.cr"]
  }

  assert {
    condition     = output.scopes == "system/Patient.crs system/Encounter.crs system/Observation.cr"
    error_message = "The configured scopes must be the ones output for fhir-proxy."
  }

  assert {
    condition     = alltrue([for h in values(output.edge_vm_hooks) : strcontains(h, "OPENEMR_CLIENT_SCOPES='system/Patient.crs system/Encounter.crs system/Observation.cr'")])
    error_message = "The hooks must register exactly the configured scopes."
  }
}

run "rejects_wildcard_scope" {
  command = plan
  variables {
    scopes = ["system/*.read"]
  }
  expect_failures = [var.scopes]
}

run "rejects_other_resource_scope" {
  command = plan
  variables {
    scopes = ["system/Patient.read", "system/Practitioner.read"]
  }
  expect_failures = [var.scopes]
}

run "rejects_unordered_v2_scope" {
  command = plan
  variables {
    scopes = ["system/Patient.rcs"]
  }
  expect_failures = [var.scopes]
}

run "rejects_empty_suffix_and_user_scope" {
  command = plan
  variables {
    scopes = ["system/Patient.", "user/Patient.read"]
  }
  expect_failures = [var.scopes]
}

run "rejects_repeated_scope" {
  command = plan
  variables {
    scopes = ["system/Patient.read", "system/Patient.read"]
  }
  expect_failures = [var.scopes]
}

run "rejects_no_scopes" {
  command = plan
  variables {
    scopes = []
  }
  expect_failures = [var.scopes]
}
