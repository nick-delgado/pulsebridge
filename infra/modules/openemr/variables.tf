# Every value here ends up in a hook script that is stored in instance metadata, which anyone who can
# view the VM can read. So no input carries a secret: secrets are named by their Secret Manager
# resource ID and read on the VM from the files edge-vm copies them to. Every string that reaches
# the scripts is limited by a validation to characters without quotes, so single-quoting them in the
# rendered configuration block is safe (and a precondition in main.tf checks it again).

variable "internal_ip" {
  description = "Internal IPv4 address of the edge VM (edge-vm output `internal_ip`, from its reserved address). OpenEMR's site address, the URL fhir-proxy calls and the address in OpenEMR's TLS certificate are all https://<this address>. Taking it from the reserved address, not from the instance, is what keeps this module free of a dependency cycle with edge-vm, which takes this module's hooks."
  type        = string

  validation {
    condition     = can(cidrhost("${var.internal_ip}/32", 0)) && can(regex("^[0-9]{1,3}(\\.[0-9]{1,3}){3}$", var.internal_ip))
    error_message = "internal_ip must be an IPv4 address such as 10.10.0.10."
  }
}

variable "https_port" {
  description = "Port OpenEMR's HTTPS is published on at internal_ip (project-base output `openemr_https_port`, the port its firewall rule opens from the serverless subnet). Inside the container Apache always listens on 443; only 443 is published, never 80."
  type        = number
  default     = 443

  validation {
    condition     = var.https_port >= 1 && var.https_port <= 65535 && floor(var.https_port) == var.https_port
    error_message = "https_port must be a whole number from 1 to 65535."
  }
}

variable "dockerhub_mirror_url" {
  description = "Registry path of the Docker Hub pull-through cache (project-base output `dockerhub_mirror_url`, <location>-docker.pkg.dev/<project>/<repository>). The VM has no internet route, so the OpenEMR image is pulled through it, as the VM's own account (read access granted by project-base)."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9-]+-docker\\.pkg\\.dev/[a-z][a-z0-9-]{4,28}[a-z0-9]/[a-z][a-z0-9-]{0,61}[a-z0-9]$", var.dockerhub_mirror_url))
    error_message = "dockerhub_mirror_url must look like <location>-docker.pkg.dev/<project>/<repository>."
  }
}

variable "openemr_image" {
  description = <<-EOT
    OpenEMR image path inside the Docker Hub mirror, pinned by digest (a tag before the digest is
    kept for readability only; the digest decides what is pulled). Default: the image spikes S1 and
    S2 tested, openemr/openemr:8.5.0-2026-11-12, which was also `8.5.0` and `latest` on 2026-11-13.
    OpenEMR rebuilds its tags daily, so a tag alone is refused. To find the digest of a tag without
    pulling the 3.6 GB image: `crane digest openemr/openemr:<tag>`, or
    `docker buildx imagetools inspect openemr/openemr:<tag>`, or a registry HEAD request for
    /v2/openemr/openemr/manifests/<tag> on registry-1.docker.io and read Docker-Content-Digest.
  EOT
  type        = string
  default     = "openemr/openemr:8.5.0-2026-11-12@sha256:f9536d0bcad7956d21471e73b91690215ad00ee4aed9448770e5012df15318cd"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$", var.openemr_image))
    error_message = "openemr_image must be a path inside the mirror pinned by digest, such as \"openemr/openemr:8.5.0-2026-11-12@sha256:<64 hex>\"; a tag alone is refused because OpenEMR rebuilds its tags daily."
  }
}

variable "secret_ids" {
  description = <<-EOT
    Secret Manager resource IDs (identity output `secret_resource_ids`), never values:
      db_password        = secret_resource_ids["openemr-db-password"]   (OpenEMR's own MariaDB user)
      admin_password     = secret_resource_ids["openemr-admin-password"] (the admin user the image's
                           first-time setup requires)
      client_private_key = secret_resource_ids["openemr-client-private-key"] (written by the VM: the
                           registration hook adds a version holding the OAuth2 client's private key,
                           client ID and OpenEMR's certificate; fhir-proxy reads it)
    The two passwords reach the VM through edge-vm's `secret_files` (output `edge_vm_secret_files`).
  EOT
  type = object({
    db_password        = string
    admin_password     = string
    client_private_key = string
  })

  validation {
    condition     = alltrue([for v in values(var.secret_ids) : can(regex("^projects/[a-z0-9-]+/secrets/[A-Za-z0-9_-]{1,255}$", v))])
    error_message = "Each secret_ids value must be a secret resource ID, projects/<project>/secrets/<secret id>, without a version."
  }
}

variable "db_user" {
  description = "Name of OpenEMR's own MariaDB user, created by the image's first-time setup (MYSQL_USER) with the password from the openemr-db-password secret. The registration hook also connects as this user, not as root, to enable the client and check the settings."
  type        = string
  default     = "openemr"

  validation {
    condition     = can(regex("^[a-z][a-z0-9_]{0,31}$", var.db_user))
    error_message = "db_user must be 1-32 characters: lower-case letters, digits and underscores, starting with a letter."
  }
}

variable "client_name" {
  description = "client_name of the OAuth2 client registered for fhir-proxy. The hook uses it to find and revoke earlier PulseBridge clients when it registers a new one, so keep it stable."
  type        = string
  default     = "pulsebridge-fhir-proxy"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,39}$", var.client_name))
    error_message = "client_name must be 3-40 lower-case letters, digits and hyphens, starting with a letter."
  }
}

variable "block_metadata_server" {
  description = "Add an iptables rule (DOCKER-USER chain) that drops traffic from the edge Docker network to the metadata server 169.254.169.254, so a compromised OpenEMR cannot get an access token for the VM's service account. It matches the network's bridge interface, so it covers OpenEMR (and MariaDB, which never needs the metadata server) but not the MLLP adapter, which is on Docker's default bridge and needs its token, nor the VM itself. On by default; OpenEMR is not started if the rule cannot be added. Turn off only to debug, and note it on the first-boot checklist."
  type        = bool
  default     = true
}

variable "tls_certificate_days" {
  description = "Validity in days of the self-signed certificate the hook generates for OpenEMR's HTTPS, with the VM's address as its subject alternative name. fhir-proxy trusts exactly this certificate (it pins it, see README \"TLS\"), so its lifetime is not a public-CA question; it is replaced, together with a new client registration, when its file is deleted or the address changes. Default ten years."
  type        = number
  default     = 3650

  validation {
    condition     = var.tls_certificate_days >= 30 && var.tls_certificate_days <= 3650 && floor(var.tls_certificate_days) == var.tls_certificate_days
    error_message = "tls_certificate_days must be a whole number from 30 to 3650."
  }
}

variable "tls_mount_targets" {
  description = "Where the OpenEMR image's Apache looks for its self-signed certificate and key. The hook bind-mounts its own certificate over these paths, so the image keeps its own Apache configuration and skips generating a certificate. The defaults are the paths of the image's ssl.sh as recalled, not checked against the 8.5.0 image (README, \"Not verified\"); the readiness check, which verifies OpenEMR's certificate against ours, fails loudly if they are wrong."
  type = object({
    certificate = string
    key         = string
  })
  default = {
    certificate = "/etc/ssl/certs/selfsigned.cert.pem"
    key         = "/etc/ssl/private/selfsigned.key.pem"
  }

  validation {
    condition     = alltrue([for v in values(var.tls_mount_targets) : can(regex("^/[A-Za-z0-9._/-]+$", v))])
    error_message = "tls_mount_targets must be absolute paths of letters, digits, dots, underscores, hyphens and slashes."
  }
}

variable "hook_timeout_minutes" {
  description = "The time limit each hook needs (pass this module's output `hook_timeout_minutes` to edge-vm's `hook_timeout_minutes`). First boot: about 8 minutes to pull the 3.6 GB image plus about 5 minutes of OpenEMR first-time setup (spike S2), on a CPU that slows down under sustained load. Each hook stops itself a minute before the limit with a clear error."
  type        = number
  default     = 40

  validation {
    condition     = var.hook_timeout_minutes >= 30 && var.hook_timeout_minutes <= 120 && floor(var.hook_timeout_minutes) == var.hook_timeout_minutes
    error_message = "hook_timeout_minutes must be a whole number from 30 to 120 (first boot needs about 15 minutes; edge-vm allows at most 120)."
  }
}

variable "scopes" {
  description = <<-EOT
    OAuth2 scopes the client is registered with, and fhir-proxy requests. Default: spike S1's six
    SMART v1 scopes, the only set verified end to end with client_credentials. In OpenEMR 8.5.0 a v1
    `system/<R>.write` grants create, update AND delete, and `.read` grants read and search
    (ScopePermissionObject.php: write -> cud, read -> rs), so the default allows more than the share
    needs (create, read, search). The narrower SMART v2 set to try on the first boot:
      ["system/Patient.crs", "system/Encounter.crs", "system/Observation.cr"]
    (Patient: search by identifier, create, read back; Encounter: create, search to reuse, read
    back; Observation: create, read back by id). v2 system scopes with client_credentials are not
    verified on OpenEMR 8.5.0. A change registers a new client on the next boot.
  EOT
  type        = list(string)
  # null means the default below (a caller passing null gets these, not null).
  nullable = false
  default = [
    "system/Patient.read",
    "system/Patient.write",
    "system/Encounter.read",
    "system/Encounter.write",
    "system/Observation.read",
    "system/Observation.write",
  ]

  validation {
    # system/ scopes on the three resources the share uses, with a SMART v1 suffix (read, write) or
    # a SMART v2 permission string: the letters c r u d s, each at most once, in that order
    # (OpenEMR's isOrderedCrudString). No wildcard (*), no other resource, no query constraints.
    condition = length(var.scopes) > 0 && alltrue([
      for s in var.scopes : can(regex("^system/(Patient|Encounter|Observation)\\.(read|write|c?r?u?d?s?)$", s)) && !endswith(s, ".")
    ])
    error_message = "scopes must be system/Patient, system/Encounter or system/Observation scopes ending in .read, .write or an ordered subset of cruds (for example .crs); no wildcards."
  }

  validation {
    condition     = length(distinct(var.scopes)) == length(var.scopes)
    error_message = "scopes must not repeat a scope."
  }
}
