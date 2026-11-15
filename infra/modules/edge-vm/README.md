# Module `edge-vm`

The one always-on machine in PulseBridge (task T21). It creates:

- an **e2-micro VM** running Container-Optimized OS (COS) on the private `edge` subnet, with **no
  external IP and no public port**, Shielded VM on, OS Login on, and a dedicated service account;
- a **separate persistent disk** for the MariaDB database and OpenEMR's site files, attached by a
  stable device name, which survives VM replacement and is removed by `terraform destroy`;
- a **reserved internal IP address**, so the address the cuff simulator sends MLLP to does not
  change when the VM is replaced;
- one IAM grant: **read access to the Docker Hub mirror repository** for the VM's account;
- a **startup script**, run by COS on every boot, that mounts the disk, turns on a 2 GB swap file,
  and starts Google's **MLLP adapter** and **MariaDB** as containers, then runs hook scripts
  (the seam for T23, OpenEMR).

It creates **no firewall rule** (decision D14: the rule that lets the serverless subnet reach
MLLP 2575 and HTTPS 443 lives in `project-base`), no service account (module `identity`) and no
provider or backend configuration (the env root configures those). Nothing in it carries a
password or key; see "Secrets".

Terms used below:

- **Container-Optimized OS (COS)**: Google's minimal Linux image for running containers. Docker is
  preinstalled; there is no package manager, and the root filesystem is read-only.
- **Startup script**: a script stored in the instance's metadata under the key `startup-script`;
  the guest agent runs it as root on every boot.
- **Persistent disk**: a network block device in the same zone as the VM. A separate one can be
  detached from one VM and attached to its replacement.
- **Private Google Access (PGA)**: a subnet setting that lets a VM with only an internal address
  reach Google's APIs (Artifact Registry, Secret Manager, the Healthcare API), and nothing else.
- **Access scopes**: an older, coarse per-VM limit on which Google APIs the VM's tokens may call.
  IAM roles are the real permission system.

## Usage

```hcl
module "edge_vm" {
  source = "../../modules/edge-vm"

  project_id            = var.project_id
  zone                  = var.zone                    # e.g. "us-central1-a"; inside the subnet's region
  edge_subnet_id        = module.project_base.edge_subnet_id
  network_tag           = module.project_base.edge_vm_network_tag
  mllp_port             = module.project_base.mllp_port
  dockerhub_mirror_url  = module.project_base.dockerhub_mirror_url
  service_account_email = module.identity.service_account_emails["edge-vm"]
  hl7v2_store_flags     = module.healthcare.mllp_adapter_store_flags

  secret_files = {
    "mariadb-root-password" = module.identity.secret_resource_ids["db-root-password"]
    # T23 adds, for example:
    # "openemr-db-password"    = module.identity.secret_resource_ids["openemr-db-password"]
    # "openemr-admin-password" = module.identity.secret_resource_ids["openemr-admin-password"]
  }

  # T23: hooks = module.openemr.edge_vm_hooks   (a map of name => bash script)
}

# T22 (phr): the cuff simulator's MLLP_HOST = module.edge_vm.mllp_host, port module.edge_vm.mllp_port
```

What T25 must set or wire: `project_id`, `zone`, the five module outputs above, `secret_files`
with at least `mariadb-root-password`, and later T23's `hooks` and extra `secret_files`. Every
other input has a default. Keep `name_prefix` equal to `project-base`'s. `mllp_port` must equal
`project-base`'s `mllp_port`, because that is the port its firewall rule opens.

## How the stack starts on the VM

### Why `docker run` from the startup script, and not Docker Compose

Spike S2 ran the stack with a compose file on Debian, installing Docker and the Compose plugin
from the internet. Neither works here:

- The VM has no route to the internet (no external IP, no NAT), so nothing can be installed with
  `apt` and Docker cannot come from Docker's repository. COS comes with Docker, containerd and
  cloud-init preinstalled, which is why it is the first choice (COS overview: "instances come
  pre-installed with the Docker and containerd runtimes and cloud-init").
- COS "does not include a package manager", and its root filesystem is read-only, so the Compose
  plugin cannot be installed either. The COS documentation and release notes list Docker and
  containerd versions but never a Compose plugin; COS documents only `docker run`, and its own
  deprecation note for the old container agent says: "Use the docker run commands in a startup
  script or use the cloud-init tool" (COS "Creating and configuring instances").
- Running Compose from a container image would need another pinned image, the Docker socket
  mounted into it, and its binary placed on one of the few executable writable paths. For three
  containers that is more moving parts than it saves.

So the startup script starts each container with `docker run`, and S2's compose file is **reused
as settings, not as a file**:

| S2 compose setting | Here |
| --- | --- |
| `mariadb` image by digest | `<mirror>/library/mariadb:11.8@sha256:d36f18d6...` through the Docker Hub mirror (`mariadb_image`) |
| `command: mariadbd --innodb-buffer-pool-size=64M ...` (eleven flags) | the same eleven words (`local.mariadb_command`) |
| `restart: always` | `--restart unless-stopped` (as S4 used; a container stopped by hand stays stopped) |
| `MARIADB_ROOT_PASSWORD: ${DB_ROOT_PASS}` from `.env` | `MARIADB_ROOT_PASSWORD_FILE` pointing at a mode-600 file from Secret Manager (no `.env`) |
| `dbdata` volume | bind mount of `/mnt/disks/edge/mariadb` on the separate data disk |
| health check `healthcheck.sh --su-mysql --connect --innodb_initialized`, start 2m / interval 1m / timeout 10s / retries 3 | the same, as `--health-*` options |
| `mllp` service with `network_mode: host` and `--logtostderr` | S4's `docker run` instead: bridge network with `--publish 2575:2575` and no `--logtostderr` (S4 found that flag does not exist in this build and crash-loops the adapter) |
| `openemr` service, `apache-lowmem.conf`, `depends_on: service_healthy` | **T23** (a hook that runs only after MariaDB is healthy) |

Why `startup-script` rather than cloud-init `user-data`: both run on every boot on COS ("modules
such as write_files and runcmd ... are run on every boot on Container-Optimized OS"). A plain
bash file can be shellchecked and tested in a sandbox (`tests/startup_script.sh`); YAML with an
embedded script cannot, as easily. COS supports the `startup-script` key ("You can specify a
startup script through the metadata server, using the startup-script metadata key").

Why `metadata["startup-script"]` and not the provider's `metadata_startup_script` argument: the
provider documents that `metadata_startup_script` "forces the instance to be recreated" when it
changes, while a metadata change is applied in place. A script change then takes effect on the
next boot, or at once with `sudo google_metadata_script_runner startup` on the VM.

### What the script does on every boot

The script is `templates/startup-config.sh.tftpl` (settings, rendered by `templatefile()` with
non-secret values only) followed by `scripts/edge-startup.sh` (the logic, plain bash).

1. **Mount the data disk.** It waits for `/dev/disk/by-id/google-edge-data` (Compute Engine
   creates that link from the attachment's `device_name`), and **fails loudly** if it does not
   appear. `blkid -p` decides what to do: exit 0 with `ext4` means mount it; exit 2 ("nothing
   found") means a new disk, formatted once with Google's recommended `mkfs.ext4` options; any
   other result (another filesystem type, a read error) stops the step without formatting.
   COS keeps `/etc` in memory, so there is no `/etc/fstab` across boots ("you cannot use
   /etc/fstab to automatically fsck and mount the disks on boot"); the script runs `fsck.ext4 -p`
   and mounts at `/mnt/disks/edge` itself on every boot. It also grows the filesystem if the disk
   was enlarged.
2. **Turn on swap.** A 2 GB file at `/mnt/stateful_partition/pulsebridge-swapfile` on the boot
   disk's stateful partition (writable and kept across reboots). The file is created once
   (`fallocate`, `mkswap`) and `swapon` is run on every boot, since there is no fstab. See "Not
   verified" on swap support in COS.
3. **Prepare Docker.** Wait for the daemon, run `docker-credential-gcr configure-docker
   --registries=<region>-docker.pkg.dev` (preinstalled on COS "starting with milestone 60"; it
   authenticates pulls as the VM's service account through the metadata server). COS does not
   allow writing `/root`, so `HOME` and `DOCKER_CONFIG` point at `/var/lib/pulsebridge-edge`
   (COS docs: otherwise the helper "will try to write credentials to /root/.docker/config.json,
   which is not permitted"). Create the bridge network `pulsebridge-edge`.
4. **Start the MLLP adapter** (it needs neither the disk nor a secret, so the HL7v2 path comes up
   first and stays up if MariaDB has a problem), then **watch it for 15 seconds**: if it is not
   `running` or its restart count went up, log `ERROR: mllp-adapter is crash-looping or down`
   with its last 20 log lines (a wrong flag shows as a usage message, spike S4).
5. **Copy secrets** listed in `secret_files` from Secret Manager to mode-600 files in
   `/mnt/disks/edge/secrets` (mode 700), once. Retries for `boot_wait_minutes` (15) because on a
   first apply the secret version is added after the VM boots.
6. **Start MariaDB** and wait (up to 15 minutes) until its health check says `healthy`.
7. **Run hooks** (T23), in name order.

Each step runs in its own subshell with `errexit` on; a failure is logged as
`ERROR: step <name>: FAILED` and the script carries on with the steps that do not depend on it,
then exits non-zero listing the failed steps. A lock file stops two runs overlapping.

**Safe on every boot.** Each step checks first: a disk with a filesystem is never formatted, an
active swap file is left alone, an image already present is not pulled, a secret file that exists
is kept. Each container carries a label with a hash of its full `docker run` arguments: if the
container exists with the same hash it is left alone (or started if stopped); if the hash differs
(new digest, new flag) it is replaced. Data lives on bind mounts, so replacing a container keeps
it.

**Unattended restart after a reboot.** Both containers use `--restart unless-stopped`, so Docker
restarts them when its daemon starts, and the startup script runs again. MariaDB's data and
password are **bind mounts** from the data disk: after a reboot `/mnt/disks` is empty until the
script mounts the disk, and a bind mount with a missing source makes Docker refuse to start the
container rather than give MariaDB an empty directory on the boot disk. The script then starts it.
The VM itself restarts after a host failure (`automatic_restart`) and live-migrates during
maintenance.

**Waiting.** Image pulls, the secret and MariaDB's health each retry for `boot_wait_minutes`
(default 15): spike S2 measured about 15 minutes from apply to a usable OpenEMR on first boot,
most of it the 3.6 GB OpenEMR pull (T23) and OpenEMR's first-run setup.

### The MLLP adapter command

Exactly what spike S4 ran (output `mllp_adapter_command`):

```bash
docker run --detach --name mllp-adapter --label pulsebridge.config=<hash> \
  --restart unless-stopped --publish 2575:2575 --log-opt max-size=10m --log-opt max-file=3 \
  gcr.io/cloud-healthcare-containers/mllp-adapter@sha256:92eaa0fc5fcd0eb7ff62bd28c9140dcd7582bfec20790e5b203ca8dc75c4a90f \
  /usr/mllp_adapter/mllp_adapter \
  --port=2575 --receiver_ip=0.0.0.0 \
  --hl7_v2_project_id=<project> --hl7_v2_location_id=<location> \
  --hl7_v2_dataset_id=<dataset> --hl7_v2_store_id=<store> \
  --log_error_msg=true --export_stats=false
```

- The image has no entrypoint, so the binary path is the first word of the command.
- The four store flags are short names, taken unchanged from the healthcare module's
  `mllp_adapter_store_flags`. A resource path is rejected by validation.
- `--export_stats=false`: the default sends metrics to Cloud Monitoring, which the VM's account may
  not write.
- Content logging stays off: `--log_ack`, `--log_nacked_msg` and `--log_input_msg_in_base64` are
  not passed (they log message content, which is patient data). `--log_error_msg=true` logs only
  the store's error text for a NACK.
- No `--credentials`: the adapter gets tokens for the VM's account from the metadata server.
- Bridge networking with `--publish`, not host networking: COS has "a locked-down firewall" on the
  host, and Docker's published ports are forwarded by Docker's own rules.
- On a Healthcare API failure (for example a missing permission) the adapter logs `failed to handle
  message` and closes the connection without an ACK (S4); the cuff simulator shows "no
  acknowledgment".

The image has only the moving tag `latest`, so the digest is the pin. On 2026-11-15 `latest` still
resolved to S4's digest (checked with a registry `HEAD` request, no pull). To check for a newer
build: `crane digest gcr.io/cloud-healthcare-containers/mllp-adapter:latest`, then test it before
changing `mllp_adapter_image`. A tag is rejected by validation.

## Network and images

- **No external IP:** the network interface has no `access_config` block (that block is what gives
  an instance an external address). The VM is reachable only from inside the VPC, and the
  project-base firewall admits only the serverless subnet on 2575 and 443.
- The VM carries the project-base network tag, so the firewall rule applies whether it targets the
  tag or (with `edge_vm_service_account_email` set in project-base) the VM's service account.
- **Images without internet:** MariaDB (and OpenEMR, T23) come from the Docker Hub mirror, an
  Artifact Registry remote repository that fetches from Docker Hub on Google's side. The adapter
  comes from `gcr.io`, which is public.
- **Artifact Registry over Private Google Access, confirmed from documentation only.** Google's
  "Configure Private Google Access" page lists `pkg.dev or *.pkg.dev` and `gcr.io or *.gcr.io`
  among the domains reachable through Private Google Access. With the default domains no DNS
  change is needed; the VPC needs the default route to `default-internet-gateway` and egress
  allowed, both true in project-base (it keeps the default route and denies only ingress).
  **Assumption to verify at T25:** the first boot's log shows both pulls succeeding.
  **Fallback if not:** set `enable_cloud_nat = true` in project-base (about $4.70 a month plus
  $0.045 per GiB, billed hourly); nothing in this module changes, because the same image paths are
  then reached through NAT.

## Identity and scopes

- The VM runs as the identity module's `edge-vm` account; the Compute Engine default account is
  refused by validation (it has project-wide Editor in older projects).
- Scopes are `cloud-platform` only. Secret Manager's access-control page: "the underlying instance
  or node must have the cloud-platform OAuth scope", so no narrower set works, and Google's
  guidance is "set the full cloud-platform access scope on the instance, then control the service
  account's access using IAM roles".
- What the account may do (granted elsewhere unless noted): `roles/healthcare.hl7V2Ingest` on the
  HL7v2 store only (healthcare module), its only Healthcare permission; `secretAccessor` on the
  three database/OpenEMR password secrets and version-adding on the OpenEMR key secret (identity);
  `roles/logging.logWriter` (identity); and `roles/artifactregistry.reader` on the Docker Hub
  mirror repository (**this module**).
- The mirror grant is made here although the repository belongs to project-base, because pulling
  is this module's need and project-base grants nothing on it, so the grant still has exactly one
  owner. If the orchestrator prefers the identity README's rule "a binding lives in the module that
  owns the resource", move it to project-base (which would need the VM's email as an input).

## Secrets

No password or key is in Terraform, in the startup script, in metadata or in state:

- The only database password T21 needs, MariaDB's root password, lives in the identity module's
  `db-root-password` secret. The VM reads it at boot **with its own account**: an access token
  from the metadata server, sent to curl on standard input (not as an argument, so it does not
  show in the process list), then `secretmanager.googleapis.com/v1/<secret>/versions/latest:access`.
  The value goes into a mode-600 file and reaches MariaDB as `MARIADB_ROOT_PASSWORD_FILE`, so it
  is not in `docker inspect` either. It is never logged; the tests check this with sentinels.
- A secret file is fetched **once** and then kept: MariaDB takes its root password only when it
  initialises an empty data directory, so the file must keep matching the database. Rotating it
  means changing it inside MariaDB, then the file.
- If no version exists yet (first apply, before `scripts/secrets` from T27 has run), the script
  logs `ERROR: secret mariadb-root-password ... HTTP status 404` after 15 minutes, keeps the
  adapter running, and leaves MariaDB stopped. After adding the version: `sudo
  google_metadata_script_runner startup`, or reset the VM.

## The seam with T23 (OpenEMR)

T21 provides, and T23 builds on:

| Provided by T21 | Where |
| --- | --- |
| Docker (COS), credential helper for the mirror host | on every boot, before hooks run |
| MariaDB 11.8, low-memory settings, healthy before any hook runs | container `mariadb` on network `pulsebridge-edge` (outputs `mariadb_container`, `docker_network`) |
| Root password file | `/mnt/disks/edge/secrets/mariadb-root-password` (output `secrets_dir`) |
| More secret files | add entries to `secret_files` (for example `openemr-db-password`, `openemr-admin-password`) |
| A directory for OpenEMR's sites on the data disk | `/mnt/disks/edge/openemr` (output `openemr_data_dir`) |
| 2 GB swap | on every boot |
| A place for T23's containers | `hooks`: `{ name = bash script }`, stored as metadata `pulsebridge-hook-<name>`, run on every boot after MariaDB is healthy |
| A stable address for `site_addr_oath` | output `internal_ip` (from the reserved address, so no dependency cycle with the hook) |
| The HTTPS firewall rule | project-base (443 from the serverless subnet) |

T23 adds: the OpenEMR container (pinned by digest, pulled from `<mirror>/openemr/openemr@sha256:...`,
on network `pulsebridge-edge`, `--publish 443:443`, sites bind-mounted from `openemr_data_dir`,
Apache capped at four workers, `OPENEMR_SETTING_*` variables), waiting for `/meta/health/readyz`
(about 15 minutes on first boot), and the OAuth2 client registration. Hook rules:

- A hook runs as root on **every boot**, so it must be idempotent (use the same label-hash idea, or
  check before acting).
- A hook must contain **no secret**: metadata is readable by anyone who can view the instance. It
  reads secrets from `$EDGE_SECRETS_DIR/<name>`.
- Environment given to a hook: `EDGE_DATA_DIR`, `EDGE_SECRETS_DIR`, `EDGE_MARIADB_DIR`,
  `EDGE_OPENEMR_DIR`, `EDGE_STATE_DIR`, `EDGE_DOCKER_NETWORK`, `EDGE_MARIADB_CONTAINER`,
  `EDGE_MIRROR_URL`, `EDGE_BOOT_WAIT_SECONDS`, plus `HOME` and `DOCKER_CONFIG` (credential helper
  already configured).
- A hook's non-zero exit is logged as `ERROR: hook <name>: FAILED` and makes the script exit
  non-zero; other hooks still run.
- A hook that passes a password to OpenEMR as an environment value puts it in the container's
  configuration on the boot disk (`docker inspect`); prefer files where the image supports them.
- Instance metadata holds 256 KB per value and 512 KB in total; a precondition checks this.

## Looking at the VM without public access

There is no external IP and, by default, no SSH rule. The usual way in is **SSH through
Identity-Aware Proxy (IAP) TCP forwarding**: `gcloud compute ssh <instance> --zone <zone>
--tunnel-through-iap`. It needs, and **this module creates none of it**:

- a firewall rule allowing TCP 22 from `35.235.240.0/20` (IAP's forwarding range) to the VM:
  `enable_iap_ssh = true` in project-base, which also enables the IAP API;
- for the person: `roles/iap.tunnelResourceAccessor` (on the project or the VM), and because OS
  Login is on, `roles/compute.osAdminLogin` (or `roles/compute.osLogin`), plus
  `roles/iam.serviceAccountUser` on the VM's service account.

Without SSH, the startup script's output is on serial port 1 (`gcloud compute instances
get-serial-port-output <instance> --zone <zone>`) and, with the logging agent on, in Cloud
Logging. On the VM: `sudo journalctl -u google-startup-scripts.service`, `docker ps`, `docker logs
mllp-adapter`. With OS Login, a user needs `sudo` for `docker` (COS adds only metadata-key users
to the `docker` group).

## Cost

The VM is PulseBridge's only always-on resource.

| Item | Monthly cost |
| --- | --- |
| e2-micro VM | **$0 inside the free tier**: one non-preemptible e2-micro per billing account, in `us-west1`, `us-central1` or `us-east1`, counted in hours across all e2-micro VMs of the account. A second e2-micro (another deployment, or the old instance the brief keeps) or another region is billed: list price about $6 a month in us-central1, **not confirmed** (Google's price table did not render here) |
| Boot disk 20 GB + data disk 10 GB, `pd-standard` | **$0**: the free tier covers 30 GB-months of standard persistent disk. Beyond that $0.04 per GB-month (Google's example: 200 GB standard for $8.00 a month) |
| Reserved internal IP | $0 ("There is no charge for static or ephemeral internal IP addresses") |
| Shielded VM, COS image, OS Login | $0 |
| Artifact Registry storage for cached images | project-base's cost: $0.10 per GB-month beyond 0.5 GB free. The cached MariaDB image is a fraction of a GB (not measured); the adapter comes from gcr.io and is not cached; OpenEMR (T23, 3.6 GB unpacked) will likely take the total past the free 0.5 GB, so cents a month |
| Network | $0 inside the region (VM to Google APIs in the same region, Cloud Run to the VM) |
| Cloud Logging | free up to 50 GiB a month per project; this VM logs little |
| Cloud NAT | **not used**. Only if Private Google Access fails for Artifact Registry: about $4.70 a month plus $0.045 per GiB, **billed hourly** (project-base `enable_cloud_nat`) |

Nothing in this module is billed by the hour except the VM itself if it leaves the free tier
(machine type other than e2-micro, a second e2-micro, another region, or `disk_type` other than
`pd-standard`, which is billed per GB-month). `terraform destroy` removes the VM, both disks and
the address: nothing billable is left. There are no snapshots of the data disk: a destroyed disk
is gone (acceptable for synthetic dev data; add a snapshot schedule for prod if wanted, at
snapshot storage prices).

## Tests

```bash
make tf-test    # runs everything below with Google credentials removed from the environment
```

- `tests/edge_vm.tftest.hcl` (`terraform test`, `mock_provider`, `command = plan`): no
  `access_config`; machine type default and variable; dedicated service account and the
  `cloud-platform` scope only; the mirror reader grant on that one repository; Shielded VM; the
  separate disk attached read-write as `edge-data` and mounted by that name; disk sizes inside the
  free tier; metadata keys (no SSH keys, nothing secret-looking, OS Login on); MariaDB's password
  passed as a file; the adapter command exactly as S4 (also in the rendered script); digests;
  hooks and extra secrets reaching the script.
- `tests/validation.tftest.hcl`: a tag instead of a digest (adapter and MariaDB), the default
  Compute account, a store path instead of a short name, a missing MariaDB secret, a secret with a
  version, a non-COS image, a zone outside the subnet's region, and more are each rejected.
- `tests/static_checks.sh`: fails on any `access_config`, any firewall resource, any use of the
  default Compute account, any service account key or `--credentials`, any secret-looking metadata
  key or secret-valued variable, any hardcoded project, `set -x` in the script, or a child module.
- `tests/startup_script.sh`: shellcheck and `bash -n`; a check that the template assigns every
  setting the script reads; and behaviour tests that run the real script in a sandbox whose PATH
  holds only fakes (`tests/fakes/fake-command` as `docker`, `curl`, `mount`, `blkid`, `mkfs.ext4`,
  ...). Cases: first boot on an empty disk, a second run (formats nothing, starts nothing), a
  reboot (mounts and starts, creates nothing), a changed adapter flag (replaced), a disk with ext4
  (not formatted), with another filesystem or a read error (not formatted, not mounted), a missing
  disk (fails loudly, adapter still up), a crash-looping adapter (logged with its usage output), a
  secret with no version, failing pulls, an unhealthy MariaDB (no hooks), hooks with their
  environment and a failing hook, and a missing command. Sentinel values check that neither the
  secret, its base64 form nor the access token appears in the log or in any command line.
  shellcheck is taken from PATH or `$SHELLCHECK`; without it the lint is skipped with a warning,
  except when `CI=true`.

## Not verified

Nothing in this module has been applied: no VM, disk or grant was created, and the script has
run only against fakes. Verify at T25, in this order (each step depends on the ones before):

1. **The VM boots** COS with Shielded VM on an e2-micro, and the startup script runs: serial port
   shows `pulsebridge-edge: edge VM startup: begin`.
2. **Every command the script needs exists on COS**: the preflight step logs any missing one
   (`blkid`, `mkfs.ext4`, `fsck.ext4`, `mkswap`, `swapon`, `curl`, `sha256sum`, `stat`; `flock`,
   `fallocate` and `resize2fs` are optional). The COS pages show `curl`, `fsck.ext4` and `mount`
   in use on COS; the others are assumed.
3. **The data disk** appears as `/dev/disk/by-id/google-edge-data`, is formatted once, mounted, and
   **after a reboot** is mounted again without formatting (check `dmesg` and the log line "has an
   ext4 filesystem; not formatting").
4. **Swap**: `swapon --show` lists the file. Swap on COS is not documented for Compute Engine VMs:
   the evidence is indirect (GKE offers node swap on COS nodes; COS release notes mention zswap).
   If `swapon` fails, the step logs an error and everything else still runs; S2 found the tuned
   stack survived without swap.
5. **Artifact Registry and gcr.io over Private Google Access**: both pulls succeed with no NAT.
   If not, turn on Cloud NAT in project-base (fallback above).
6. **The mirror grant**: `roles/artifactregistry.reader` on the repository is enough for a pull
   through a remote repository (Docker Hub fetched on Google's side).
7. **docker-credential-gcr** writes to `$HOME/.docker` under `/var/lib/pulsebridge-edge` and
   `docker pull` uses it (the docker CLI reads `DOCKER_CONFIG`).
8. **The adapter** stays up (`mllp-adapter is up and stayed up`), the published port is reachable
   from the cuff simulator through the project-base firewall and COS's host firewall, and a test
   message gets `AA` with **only** `roles/healthcare.hl7V2Ingest` on the store (S4 proved ingest
   only with project Editor).
9. **The secret**: with a version added (T27), the file appears with mode 600; the Secret Manager
   response parsing (a `sed` over the JSON, as COS has no `jq`) works on the real response.
10. **MariaDB** initialises on the bind-mounted directory, becomes `healthy`, and `--health-start-interval`
    is accepted (it needs Docker 25 or newer; COS milestone 125 ships Docker 27.5).
11. **After a reboot** both containers come back without help and the script reports all steps ok.
12. **Cloud Logging**: the logging agent ships the startup script's journal lines (the COS page says
    it exports "some system and container logs").
13. **The crash-loop check** on a real crash: Docker's restart back-off may space restarts more
    than 15 seconds apart after a long loop; the check also catches the `restarting` status.
14. **The e2-micro free tier** applies to this VM (billing report after the first month), and the
    e2-micro list price outside it.
15. **Docker log options** (`--log-opt max-size`) are compatible with the COS logging agent.

Other assumptions about other modules (exact output names): `project-base.edge_subnet_id`,
`edge_vm_network_tag`, `mllp_port`, `dockerhub_mirror_url` (non-null: `create_dockerhub_mirror`
must stay true); `identity.service_account_emails["edge-vm"]`,
`secret_resource_ids["db-root-password"]`; `healthcare.mllp_adapter_store_flags`. No change to
those modules is needed. The identity README's role table does not yet list the mirror reader
grant made here.

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.11 |
| <a name="requirement_google"></a> [google](#requirement\_google) | ~> 8.11 |

## Providers

| Name | Version |
|------|---------|
| <a name="provider_google"></a> [google](#provider\_google) | 8.11.0 |

## Modules

No modules.

## Resources

| Name | Type |
|------|------|
| [google_artifact_registry_repository_iam_member.mirror_reader](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/artifact_registry_repository_iam_member) | resource |
| [google_compute_address.internal](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/compute_address) | resource |
| [google_compute_disk.data](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/compute_disk) | resource |
| [google_compute_instance.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/compute_instance) | resource |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_boot_disk_size_gb"></a> [boot\_disk\_size\_gb](#input\_boot\_disk\_size\_gb) | Boot disk size in GB. It holds the COS system partitions, Docker's image store (OpenEMR is about 3.6 GB unpacked, spike S2) and the swap file. 20 GB boot plus 10 GB data stays inside the free tier's 30 GB-months of standard persistent disk. | `number` | `20` | no |
| <a name="input_boot_image"></a> [boot\_image](#input\_boot\_image) | Boot image: a Container-Optimized OS image or image family from the cos-cloud project. The startup script relies on COS (Docker and docker-credential-gcr preinstalled, read-only root, stateless /etc), so other images are refused. | `string` | `"projects/cos-cloud/global/images/family/cos-stable"` | no |
| <a name="input_boot_wait_minutes"></a> [boot\_wait\_minutes](#input\_boot\_wait\_minutes) | How long the startup script keeps retrying things that are slow on first boot (image pulls, a secret version not yet added, MariaDB becoming healthy) before it logs an error and gives up for this boot. Spike S2: budget about 15 minutes from apply to a usable stack. | `number` | `15` | no |
| <a name="input_data_disk_size_gb"></a> [data\_disk\_size\_gb](#input\_data\_disk\_size\_gb) | Size in GB of the separate persistent disk for MariaDB and OpenEMR site data. A fresh OpenEMR database is a few hundred MB. Growing it later is done in place (the startup script grows the filesystem on the next boot); shrinking is not possible. | `number` | `10` | no |
| <a name="input_disk_type"></a> [disk\_type](#input\_disk\_type) | Persistent disk type for both disks. pd-standard is the only type the free tier covers; pd-balanced is faster (spike S2 suspected the slow disk for the 5-minute OpenEMR first boot) but billed. | `string` | `"pd-standard"` | no |
| <a name="input_dockerhub_mirror_url"></a> [dockerhub\_mirror\_url](#input\_dockerhub\_mirror\_url) | Registry path of the Docker Hub pull-through cache from project-base (output `dockerhub_mirror_url`, <location>-docker.pkg.dev/<project>/<repository>). MariaDB (and OpenEMR, T23) are pulled from here; this module grants the VM's account read access on that one repository. | `string` | n/a | yes |
| <a name="input_edge_subnet_id"></a> [edge\_subnet\_id](#input\_edge\_subnet\_id) | ID of the edge subnet from project-base (output `edge_subnet_id`, projects/<project>/regions/<region>/subnetworks/<name>). The subnet must have Private Google Access on: the VM has no external IP and pulls its images from Artifact Registry and gcr.io through it. | `string` | n/a | yes |
| <a name="input_enable_cloud_logging"></a> [enable\_cloud\_logging](#input\_enable\_cloud\_logging) | Turn on Container-Optimized OS's logging agent (metadata google-logging-enabled), so startup-script and container logs reach Cloud Logging. The VM's account has roles/logging.logWriter from the identity module. The agent uses some of the 1 GB of RAM; off still leaves the serial console. | `bool` | `true` | no |
| <a name="input_hl7v2_store_flags"></a> [hl7v2\_store\_flags](#input\_hl7v2\_store\_flags) | The four store coordinates the MLLP adapter takes as flags (healthcare output `mllp_adapter_store_flags`). Each is a short name, not a resource path: --hl7\_v2\_store\_id=<store name>, as spike S4 recorded. | <pre>object({<br/>    hl7_v2_project_id  = string<br/>    hl7_v2_location_id = string<br/>    hl7_v2_dataset_id  = string<br/>    hl7_v2_store_id    = string<br/>  })</pre> | n/a | yes |
| <a name="input_hooks"></a> [hooks](#input\_hooks) | Extra boot steps, as { <name> = <bash script> }. Each is stored in instance metadata as<br/>`pulsebridge-hook-<name>` and run by the startup script on every boot, in name order, after<br/>MariaDB is healthy. This is the seam for T23 (OpenEMR container, client registration). A hook<br/>must be idempotent and must contain no secret (metadata is readable by anyone who can view the<br/>instance); it reads secrets from the files listed in `secret_files`. | `map(string)` | `{}` | no |
| <a name="input_internal_ip"></a> [internal\_ip](#input\_internal\_ip) | Optional fixed internal IPv4 address for the VM inside the edge subnet. Null lets Google pick a free one, which is then reserved so it survives VM replacement (the cuff simulator's MLLP\_HOST and OpenEMR's site address stay valid). | `string` | `null` | no |
| <a name="input_labels"></a> [labels](#input\_labels) | Labels added to the VM, its disks and its address. | `map(string)` | `{}` | no |
| <a name="input_machine_type"></a> [machine\_type](#input\_machine\_type) | Machine type of the VM. e2-micro (2 shared vCPUs, 1 GB) is the free-tier size that spike S2 measured as enough for one reviewer at a time; e2-small eases the CPU-burst limit but is outside the free tier and billed by the hour. | `string` | `"e2-micro"` | no |
| <a name="input_mariadb_image"></a> [mariadb\_image](#input\_mariadb\_image) | MariaDB image path inside the Docker Hub mirror, pinned by digest (an optional tag before the digest is kept for readability only). Default: mariadb 11.8 as tested in spikes S1 and S2; the digest was confirmed current on Docker Hub on 2026-11-15. | `string` | `"library/mariadb:11.8@sha256:d36f18d65a0459510b8b0f643b226a6d299e3206486e2b64ff20754a6d291707"` | no |
| <a name="input_mllp_adapter_image"></a> [mllp\_adapter\_image](#input\_mllp\_adapter\_image) | Google's MLLP adapter image, pinned by digest. The image has no version tags, only `latest` (spike S4), so a digest is the only pin. To check for a newer build without pulling: `crane digest gcr.io/cloud-healthcare-containers/mllp-adapter:latest`, or a HEAD request for /v2/cloud-healthcare-containers/mllp-adapter/manifests/latest on gcr.io and read Docker-Content-Digest. Default: the digest spike S4 tested, still `latest` on 2026-11-15. | `string` | `"gcr.io/cloud-healthcare-containers/mllp-adapter@sha256:92eaa0fc5fcd0eb7ff62bd28c9140dcd7582bfec20790e5b203ca8dc75c4a90f"` | no |
| <a name="input_mllp_port"></a> [mllp\_port](#input\_mllp\_port) | Port the MLLP adapter is published on at the VM's address. Must equal project-base's `mllp_port`, which is the port its firewall rule opens from the serverless subnet. | `number` | `2575` | no |
| <a name="input_name_prefix"></a> [name\_prefix](#input\_name\_prefix) | Prefix for the names of every resource this module creates (<prefix>-edge-vm, <prefix>-edge-data, <prefix>-edge-internal). Use the same prefix as project-base. | `string` | `"pulsebridge"` | no |
| <a name="input_network_tag"></a> [network\_tag](#input\_network\_tag) | Network tag the project-base firewall rules target (project-base output `edge_vm_network_tag`). Always attached; harmless when project-base targets the service account instead. | `string` | n/a | yes |
| <a name="input_project_id"></a> [project\_id](#input\_project\_id) | ID of the GCP project the VM, its disk and its internal address are created in. | `string` | n/a | yes |
| <a name="input_secret_files"></a> [secret\_files](#input\_secret\_files) | Secret Manager secrets the VM copies to files on its data disk at boot, as<br/>{ <file name> = "projects/<project>/secrets/<secret id>" } (identity output `secret_resource_ids`).<br/>The VM reads them with its own account; the values never pass through Terraform, metadata or<br/>logs. `mariadb-root-password` is required (identity secret `db-root-password`). T23 adds the<br/>OpenEMR passwords here. A file is fetched only when it does not exist yet, so it stays equal to<br/>what the database was initialised with. | `map(string)` | n/a | yes |
| <a name="input_service_account_email"></a> [service\_account\_email](#input\_service\_account\_email) | Email of the VM's dedicated service account (identity output `service_account_emails["edge-vm"]`). Never the Compute Engine default account: in older projects that account holds project-wide Editor. | `string` | n/a | yes |
| <a name="input_swap_size_mb"></a> [swap\_size\_mb](#input\_swap\_size\_mb) | Size of the swap file in MiB (spike S2: 2 GB, cheap insurance against a memory spike; the tuned stack also survived without it). | `number` | `2048` | no |
| <a name="input_zone"></a> [zone](#input\_zone) | Zone for the VM and its data disk, inside the region of the edge subnet. The e2-micro free tier applies only in us-west1, us-central1 and us-east1 (one VM per billing account). | `string` | n/a | yes |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_data_disk_device"></a> [data\_disk\_device](#output\_data\_disk\_device) | Stable device path of the data disk on the VM. |
| <a name="output_data_disk_name"></a> [data\_disk\_name](#output\_data\_disk\_name) | Name of the persistent data disk (MariaDB data, OpenEMR site files, copied secrets). |
| <a name="output_data_mount_dir"></a> [data\_mount\_dir](#output\_data\_mount\_dir) | Where the data disk is mounted on the VM. |
| <a name="output_docker_network"></a> [docker\_network](#output\_docker\_network) | User-defined Docker bridge network MariaDB is on. T23's OpenEMR container joins it and reaches the database as host `mariadb`. |
| <a name="output_hook_metadata_keys"></a> [hook\_metadata\_keys](#output\_hook\_metadata\_keys) | Instance metadata keys holding the hook scripts, in the order they run. |
| <a name="output_instance_id"></a> [instance\_id](#output\_instance\_id) | ID of the edge VM (projects/<project>/zones/<zone>/instances/<name>). |
| <a name="output_instance_name"></a> [instance\_name](#output\_instance\_name) | Name of the edge VM. |
| <a name="output_internal_ip"></a> [internal\_ip](#output\_internal\_ip) | Reserved internal IPv4 address of the edge VM. It survives VM replacement. This is the cuff simulator's MLLP\_HOST (T22) and the host part of OpenEMR's site address (T23). |
| <a name="output_mariadb_container"></a> [mariadb\_container](#output\_mariadb\_container) | Name (and DNS name on docker\_network) of the MariaDB container. |
| <a name="output_mariadb_data_dir"></a> [mariadb\_data\_dir](#output\_mariadb\_data\_dir) | MariaDB's data directory on the data disk (bind-mounted at /var/lib/mysql). |
| <a name="output_mariadb_image"></a> [mariadb\_image](#output\_mariadb\_image) | Full MariaDB image reference the VM pulls (through the Docker Hub mirror, by digest). |
| <a name="output_mllp_adapter_command"></a> [mllp\_adapter\_command](#output\_mllp\_adapter\_command) | The exact command the MLLP adapter container runs (binary path plus the flags recorded in spike S4). |
| <a name="output_mllp_adapter_container"></a> [mllp\_adapter\_container](#output\_mllp\_adapter\_container) | Name of the MLLP adapter container (for `docker logs mllp-adapter` on the VM). |
| <a name="output_mllp_host"></a> [mllp\_host](#output\_mllp\_host) | Host the cuff simulator sends MLLP to (same as internal\_ip; named for T22's MLLP\_HOST). |
| <a name="output_mllp_port"></a> [mllp\_port](#output\_mllp\_port) | Port the MLLP adapter is published on at mllp\_host. |
| <a name="output_openemr_data_dir"></a> [openemr\_data\_dir](#output\_openemr\_data\_dir) | Directory on the data disk reserved for T23: OpenEMR's sites directory (/var/www/localhost/htdocs/openemr/sites in the container). |
| <a name="output_secrets_dir"></a> [secrets\_dir](#output\_secrets\_dir) | Mode-700 directory on the data disk with one mode-600 file per entry of secret\_files (for example mariadb-root-password). |
| <a name="output_service_account_email"></a> [service\_account\_email](#output\_service\_account\_email) | Service account the VM runs as (passed in from the identity module). |
| <a name="output_state_dir"></a> [state\_dir](#output\_state\_dir) | Boot-disk directory for the startup script's state: docker client configuration (.docker), the run lock and the fetched hook scripts (hooks/). This is the units directory: there is no compose file on COS. |
| <a name="output_zone"></a> [zone](#output\_zone) | Zone of the edge VM and its data disk. |
<!-- END_TF_DOCS -->
