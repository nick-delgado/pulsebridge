# Module `openemr`

OpenEMR, the "clinic" PulseBridge shares readings with, running as a container on the edge VM
(task T23), plus the **OAuth2 client** that `fhir-proxy` uses to write to OpenEMR's FHIR API.

**This module creates no cloud resources.** It renders two **boot hooks** (bash scripts) and a few
settings, and the env root (T25) passes them to `edge-vm`, which stores the hooks in the VM's
metadata and runs them on every boot. Everything else (the VM, its disk, the firewall rule, the
service accounts, the secrets) belongs to other modules.

```
module identity ──secret IDs──┐
module project-base ─mirror URL, HTTPS port──┐
module edge-vm ──internal_ip (reserved address)──> module openemr ──hooks, secret_files, timeout──> module edge-vm
                                                        │
                                                        └──URLs, scopes, key secret──> module phr (fhir-proxy, T22/T15)

On the VM, every boot, after MariaDB is healthy:
  hook "openemr"               pull by digest -> TLS cert -> metadata block -> container -> readyz -> settings check
  hook "openemr-oauth-client"  key pair -> register -> Secret Manager version -> enable client -> remove key copies
```

## Terms used here

- **OpenEMR**: an open-source electronic medical record system (PHP and MariaDB), ONC-certified,
  with a FHIR R4 API. Here it plays the clinic; only its FHIR API is used.
- **Boot hook**: edge-vm's seam for this module: `{ name = bash script }`, stored as instance
  metadata `pulsebridge-hook-<name>` and run as root on every boot after MariaDB is healthy, under
  a time limit (edge-vm README, "The seam with T23").
- **OAuth2 client**: a program registered with OpenEMR's authorization server so it can ask for
  access tokens. OpenEMR gives it a **client ID** at registration.
- **Client credentials grant**: the OAuth2 flow for server-to-server calls: the client proves its
  own identity and gets a token; no person logs in.
- **`private_key_jwt`**: how the client proves its identity: it signs a short JSON Web Token (an
  "assertion") with its **private key**; OpenEMR checks the signature against the **public key**
  registered with the client. There is no shared password to leak, only a private key that never
  leaves the VM except into Secret Manager.
- **JWKS** (JSON Web Key Set, RFC 7517): the JSON form of public keys. **`kid`** (key ID) names a
  key; here it is the key's **RFC 7638 thumbprint** (a SHA-256 of its public numbers), so anyone
  holding the key can recompute it.
- **Scope**: one permission a token carries, such as `system/Observation.write`. `system/` means
  "on behalf of a backend system", not a user or a patient.
- **`site_addr_oath`**: an OpenEMR setting (a row in its `globals` table) holding its own public
  base URL. OpenEMR derives its OAuth2 issuer and the audience it expects in an assertion from it.
- **Self-signed certificate / pinning**: a TLS certificate not issued by a public authority.
  A client can still verify it fully by trusting exactly that one certificate ("pinning") instead
  of the usual public authorities.
- **Bind mount**: a host directory or file made visible inside a container
  (`--mount type=bind,source=...,target=...`).
- **Metadata server** (169.254.169.254): the VM-local endpoint that hands out access tokens for
  the VM's service account to anything on the VM that asks, containers included.
- **`DOCKER-USER`**: the iptables chain Docker reserves for the administrator's own rules on
  container traffic, evaluated before Docker's rules.

## What this module is, and why it has no resources

The brief lists `infra/modules/openemr/` as "OpenEMR container on the edge VM, DB connection,
registered OAuth2 client (via provisioning script)". The VM is `edge-vm`'s, and edge-vm takes
`hooks` and `secret_files` as inputs. A module here that **created** something on the VM (a
metadata entry, a guest attribute, a startup step) would need edge-vm's instance, while edge-vm
needs this module's hooks: a dependency cycle. So this module only **produces values**:

| Output | Goes to | What it is |
| --- | --- | --- |
| `edge_vm_hooks` | edge-vm `hooks` | The two hook scripts |
| `edge_vm_secret_files` | merged into edge-vm `secret_files` | The two OpenEMR password secrets (IDs, not values) |
| `hook_timeout_minutes` | edge-vm `hook_timeout_minutes` | 40: what OpenEMR's first boot needs |
| `site_address`, `fhir_base_url`, `token_url`, `scopes`, `client_secret_id` | phr / fhir-proxy (T22, T15) | How to reach OpenEMR and where its client credentials are |

The only input from edge-vm is `internal_ip`, which comes from edge-vm's **reserved address**
resource, not from the instance, so Terraform's graph has no cycle (module inputs and outputs are
separate graph nodes: address -> `internal_ip` -> these hooks -> the instance's metadata). With no
provider at all, nothing in this module can call a Google API.

Considered and rejected: a module that creates the VM's OpenEMR resources itself (the cycle above);
moving OpenEMR into edge-vm (T21 deliberately left it as a seam, and edge-vm is done and reviewed).

## Usage

How T25's env root wires it (output names are the real ones of the other modules):

```hcl
module "openemr" {
  source = "../../modules/openemr"

  internal_ip          = module.edge_vm.internal_ip                 # from the reserved address
  https_port           = module.project_base.openemr_https_port     # the port its firewall rule opens
  dockerhub_mirror_url = module.project_base.dockerhub_mirror_url   # create_dockerhub_mirror stays true
  secret_ids = {
    db_password        = module.identity.secret_resource_ids["openemr-db-password"]
    admin_password     = module.identity.secret_resource_ids["openemr-admin-password"]
    client_private_key = module.identity.secret_resource_ids["openemr-client-private-key"]
  }
}

module "edge_vm" {
  source = "../../modules/edge-vm"
  # ... as in the edge-vm README ...

  secret_files = merge(
    { "mariadb-root-password" = module.identity.secret_resource_ids["db-root-password"] },
    module.openemr.edge_vm_secret_files,
  )
  hooks                = module.openemr.edge_vm_hooks
  hook_timeout_minutes = module.openemr.hook_timeout_minutes # 40; edge-vm's default 30 is too short
}
```

Every other input has a default. `identity`'s `edge_vm_writes_openemr_key` must stay `true` (its
default): it lets the VM's account add versions to the key secret and read version metadata.

## OpenEMR here, and why it is not exposed

OpenEMR receives the vitals PulseBridge shares and gives them back through its FHIR API for the
dashboard's "As stored in OpenEMR" panel (decision D13). Its screens are **not exposed**:

- the VM has no external IP; OpenEMR's port is published only on the VM's internal address;
- project-base's one firewall rule admits only the serverless subnet (Cloud Run's Direct VPC
  egress, so in practice `fhir-proxy`) to that port (D14);
- port 80 is not published at all, and there is no public switch;
- **no reviewer account** is created. The image's first-time setup requires one administrator
  (`OE_USER`/`OE_PASS`): it is `admin`, with the password from the `openemr-admin-password`
  secret, and it cannot be reached from the internet. Reviewers see OpenEMR's chart as a screenshot
  in the README (T28).

OpenEMR's dynamic client registration endpoint is unauthenticated (spike S1): anyone who can reach
the port can register a client. Such a client stays **disabled** until someone with database
access enables it, and only the serverless subnet can reach the port.

## What the hooks do on every boot

edge-vm runs hooks in name order, each under `timeout` (`hook_timeout_minutes`), after the data disk
is mounted, the secrets are copied and MariaDB is healthy. Each hook also stops itself a minute
before that limit, with a message saying what it was waiting for.

### Hook `openemr` (the container)

1. **Refuse unless safe**: the data disk is mounted (OpenEMR's site files and database live there),
   MariaDB's health check says `healthy` (spike S1: OpenEMR exits with "unable to connect to
   database as root" if started too early), and the three password files exist. Otherwise it
   exits with an error naming what is missing, and OpenEMR is not started.
2. **Pull** `<mirror>/openemr/openemr:8.5.0-2026-11-12@sha256:f9536d0b...` unless present, each
   attempt under `timeout` (at most 15 minutes), retried until the deadline.
3. **Seed the sites directory** once: a bind mount, unlike an empty Docker volume, does not receive
   the image's files, so on first boot the image's `sites/` is copied onto the data disk (into a
   temporary directory, then moved into place, so an interrupted copy is never mistaken for a
   seeded one).
4. **TLS certificate**: generated once for the VM's address (see "TLS"), kept on the data disk.
5. **Apache settings**: spike S2's `apache-lowmem.conf` (four workers) written to the state
   directory and mounted read-only as `/etc/apache2/conf.d/zz-lowmem.conf`.
6. **Block the metadata server** for the edge Docker network (see "Metadata server").
7. **Start the container**, with edge-vm's label-hash pattern: same configuration, leave it (or
   start it if stopped); changed image, flags, certificate or Apache file: `docker stop --time 60`
   (SIGTERM, then up to 60 s), `docker rm` without `--force`, `docker run` again:

   ```bash
   docker run --detach --name openemr --label pulsebridge.config=<hash> \
     --restart unless-stopped --stop-timeout 60 --network pulsebridge-edge \
     --publish 443:443 \
     --mount type=bind,source=/mnt/disks/edge/openemr,target=/var/www/localhost/htdocs/openemr/sites \
     --mount type=bind,source=/var/lib/pulsebridge-edge/openemr/apache-lowmem.conf,target=/etc/apache2/conf.d/zz-lowmem.conf,readonly \
     --mount type=bind,source=/mnt/disks/edge/openemr-tls/server.crt,target=/etc/ssl/certs/selfsigned.cert.pem,readonly \
     --mount type=bind,source=/mnt/disks/edge/openemr-tls/server.key,target=/etc/ssl/private/selfsigned.key.pem,readonly \
     --label pulsebridge.tls-sha256=<hash> --label pulsebridge.apache-sha256=<hash> \
     --env MYSQL_HOST=mariadb --env MYSQL_ROOT_PASS --env MYSQL_USER=openemr --env MYSQL_PASS \
     --env OE_USER=admin --env OE_PASS \
     --env OPENEMR_SETTING_rest_fhir_api=1 --env OPENEMR_SETTING_rest_system_scopes_api=1 \
     --env OPENEMR_SETTING_site_addr_oath=https://<internal_ip> \
     --log-opt max-size=10m --log-opt max-file=3 \
     <mirror>/openemr/openemr:8.5.0-2026-11-12@sha256:f9536d0b...
   ```

   `--env NAME` without `=value` copies the value from the hook's environment, so no password is
   a command-line argument. Bind mounts use `--mount type=bind`, never `-v` (which would silently
   create a missing source directory, and OpenEMR would run first-time setup on it).
8. **Wait for readiness**: `GET https://<internal_ip>/meta/health/readyz` until `200`, every 10 s,
   each request at most 30 s, verified against OpenEMR's certificate (`--cacert`, never `-k`).
9. **Check the settings** in the database. Spike S1 saw the image apply `OPENEMR_SETTING_*` on
   every start; spike S2 saw no effect and wrote the `globals` table. So the hook reads
   `rest_fhir_api`, `rest_system_scopes_api`, `site_addr_oath` and `rest_api` back and, for any
   that is wrong, writes it (S2's `REPLACE INTO globals (gl_name, gl_index, gl_value) ...`),
   reads again, and logs either `OPENEMR_SETTING_* took effect` or `applied through the database:
   <names>`. `rest_api` (the standard, non-FHIR REST API) is not needed (S1) and is set back to `0`
   if it is ever on. Finally `GET /apis/default/fhir/metadata` must answer `200` (it answers "API
   is disabled" when `rest_fhir_api` is off).

### Hook `openemr-oauth-client` (the registration)

Spike S1's recipe, made safe to repeat. On each boot it first checks, in this order, whether the
client recorded on the data disk (`/mnt/disks/edge/openemr-client/state`, no secret in it) is still
usable:

| Check | If it fails |
| --- | --- |
| state file says `done` | first boot, or an earlier attempt stopped half-way: register |
| the client is in `oauth_clients` with `is_enabled = 1` | database recreated (row gone) or client revoked: register a new one |
| OpenEMR's certificate is the one stored with the client | certificate replaced: register again, so fhir-proxy gets the new certificate with a matching key |
| Secret Manager's `latest` version of the key secret is ENABLED and is the version recorded | no version, destroyed, disabled, or someone added another: register again. The VM cannot re-upload the old key (it removed its copy, and it cannot read the secret) |
| the metadata read itself fails (403, timeout) | **not** a reason to register: it logs a warning and trusts the state file |

If everything holds, it logs `nothing to do`. Otherwise:

1. **Generate an RSA 2048 key pair on the VM**, in a fresh mode-700 directory under `/run`
   (memory-backed on COS, so the key never touches a disk), in a throwaway container of the
   OpenEMR image with **no network** (`docker run --rm --network none --entrypoint php`). COS has no
   PHP and may have no `openssl` command; the OpenEMR image is already pulled and pinned, and it
   certainly has PHP with the openssl extension (OpenEMR's own OAuth2 server needs it), so no extra
   image is pulled. The helper is `scripts/client-tools.php`.
2. **Register** with `POST /oauth2/default/registration` (S1 step 4):

   ```json
   {"application_type": "private", "client_name": "pulsebridge-fhir-proxy",
    "redirect_uris": ["https://<internal_ip>/pulsebridge-unused-redirect"],
    "token_endpoint_auth_method": "private_key_jwt", "grant_types": ["client_credentials"],
    "scope": "system/Patient.read system/Patient.write system/Encounter.read system/Encounter.write system/Observation.read system/Observation.write",
    "jwks": {"keys": [{"kty": "RSA", "kid": "<RFC 7638 thumbprint>", "use": "sig", "alg": "RS384", "n": "...", "e": "AQAB"}]}}
   ```

   OpenEMR answers with a `client_id` (it generates it; the registrant cannot choose one) and also a
   `client_secret` and `registration_access_token` that this flow never uses. The response is read
   from a mode-600 file and deleted, never logged. OpenEMR stores the client **disabled**.
3. **Store in Secret Manager** (`POST .../secrets/<key secret>:addVersion`) as the VM's account,
   which may add versions but not read them. The token goes to curl on standard input, the body
   from a mode-600 file, so neither is in the process list. Retried on 5xx and timeouts until the
   deadline (a duplicate version after a lost response is identical, so harmless); a 4xx is not
   retried.
4. **Only then enable the client** (S1 step 6: `UPDATE oauth_clients SET is_enabled = 1 ...`),
   check it reads back as `1`, and **revoke** (disable, not delete) earlier clients with the same
   `client_name`, so exactly one PulseBridge client can get tokens.
5. **Record** `status=done`, the client ID, the version number, the certificate hash and the key
   ID; write the client ID to `/mnt/disks/edge/openemr-client/client_id`; remove every key copy
   (`shred -u`, then the directory). The `EXIT`/`TERM` traps remove it on any failure or timeout.

**Failure handling.** If the upload fails, the new client is **deleted while still disabled** and
the state file removed, so there is never an enabled client without a stored key; the next boot
(or `sudo google_metadata_script_runner startup`) registers from scratch. If the hook stops after
registering but before enabling, the state says `registered`, and the next boot deletes that
disabled client and starts again. If enabling fails after the upload, the key's version is the
latest but its client is disabled: fhir-proxy gets `invalid_client` until the next boot replaces
both (logged as an error).

**Database access.** The hook connects as **OpenEMR's own database user** (`openemr`), not root,
through the MariaDB container's client: `docker exec -i --env MYSQL_PWD mariadb mariadb
--protocol=TCP --host=127.0.0.1 --user=openemr ... openemr`, SQL on standard input. `MYSQL_PWD` is
copied from the hook's environment, never an argument. The `mariadb` client has no bound
parameters, so every value placed in a SQL string is first checked to contain only
`[A-Za-z0-9:/._-]` (the client ID, the client name and the settings), which needs no escaping.

## What fhir-proxy gets (for T15 and T22)

The client ID is generated on the VM at registration, so it cannot be a Terraform value. It
reaches fhir-proxy **in the same Secret Manager version as the private key**, as one JSON document:

```json
{
  "format": "pulsebridge.openemr-client.v1",
  "client_id": "<generated by OpenEMR>",
  "token_endpoint_auth_method": "private_key_jwt",
  "kid": "<RFC 7638 thumbprint of the key>",
  "alg": "RS384",
  "private_key_pem": "<the private key, PEM-encoded PKCS#8>",
  "site_addr_oath": "https://<internal_ip>",
  "token_url": "https://<internal_ip>/oauth2/default/token",
  "fhir_base_url": "https://<internal_ip>/apis/default/fhir",
  "scope": "system/Patient.read ... system/Observation.write",
  "server_certificate_pem": "<OpenEMR's TLS certificate, PEM>",
  "created": "2026-11-15T12:00:00Z"
}
```

Why one document and not a second secret or a file the deployer copies:

- **A matching set, always.** A new registration (database recreated, certificate replaced)
  changes the key, the client ID and possibly the certificate together. In one version they
  change atomically; with two secrets the proxy could pair the new key with the old ID and get
  `invalid_client` until both caught up.
- **No identity change and no manual step.** The `openemr-client-private-key` secret, the VM's
  `secretVersionAdder` and `viewer` grants and fhir-proxy's `secretAccessor` grant already exist.
  A deployer-copied client ID would need a second `apply` after every first boot and after every
  database wipe.
- Rejected: a fixed client ID (OpenEMR generates it; overwriting the primary key in its table is
  unsupported surgery); guest attributes or metadata written by the VM (needs an edge-vm change,
  and fhir-proxy would need Compute permissions); a second "plain" secret (above).

**The client ID is still plain configuration**: nothing treats it as secret. It is logged on the
VM, written to `client_id` on the data disk, and fhir-proxy may show it in traces. Only
`private_key_pem` is secret; the trace must never contain it, the signed assertion or the token
(T15 AC).

What **T15** does with it:

- Read the mounted file; refuse a document whose `format` is unknown, or whose `site_addr_oath`
  differs from the proxy's configured `OPENEMR_SITE_ADDRESS` (a clear error beats an `invalid_client`).
- Sign the assertion with `private_key_pem`, algorithm `RS384`, header `kid`, claims `iss` = `sub`
  = `client_id`, `aud` = `token_url` (S1: any other audience is refused), `exp` about 60 s ahead,
  a fresh `jti`.
- `POST token_url` with `grant_type=client_credentials`, `scope` = the six scopes,
  `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`. Tokens last
  **300 seconds** with no refresh token (S1); cache for that long.
- On `invalid_client`, re-read the file once (a new registration may have just replaced it) before
  reporting OpenEMR as misconfigured.
- Verify TLS as below.

What **T22** does: mount `client_secret_id` (version `latest`) as a file in fhir-proxy, like the
reviewer password (so a new version takes effect without a redeploy), and set as plain environment
variables `OPENEMR_SITE_ADDRESS` = `site_address`, `OPENEMR_TOKEN_URL` = `token_url`,
`OPENEMR_FHIR_BASE_URL` = `fhir_base_url`, `OPENEMR_SCOPES` = `scopes` (names are T15's to choose;
these are suggestions). Direct VPC egress on the serverless subnet (already in T22's AC) is what
lets the proxy reach the VM's internal address.

## TLS: how fhir-proxy verifies OpenEMR

OpenEMR's image creates its own self-signed certificate for `localhost` when the container is
created. It would change on every container replacement and does not name the VM's address, so a
client that verifies properly would reject it, and the only ways around that weaken TLS. So:

- The `openemr` hook generates **one certificate for the VM's address** (subject alternative name
  `IP:<internal_ip>`), valid `tls_certificate_days` (default ten years), keeps it and its key on
  the data disk (`/mnt/disks/edge/openemr-tls/`, mode 700; the key never leaves the VM), and
  bind-mounts both over the paths where the image's Apache looks (`tls_mount_targets`), so the
  image's own configuration is untouched and it skips generating one.
- The certificate travels to fhir-proxy in the client document (`server_certificate_pem`). T15
  builds its TLS context from **that certificate only** (Python: `ssl.create_default_context(cadata=...)`),
  with hostname checking **on**. Nothing else is trusted for OpenEMR, and verification is never
  switched off (T15 AC).
- Its shape is what OpenSSL's strict verification (Python 3.13's default) accepts for a
  self-signed trust anchor: `basicConstraints = critical, CA:TRUE, pathlen:0`, `keyUsage =
  critical, digitalSignature, keyEncipherment, keyCertSign`, `extendedKeyUsage = serverAuth`,
  subject and authority key identifiers. Checked here with Python 3.13.18: a handshake verified
  against it succeeds for its IP and fails for another.
- The hooks themselves call OpenEMR at `https://<internal_ip>` with `--cacert <that certificate>`,
  exactly as the proxy will, so a wrong `tls_mount_targets` shows on the first boot as
  `TLS verification of OpenEMR failed`.
- Rotation: delete `/mnt/disks/edge/openemr-tls/` and re-run the startup script. A new certificate
  replaces the container and leads to a new client registration and secret version, so the proxy
  gets the new certificate with a matching key. The same happens if `internal_ip` changes.

## Secrets

Nothing secret is in Terraform, in the hooks, in metadata or in state:

| Secret | Where it lives | How it reaches its user | Exposure |
| --- | --- | --- | --- |
| MariaDB root password | identity `db-root-password` -> edge-vm file `mariadb-root-password` | OpenEMR's first-time setup (`MYSQL_ROOT_PASS`) creates its database and user with it | see below |
| OpenEMR DB password | `openemr-db-password` -> file of that name | `MYSQL_PASS` (OpenEMR); `MYSQL_PWD` for the hook's own SQL | see below |
| OpenEMR admin password | `openemr-admin-password` -> file of that name | `OE_PASS` (first-time setup) | see below |
| OAuth2 client private key | generated on the VM in `/run`, removed after upload | Secret Manager version of `openemr-client-private-key`; read only by fhir-proxy | never on a disk of the VM, never logged |
| TLS server key | `/mnt/disks/edge/openemr-tls/server.key`, mode 600 | bind-mounted read-only into OpenEMR | stays on the VM |

- **The passwords reach OpenEMR as environment variables**, because the image reads only
  `MYSQL_ROOT_PASS`, `MYSQL_PASS` and `OE_PASS` (it has no `*_FILE` variants). They are read from
  the root-only files at start (trailing newlines stripped) and passed by name (`--env NAME`), so
  they are never in a command line or a log. **The exposure**: they become part of the container's
  configuration, readable by root on the VM with `docker inspect openemr` (and stored by Docker on
  the boot disk, root-only). Root can read the secret files anyway, so this adds no new reader.
- No sentinel password, token, client secret or private-key line appears in the hook log, in any
  command line or in the hook text (tested).
- **For T27:** write `openemr-db-password` and `openemr-admin-password` versions without a trailing
  newline (`printf '%s'`). Never write `openemr-client-private-key`: the VM does.

## Metadata server

Every container on the VM can ask the metadata server for an access token of the VM's account
(edge-vm README, "Trust boundary"). OpenEMR is a large PHP application reachable from fhir-proxy,
so the `openemr` hook adds, before it starts OpenEMR:

```bash
iptables -w 10 -I DOCKER-USER -i br-<edge network id, 12 characters> -d 169.254.169.254/32 -j DROP
```

- **Matches the network, not an address.** The rule matches packets arriving from the edge Docker
  network's bridge, so it holds whatever IP Docker gives the container after a restart. It covers
  OpenEMR and MariaDB (which never needs the metadata server). The **MLLP adapter** is on Docker's
  default bridge and keeps its token; the **VM itself** (the startup script, docker-credential-gcr)
  is not affected, because host traffic does not pass the `FORWARD` chain that `DOCKER-USER` hangs
  off. Name resolution for containers goes through Docker's embedded DNS, which the daemon answers
  from the host, so it is not blocked either.
- **Idempotent**: `iptables -C` first; added once.
- **Survives Docker restarts**: Docker does not flush `DOCKER-USER` when its daemon restarts.
  **A reboot empties iptables**; the hook adds the rule again before it starts OpenEMR, and Docker
  cannot start OpenEMR earlier because its bind mounts live on the data disk, which is not mounted
  until the startup script runs.
- If the rule cannot be added, OpenEMR is **not started**. `block_metadata_server = false`
  switches it off for debugging, with a warning on every boot. It cannot be proven without a real
  VM, so it is on the first-boot checklist.

## Memory on the e2-micro

From spike S2, with Apache capped at four workers (required: stock settings grew to 38 PHP workers
and 223 MB of swap under a burst; capped, 5 processes and 36 to 67 MB):

| Container | `docker stats` memory | Private memory |
| --- | --- | --- |
| OpenEMR (Apache + PHP, 3 `httpd` at idle) | 100 to 128 MB | 18 to 25 MB |
| MariaDB (64 MB buffer pool) | 119 to 179 MB | 95 to 152 MB |
| MLLP adapter | 19 to 29 MB | 16 to 21 MB |

The VM (969 MB) had **216 to 330 MB available** at idle with all three, no OOM kill in any run, 0 to
140 MB of edge-vm's 2 GB swap in use. The real limit is the shared CPU: after about 20 s of
sustained load responses slow about eightfold. The hooks' throwaway PHP containers run for a second
or two, only when a certificate or a client is made.

## First boot: timeline and time limit

| Step | Time (spike S2 on an e2-micro, unless noted) |
| --- | --- |
| edge-vm: disk, swap, Docker, adapter, MariaDB healthy | about 1 to 3 minutes |
| Hook `openemr`: image pull (3.6 GB, through the mirror) | **about 8 minutes** (489 s) |
| sites seed, certificate, Apache file, rule, `docker run` | seconds (not measured) |
| OpenEMR first-time setup until `readyz` is 200 | **about 5 minutes** (269 to 331 s from start) |
| settings check, FHIR metadata | seconds |
| Hook `openemr-oauth-client` | under a minute (not measured) |

So **about 15 minutes** from boot to a registered client, and the hooks need edge-vm's
`hook_timeout_minutes = 40` (output `hook_timeout_minutes`): edge-vm's default 30 leaves too little
room for a slow pull on a CPU that throttles under sustained load. Later boots: no pull, no setup;
S2 measured 43 s to ready after a reboot. After a VM replacement (patching): the image is pulled
again (about 8 minutes), no setup, and the client is unchanged.

## For other tasks

- **T25** wires the module as in "Usage", passes `hook_timeout_minutes`, keeps
  `create_dockerhub_mirror` and identity's `edge_vm_writes_openemr_key` on, and works through
  "Not verified" below on the first real boot, after edge-vm's own checklist.
- **T22** mounts `client_secret_id` and sets the URLs and scopes ("What fhir-proxy gets").
- **T15** reads the client document, signs `private_key_jwt` assertions with `aud = token_url`,
  caches tokens for 300 s, and trusts only `server_certificate_pem` with hostname checking on.
- **T27** writes the two OpenEMR password versions without a trailing newline, and never the key.
- **identity** (not edited here): no new secret and no new grant is needed. The `purpose` of
  `openemr-client-private-key` says "Private key (PEM)"; it should say "OpenEMR client document
  (JSON): private key, client ID, key ID and OpenEMR's certificate (T23 README)", and its README row
  likewise.
- **edge-vm, project-base**: no change needed. The firewall rule already opens `openemr_https_port`.

## Cost

Nothing new is billed. OpenEMR runs on the existing e2-micro and its data on the existing data disk
(a fresh OpenEMR database is a few hundred MB). The one new storage cost is the **cached OpenEMR
image in the Docker Hub mirror** (Artifact Registry, $0.10 per GB-month beyond 0.5 GB free): the
image is 3.6 GB unpacked, less as compressed layers (not measured), so a few tens of cents a month
at most. The image on the VM's boot disk fits in edge-vm's 20 GB. Nothing here is billed by the hour.

## Tests

```bash
make tf-test    # runs everything below with Google credentials removed from the environment
```

- `tests/openemr.tftest.hcl` (`terraform test`, `command = plan`; no provider to mock): the image
  is pinned by digest through the mirror; site address, token and FHIR URLs (also with a non-default
  port); exactly the six scopes; exactly one `--publish` (`<port>:443`), nothing for 80; the three
  `OPENEMR_SETTING_*` values, `rest_api` never 1 and enforced at 0; the hook names, metadata keys
  and secret files edge-vm expects; each hook under 64 KB; no password given a value on a command
  line, no key material, no tracing, no `--insecure`, no secret version; the metadata-block switch;
  and every validation (a tag instead of a digest, a hostname or bad IP, a bad port, a secret with
  a version, a bad mirror, a quote in the client name, a bad database user, a short hook timeout,
  a long certificate, a quote in a mount path).
- `tests/static_checks.sh`: no resource, data source, module or provider block (so no IAM and no
  firewall resource); no hardcoded project; no password-, secret- or token-looking literal and no
  PEM private-key header; no `set -x`; no `docker -v`/`--volume`/`-p` and no `curl -k`/`--insecure`;
  exactly one `--publish`; no secret-named variable.
- `tests/hooks.sh`: renders both hooks with `terraform console`, so the scripts tested are exactly
  what edge-vm stores; shellcheck `-S style` on them (shellcheck from PATH or `$SHELLCHECK`;
  required when `CI=true`, clean with 0.9.0, CI's version) and `php -l`; then runs them in a
  sandbox with PATH holding only fakes (`tests/fakes/fake-command` as `docker`, `curl`,
  `iptables`, `timeout`, `date`, `sleep`, `shred`) and a faked clock. The PHP helper runs for real,
  so the key, the JWKS and the certificate checked are real (php, openssl and python3 required in
  CI; the ubuntu-24.04 runner has them). Cases:
  - first boot: the exact `docker run` arguments; the passwords arrive in the container with
    trailing newlines stripped but are never arguments; the rule is added once and before OpenEMR
    is created; the sites seed; the certificate's SAN; every HTTPS call verifies with `--cacert`;
    the registration body (private client, `private_key_jwt`, `client_credentials` only, the six
    scopes, a public-only JWKS whose `kid` is its RFC 7638 thumbprint); one upload; the uploaded
    key belongs to the registered public key, and the document's client ID, URLs and certificate
    match; one enabled client; no sentinel in the log, the call log or the hooks; no copy of the
    key left anywhere in the sandbox;
  - idempotency: a second run starts, stops, pulls, registers and uploads nothing and leaves the
    database and state unchanged; a reboot re-adds the rule before `docker start` and creates
    nothing;
  - re-registration: a replaced certificate (graceful `docker stop --time 60`, `docker rm` without
    `--force`, new client, old one revoked); a wiped database; destroyed secret versions; a refused
    metadata read does **not** re-register;
  - settings: variables ignored by the image (S2) are written to the database, and not again on the
    next run; `rest_api` found on is set to 0;
  - refusals: MariaDB unhealthy, data disk not mounted, a password file missing, iptables failing:
    OpenEMR is never created or started and no client is registered; the registration hook without
    OpenEMR; a hook run without edge-vm's environment;
  - time: OpenEMR never ready, every curl hanging, `docker pull` hanging, the database hanging, the
    wrong certificate served (reported once, no fallback to `--insecure`): each ends within the hook
    limit and no call ran without a time limit;
  - upload failures (403 not retried, 503 retried to the deadline): no enabled client, the new
    client deleted, no state file, no key left; the next boot succeeds.

## Not verified

Nothing here has run on a real VM: no OpenEMR container, registration or Secret Manager call has
happened, and the hooks have run only against fakes. Verify at T25, **after edge-vm's own
checklist**, in this order (each step depends on the ones before):

1. **The hooks run**: the startup log shows `pulsebridge-openemr[openemr]: begin` after `MariaDB is
   healthy`, and `hook openemr: ok` / `hook openemr-oauth-client: ok` at the end; metadata has
   `pulsebridge-hook-openemr` and `pulsebridge-hook-openemr-oauth-client`.
2. **Commands on COS** the hooks use from the host: `awk`, `cat`, `chmod`, `curl`, `date`, `docker`,
   `grep`, `head`, `iptables`, `ls`, `mkdir`, `mktemp`, `mv`, `rm`, `rmdir`, `sed`, `sha256sum`, `sleep`,
   `timeout` (`shred` optional). `/run` is writable and memory-backed.
3. **The pull** of the OpenEMR digest through the mirror succeeds within the deadline (S2: 489 s
   from Docker Hub directly; through the mirror not measured).
4. **PHP in the image**: the certificate step succeeds, so `php` is on the image's PATH with the
   openssl extension (`docker run --rm --entrypoint php <image> -m` lists `openssl`), and runs as
   `--user 0:0` with `--network none`.
5. **The sites seed**: `/var/www/localhost/htdocs/openemr/sites` is the image's path (S2's compose
   file mounted it), `cp -a` keeps ownership, and OpenEMR's first-time setup completes on the
   bind-mounted directory (`docker logs openemr`). This is the image's documented volume path, but
   seeding a bind mount was not tried.
6. **The certificate paths**: `readyz` answers through `--cacert`, i.e. OpenEMR serves our
   certificate. The defaults of `tls_mount_targets` are the paths of the image's `ssl.sh` as
   recalled (its upstream source could not be fetched here). If the hook logs `TLS verification of
   OpenEMR failed`, find the real ones (`docker exec openemr grep -ri sslcertificate /etc/apache2`)
   and set `tls_mount_targets`. Also check the image did not try to replace the mounted files.
7. **Reaching the published port from the VM itself** at `https://<internal_ip>` (Docker's DNAT for
   locally generated traffic). If only `127.0.0.1` works from the host, the hooks need a
   `--resolve`-style change; fhir-proxy is unaffected.
8. **The metadata block**: `sudo iptables -S DOCKER-USER` shows the rule; `docker run --rm --network
   pulsebridge-edge <any image with curl> curl -m 5 -H 'Metadata-Flavor: Google'
   http://169.254.169.254/computeMetadata/v1/` times out; the MLLP adapter still gets `AA`; the
   startup script still reads secrets. COS's Docker uses the iptables (not nftables) backend and
   creates `DOCKER-USER`; the bridge is named `br-<12 characters of the network ID>`.
9. **Apache capped**: `docker exec openemr ps` shows at most 5 `httpd` under a burst, and
   `zz-lowmem.conf` is loaded after the image's `mpm.conf`.
10. **Memory**: `free -m` shows about 200 MB or more available at idle with all three containers
    (S2: 216 to 330 MB).
11. **The settings**: which log line appeared (`took effect` or `applied through the database`);
    record it in this README. `globals` holds the four values; `GET /apis/default/fhir/metadata` is 200.
12. **The database connection**: `docker exec ... mariadb --protocol=TCP --host=127.0.0.1
    --user=openemr` works (OpenEMR creates its user as `'openemr'@'%'`, assumed; MariaDB 11.8's
    client TLS defaults accepted on loopback), and `oauth_clients` has the columns used
    (`client_id`, `client_name`, `is_enabled`).
13. **The registration**: `200` with a `client_id`; the client is enabled; a **token request from
    fhir-proxy** (T15) succeeds with the RS384 assertion and the thumbprint `kid`.
14. **Secret Manager**: `addVersion` works with `secretVersionAdder`; `GET versions/latest` works
    with `viewer` and reports `state`; what `latest` resolves to when the newest version is disabled
    (either way the hook re-registers).
15. **fhir-proxy's TLS** from Cloud Run: Python's default context with `cadata` = the certificate
    and hostname checking on accepts `https://<internal_ip>` (checked here only locally, Python
    3.13.18).
16. **A reboot**: the registration hook logs `nothing to do`, the container hook starts the existing
    container after re-adding the rule, and nothing is pulled, created or registered.
17. **Time**: first boot within `hook_timeout_minutes = 40`; record the real pull and setup times.
18. **A database wipe** (in dev): stop OpenEMR, recreate the database, boot: a new client is
    registered and fhir-proxy recovers after re-reading its secret file.
19. **The admin password**: OpenEMR's first-time setup accepts the `openemr-admin-password` value
    (any password policy at setup is not known).
20. **`docker inspect` exposure** of the three passwords is acceptable for this deployment (root
    only).

Other assumptions about other modules (exact names): `edge-vm.internal_ip`, its hook environment
(`EDGE_DATA_DIR`, `EDGE_SECRETS_DIR`, `EDGE_OPENEMR_DIR`, `EDGE_STATE_DIR`, `EDGE_DOCKER_NETWORK`,
`EDGE_MARIADB_CONTAINER`, `EDGE_HOOK_TIMEOUT_SECONDS`, `HOME`, `DOCKER_CONFIG`), its secret file
`mariadb-root-password`, the MariaDB container's `mariadb` client and health check, hooks run in
name order with fd 9 closed; `project-base.dockerhub_mirror_url` (non-null) and
`openemr_https_port`; `identity.secret_resource_ids["openemr-db-password"]`,
`["openemr-admin-password"]`, `["openemr-client-private-key"]`, with edge-vm reading the first two
and adding versions to and viewing the third, and fhir-proxy reading the third.

## Inputs and outputs

| Input | Default | Meaning |
| --- | --- | --- |
| `internal_ip` | (required) | edge-vm `internal_ip`; OpenEMR's address, site address and certificate SAN |
| `https_port` | `443` | project-base `openemr_https_port`; published as `<port>:443` |
| `dockerhub_mirror_url` | (required) | project-base `dockerhub_mirror_url` |
| `openemr_image` | `openemr/openemr:8.5.0-2026-11-12@sha256:f9536d0b...` | path in the mirror, by digest (S1/S2) |
| `secret_ids` | (required) | identity `secret_resource_ids` for the two passwords and the client key |
| `db_user` | `openemr` | OpenEMR's MariaDB user |
| `client_name` | `pulsebridge-fhir-proxy` | name of the OAuth2 client |
| `block_metadata_server` | `true` | the DOCKER-USER rule |
| `tls_certificate_days` | `3650` | validity of OpenEMR's certificate |
| `tls_mount_targets` | the image's `selfsigned.*.pem` paths | where the certificate and key are mounted |
| `hook_timeout_minutes` | `40` | passed through to edge-vm |

Outputs: `edge_vm_hooks`, `edge_vm_secret_files`, `hook_timeout_minutes`, `hook_metadata_keys`,
`site_address`, `fhir_base_url`, `token_url`, `scopes`, `client_secret_id`, `image`,
`openemr_settings`, `container_name` (descriptions in `outputs.tf`).

To find the digest of a newer OpenEMR tag without pulling it: `crane digest openemr/openemr:<tag>`
or `docker buildx imagetools inspect openemr/openemr:<tag>`. OpenEMR rebuilds its tags daily
(S1), so test a new digest before changing `openemr_image`.
