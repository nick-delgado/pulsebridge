# Module `edge-vm`

The one always-on machine in PulseBridge (task T21). It creates:

- an **e2-micro VM** running Container-Optimized OS (COS) on the private `edge` subnet, with **no
  external IP and no public port**, Shielded VM on, OS Login on, and a dedicated service account;
- a **separate persistent disk** for the MariaDB database and OpenEMR's site files, attached by a
  stable device name, which survives VM replacement and is removed by `terraform destroy`;
- a **reserved internal IP address**, so the address the cuff simulator sends MLLP to does not
  change when the VM is replaced;
- a **startup script**, run by COS on every boot, that mounts the disk, turns on a 2 GB swap file,
  and starts Google's **MLLP adapter** and **MariaDB** as containers, then runs hook scripts
  (the seam for T23, OpenEMR).

It creates **no firewall rule** (decision D14: the rule that lets the serverless subnet reach
MLLP 2575 and HTTPS 443 lives in `project-base`), **no IAM binding** (each grant the VM's account
has is made by the module that owns the resource, see "Identity and scopes"), no service account
(module `identity`) and no provider or backend configuration (the env root configures those).
Nothing in it carries a password or key; see "Secrets".

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
module "project_base" {
  # ...
  # Required for the edge VM: the firewall rules target this account, and it gets read access to
  # the Docker Hub mirror (granted in project-base, which owns the repository).
  edge_vm_service_account_email = module.identity.service_account_emails["edge-vm"]
}

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
with at least `mariadb-root-password`, project-base's `edge_vm_service_account_email` (without it
the VM cannot pull MariaDB), and later T23's `hooks` and extra `secret_files`. Every other input
has a default. Keep `name_prefix` equal to `project-base`'s. `mllp_port` must equal
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
| `restart: always` | `--restart unless-stopped` (as S4 used; a container stopped by hand stays stopped), plus `--stop-timeout 120` |
| `MARIADB_ROOT_PASSWORD: ${DB_ROOT_PASS}` from `.env` | `MARIADB_ROOT_PASSWORD_FILE` pointing at a mode-600 file from Secret Manager (no `.env`) |
| `dbdata` volume | bind mount of `/mnt/disks/edge/mariadb` on the separate data disk |
| health check `healthcheck.sh --su-mysql --connect --innodb_initialized`, start 2m / interval 1m / timeout 10s / retries 3 | the same, as `--health-*` options |
| `mllp` service with `network_mode: host` and `--logtostderr` | S4's `docker run` instead: bridge network with `--publish 2575:2575` and no `--logtostderr` (see "The MLLP adapter command") |
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

1. **Mount the data disk.** It waits up to 30 s for `/dev/disk/by-id/google-edge-data` (Compute
   Engine creates that link from the attachment's `device_name`), and **fails loudly** if it does
   not appear. Then it decides whether the disk may be formatted; see "The format decision" below,
   because this is where a mistake would wipe the clinic database. COS keeps `/etc` in memory, so
   there is no `/etc/fstab` across boots ("you cannot use /etc/fstab to automatically fsck and
   mount the disks on boot"); the script runs `fsck.ext4 -p` (stopping on any exit above 1) and
   mounts at `/mnt/disks/edge` itself on every boot. It also grows the filesystem if the disk was
   enlarged.
2. **Turn on swap.** A 2 GB file at `/mnt/stateful_partition/pulsebridge-swapfile` on the boot
   disk's stateful partition (writable and kept across reboots). The file is created once
   (`fallocate`, `mkswap`) and `swapon` is run on every boot, since there is no fstab. See "Not
   verified" on swap support in COS.
3. **Prepare Docker.** Wait up to 60 s for the daemon, run `docker-credential-gcr configure-docker
   --registries=<region>-docker.pkg.dev` (preinstalled on COS "starting with milestone 60"; it
   authenticates pulls as the VM's service account through the metadata server). COS does not
   allow writing `/root`, so `HOME` and `DOCKER_CONFIG` point at `/var/lib/pulsebridge-edge`
   (COS docs: otherwise the helper "will try to write credentials to /root/.docker/config.json,
   which is not permitted"). Create the bridge network `pulsebridge-edge`.
4. **Start the MLLP adapter** (it needs neither the disk nor a secret, so the HL7v2 path comes up
   first and stays up if MariaDB has a problem), then **watch it for 15 seconds**: if it is not
   `running` or its restart count went up, log `ERROR: mllp-adapter is crash-looping or down`
   with its last 20 log lines (a wrong flag shows as a usage message, spike S4).
5. **Copy MariaDB's secret** (`mariadb-root-password`) from Secret Manager to a mode-600 file in
   `/mnt/disks/edge/secrets` (mode 700), once.
6. **Start MariaDB** and wait until its health check says `healthy`. MariaDB depends only on its
   own secret: a missing T23 secret never keeps it down. Before it starts, the script looks at the
   data directory and moves aside one left half-initialised by a failed first boot; the container
   runs a small wrapper in front of the image's entrypoint that gives a first initialisation more
   time. See "First initialisation of MariaDB" below.
7. **Copy the other secrets** in `secret_files` (T23's), all retried together.
8. **Run hooks** (T23), in name order, each under a time limit.

Each step runs in its own subshell with `errexit` on; a failure is logged as
`ERROR: step <name>: FAILED` and the script carries on with the steps that do not depend on it,
then exits non-zero listing the failed steps. A lock file (`flock` on
`/var/lib/pulsebridge-edge/startup.lock`) stops two runs overlapping: a second run started while
the first holds it exits at once with an error.

### The format decision

`blkid -p` (low-level probe of the device itself) runs first:

- exit 0 with `ext4`: mount it, never format;
- exit 0 with anything else: stop, do not touch it;
- exit 2 ("nothing found"): **not proof of a blank disk.** util-linux returns 2 when it cannot
  open the device, on a read error, and for contents it does not recognise, such as an ext4
  filesystem whose primary superblock is damaged but which is still recoverable from a backup
  superblock. So the script formats only if a second, positive check also passes: the device
  reads, its first, middle and last 4 MiB are all zero bytes (a new persistent disk reads as
  zeros), and `wipefs --no-act` (if present) finds no signature. Otherwise it stops **without
  formatting** and logs how to investigate (`blkid -p`, `dumpe2fs -h`, and the read-only
  `e2fsck -n -b 32768 <device>` against a backup superblock);
- any other exit: stop.

There is deliberately no variable that forces a format. A disk that must be reformatted is wiped
by a person, on purpose (`wipefs -a` or `blkdiscard`), after which the next boot sees a blank disk.

### First initialisation of MariaDB

**What happened (T42).** Prod's first boot failed twice on 18 November. On a new, empty data
directory the official MariaDB image's entrypoint (`/usr/local/bin/docker-entrypoint.sh`) first
creates the system tables (`mariadb-install-db`), then starts a **temporary server**, a MariaDB
running without networking, to load the time zones, set the root password and create the
`healthcheck` users, then stops it and starts the real server. It waits for that temporary server
only 30 seconds (`for i in {30..0}` in `docker_temp_server_start`), and nothing configures that.
On prod's e2-micro the server took over 33 s at boot, so the entrypoint printed "Unable to start
server." and exited. Docker restarted the container (`--restart unless-stopped`) on the
half-made directory; the entrypoint now saw a database (it tests for the `mysql` schema
directory), skipped initialisation, and started a server without the health-check users, so every
health check was refused ("Access denied for user 'mysql'@'localhost'") until the deadline. In
throwaway containers on the same VM the same step took 1 to 16 s; on dev it took 3 s. Load and
CPU contention on a shared-core VM at boot are the likely cause, not proven.

**Fix 1: a longer wait, only on the first initialisation.** `scripts/mariadb-entrypoint-wrapper.sh`
runs inside the container in front of the official entrypoint: the container is started with
`--entrypoint bash` and the command `-c <wrapper> pulsebridge-mariadb-entrypoint mariadbd <S2's
flags>`. The wrapper:

- on an **initialised** data directory (it has the `mysql` directory, the entrypoint's own test)
  runs the official entrypoint **unchanged**: restarts and reboots behave exactly as before;
- on an **empty** one writes a copy of the entrypoint to `/tmp` in the container with the one
  line `for i in {30..0}; do` changed to `for i in {300..0}; do` (`mariadb_init_wait_seconds`,
  default 300), and runs the copy. The rest of the entrypoint is byte for byte the image's
  (checked against `11.8/docker-entrypoint.sh` of MariaDB/mariadb-docker at commit `bdfe641`,
  the commit Docker's official-images library lists for 11.8.9: the copy differs in that line
  only);
- **refuses** (exits non-zero with `[ERROR] [PulseBridge]: expected 'for i in {30..0}; do'
  exactly once ...`) when that line is not in the entrypoint exactly once. A new image whose
  entrypoint changed is then noticed at its first initialisation instead of quietly getting
  the 30-second wait back. An already-initialised database is not affected by this check.

The image on disk is not modified; the copy lives in the container's writable layer and is
written again on every container start. The health check, restart policy, password file and
everything else in `docker run` are as before. The wait is bounded twice: the loop gives up after
`mariadb_init_wait_seconds`, and the variable may be at most half of `boot_wait_minutes`, so the
startup script is still waiting when the server answers (the other half covers the image pull,
`mariadb-install-db` and the health check's 1-minute interval).

Alternatives rejected: copying the entrypoint out of the image on the host and bind-mounting a
patched file (the entrypoint re-executes itself as the `mysql` user with `exec gosu mysql
"${BASH_SOURCE[0]}"`, so the copy must be executable, and `/var` on COS is mounted `noexec`);
running the first initialisation ourselves (re-implementing the entrypoint's root password,
user and time-zone steps is far more code to get wrong); a bigger machine (billed, and the cause
is not proven).

**Fix 2: recover from a half-initialised directory.** Before MariaDB starts, the script sorts the
data directory (`/mnt/disks/edge/mariadb`) into one of four cases and logs which:

| Data directory | Log line | Action |
| --- | --- | --- |
| empty | `... is empty: MariaDB will initialise a new database (first boot)` | start; the wrapper applies the longer wait |
| has `.my-healthcheck.cnf` | `... holds an initialised database (.my-healthcheck.cnf present); using it as it is` | start, never touched |
| no marker, but a database directory other than `mysql`, `sys`, `performance_schema`, `test` | `WARNING: ... has no .my-healthcheck.cnf ... holds the database(s) openemr: ... NOT moving it` | start, never touched; a person looks |
| no marker, system schemas and files only | `... is HALF-INITIALISED ...: moving it aside to /mnt/disks/edge/mariadb.incomplete-<UTC time> ...` | stop and remove the leftover container, rename the directory, make an empty one, remove older `mariadb.incomplete-*` copies, start: a fresh first initialisation |

The marker, `.my-healthcheck.cnf`, is the file the entrypoint writes into the data directory while
it creates the health-check users (`create_healthcheck_users`); `healthcheck.sh` logs in with it.
Without it the health check cannot pass, so a container on such a directory was never serving and
stopping it loses nothing. It is a **one-way** test, deliberately erring towards never moving a
real database:

- A directory that has the marker is never touched, even though the marker is written just
  **before** the SQL that sets the root password runs: a first initialisation stopped in that
  instant would keep the marker and stay unhealthy. Prod's failure was earlier (no marker).
  Recovery by hand, if it ever happens: `sudo docker stop mariadb && sudo docker rm mariadb`, `sudo
  mv /mnt/disks/edge/mariadb /mnt/disks/edge/mariadb.broken-<date>`, `sudo
  google_metadata_script_runner startup`.
- A complete database could lack the marker if it was created by an image older than the
  health-check users (added to the official images around 2023) or the file was deleted. The
  second, positive signal covers the case that matters: any database directory besides MariaDB's own system schemas (OpenEMR's
  `openemr`, for example) means real data, and the directory is not moved whatever the marker
  says. A marker-less directory with system schemas only would be moved, which loses no
  application data (it is renamed, not deleted, until the next half-initialised directory
  replaces it).
- The decision is made once per run, before the container is (re)created, so a run never loops on
  re-initialising. It cannot tell a failed first initialisation from one still in progress: a
  manual re-run started while a first initialisation is still working (only possible after the
  previous run reached its deadline, which is at least twice the temporary server's wait) would
  stop it and start again. If the new first initialisation fails too, the run ends at the deadline with
  `ERROR: MariaDB is not healthy by the boot deadline (...): its FIRST INITIALISATION did not
  complete` and MariaDB's last 20 log lines (prefixed `mariadb|`); the next run (a reboot, or
  `sudo google_metadata_script_runner startup`) tries once more.

On success the startup log says `MariaDB is healthy: first initialisation of a new database
completed (<n>s into this run)`, or `MariaDB is healthy (existing database)` on later boots. In
`docker logs mariadb` the wrapper's lines carry `[PulseBridge]`.

OpenEMR's hooks (`modules/openemr/scripts`) need only the container name `mariadb`, its health
status and `docker exec ... mariadb`; none of that changes. The first boot after this change
replaces the MariaDB container once (its `docker run` arguments changed, so the configuration
hash did), with the graceful `docker stop --time 120`; the data directory is kept.

### Safe on every boot, and graceful replacement

Each step checks first: a disk with a filesystem is never formatted, an active swap file is left
alone, an image already present is not pulled, a secret file that exists is kept. Each container
carries a label with a hash of its full `docker run` arguments: if the container exists with the
same hash it is left alone (or started if stopped); if the hash differs (new digest, new flag) it
is **stopped gracefully** (`docker stop --time 120`: SIGTERM, then up to 120 s before SIGKILL, so
MariaDB can flush and shut down cleanly), removed without `--force`, and created again. Data lives
on bind mounts, so replacing a container keeps it.

At VM shutdown, MariaDB's `--stop-timeout 120` also applies: the Docker daemon stops each
container with that container's stop timeout, and waits overall for the largest stop timeout plus
5 seconds (moby `daemon.shutdownTimeout`:
https://github.com/moby/moby/blob/master/daemon/daemon.go). Whether the VM waits that long is
outside Docker's control: Compute Engine says the time a stopping instance takes to reach
TERMINATED "varies depending on the instance type", and stops it forcefully after that
(https://docs.cloud.google.com/compute/docs/shutdownscript, "Shutdown script running time"); COS's
systemd stop timeout for `docker.service` was not checked. So a graceful MariaDB shutdown is
guaranteed on replacement, best effort on VM stop; InnoDB recovers from an unclean stop on the
next start.

### Unattended restart after a reboot

Both containers use `--restart unless-stopped`, so Docker restarts them when its daemon starts,
and the startup script runs again. MariaDB's data and password are **bind mounts** from the data
disk: after a reboot `/mnt/disks` is empty until the script mounts the disk, and a bind mount with
a missing source makes Docker refuse to start the container rather than give MariaDB an empty
directory on the boot disk. The script then starts it. The VM itself restarts after a host
failure (`automatic_restart`) and live-migrates during maintenance.

### How long a boot can take

Every wait that can be slow on first boot (image pulls, secret versions not yet added, MariaDB
becoming healthy) shares **one deadline**, `boot_wait_minutes` (default 15) from the start of the
run, and every network call has its own limit: every `curl` gets `--connect-timeout 10 --max-time
60`, every `docker pull` runs under `timeout` (at most 300 s, never past the deadline), the Docker
CLI's setup calls under `timeout 30`. A blackholed network therefore ends the run near the
deadline instead of hours later (each Secret Manager attempt used to be able to wait libcurl's 300 s
default connect timeout, and a stalled transfer forever, holding the lock).

Worst case with the defaults, before hooks:

| Part | At most |
| --- | --- |
| Data disk link | 30 s |
| `mkfs.ext4` on a blank 10 GB disk (first boot only) | not bounded by the script; with `lazy_itable_init=0` probably one to two minutes on pd-standard (not measured) |
| Docker daemon, credential helper, network | 60 s + 90 s |
| Pulls, adapter check, MariaDB's secret, MariaDB health (shared deadline; a first initialisation's temporary server may take up to `mariadb_init_wait_seconds`, default 5 min, of it) | 15 min |
| Overshoot past the deadline by the attempt in flight | about 2 min (a token call and a Secret Manager call, 60 s each) |
| Other secrets after the deadline: one last attempt each | about 2 min per missing secret |

So about **20 minutes**, plus 4 minutes for T23's two extra secrets if both are missing, plus each
hook's `hook_timeout_minutes` (default 30; OpenEMR's first boot takes about 15). Calls to the local
Docker daemon (`docker inspect`, `run`, `start`, `stop`) are not under `timeout`; `docker stop` is
bounded by its 120 s.

### The MLLP adapter command

Spike S4's flags, with error-message logging off by default (output `mllp_adapter_command`):

```bash
docker run --detach --name mllp-adapter --label pulsebridge.config=<hash> \
  --restart unless-stopped --publish 2575:2575 --log-opt max-size=10m --log-opt max-file=3 \
  gcr.io/cloud-healthcare-containers/mllp-adapter@sha256:92eaa0fc5fcd0eb7ff62bd28c9140dcd7582bfec20790e5b203ca8dc75c4a90f \
  /usr/mllp_adapter/mllp_adapter \
  --port=2575 --receiver_ip=0.0.0.0 \
  --hl7_v2_project_id=<project> --hl7_v2_location_id=<location> \
  --hl7_v2_dataset_id=<dataset> --hl7_v2_store_id=<store> \
  --log_error_msg=false --export_stats=false
```

- The image has no entrypoint, so the binary path is the first word of the command.
- The four store flags are short names, taken unchanged from the healthcare module's
  `mllp_adapter_store_flags`. A resource path is rejected by validation.
- `--export_stats=false`: the default sends metrics to Cloud Monitoring, which the VM's account may
  not write.
- **No message content is logged by default.** Upstream's flag help
  (https://github.com/GoogleCloudPlatform/mllp/blob/master/mllp_adapter/mllp_adapter.go) says of
  `--log_ack` "These info logs will contain sensitive data", and of `--log_nacked_msg`,
  `--log_input_msg_in_base64` and `--log_error_msg` "These error logs will contain sensitive
  data". The first three are not passed (default false). `--log_error_msg` is passed as
  `var.mllp_adapter_log_error_messages`, **default false**, because the adapter's logs reach Cloud
  Logging. S4 ran with it on. With it off, the adapter's own log loses the store's NACK reason;
  the cuff simulator still receives the NACK and records its MSA-3 reason in the AuditEvent.
- `--logtostderr` is not passed, following S4 (the AC). The adapter logs with glog, which does
  register that flag; S4's crash loop was most likely caused by the wrong store-flag name alone,
  and S2 ran the same digest with `--logtostderr`. The consequence of leaving it out: glog writes
  INFO and WARNING lines to log files inside the container's temporary directory, and only ERROR
  and above go to stderr, so `docker logs` and Cloud Logging see errors only. Trying
  `--logtostderr` is on the first-boot list below.
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

## Patching the VM

The VM does **not** patch itself:

- COS's in-place automatic update is switched off explicitly (`cos-update-strategy =
  update_disabled` in metadata). It could not work here anyway: "In-place updates are not
  supported on Container-Optimized OS VMs with Unified Extensible Firmware Interface (UEFI) Secure
  Boot enabled", and "In milestone 117 and later, the automatic updates feature is disabled by
  default" (https://docs.cloud.google.com/container-optimized-os/docs/concepts/auto-update).
  Setting the key makes the behaviour independent of a milestone's default.
- The boot image is in `ignore_changes`, so an unrelated `terraform apply` never replaces the VM
  because the `cos-stable` family moved. The cost: the VM stays on the image it was created from
  (output `boot_image`), and **a change to `boot_image` takes effect only when the VM is
  replaced**.

Patching is therefore a deliberate replacement. From an env root (`infra/envs/dev` or
`infra/envs/prod`), where this module sits inside the `_stack` composition, the address is
`module.stack.module.edge_vm.google_compute_instance.this`:

```bash
terraform apply -replace='module.stack.module.edge_vm.google_compute_instance.this'
```

(`module.edge_vm.google_compute_instance.this` is the address only in a root that calls this
module directly.)

What it does: Terraform deletes the VM and its boot disk, and creates a new VM from the newest
image of the family (or the image `boot_image` names). The **data disk and the reserved internal
address survive** and are attached to the new VM, so MLLP_HOST and the database are unchanged. The
new VM boots, the startup script finds the ext4 filesystem (no format), recreates the swap file,
pulls the images again and starts the containers; T23's hook starts OpenEMR on its existing site
files. Expect about **15 minutes of downtime** (S2: the OpenEMR pull is most of it; no first-time
setup, since the sites directory is on the data disk). Recommended cadence: **monthly**, and
whenever the COS release notes for the milestone announce a security fix. Check the result with
the `boot_image` output and the startup log (`edge VM startup: done, all steps ok`).

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
  "Private Google Access" page says VMs with only internal addresses "can reach the external IP
  addresses of Google APIs and services" (https://docs.cloud.google.com/vpc/docs/private-google-access).
  On "Configure Private Google Access"
  (https://docs.cloud.google.com/vpc/docs/configure-private-google-access), the "Summary of
  configuration options" table says that with the **default domains** "You access Google APIs and
  services through their public IP addresses, so no special DNS configuration is required", given
  a default route to `default-internet-gateway` and egress allowed (both true in project-base,
  which keeps the default route and denies only ingress). That page does not list the domains the
  default option covers; its "Domain options" list, which includes `pkg.dev or *.pkg.dev` and
  `gcr.io or *.gcr.io`, is the list for the `private.googleapis.com` VIP. So the documentation
  shows Artifact Registry and gcr.io are Google services reachable over Private Google Access, but
  the default-domains path for `*.pkg.dev` is an inference. **Assumption to verify at T25:** the
  first boot's log shows both pulls succeeding. **Fallback if not:** set `enable_cloud_nat = true`
  in project-base (about $4.70 a month plus $0.045 per GiB, billed hourly); nothing in this module
  changes, because the same image paths are then reached through NAT.

## Identity and scopes

- The VM runs as the identity module's `edge-vm` account; the Compute Engine default account is
  refused by validation (it has project-wide Editor in older projects).
- Scopes are `cloud-platform` only. Secret Manager's access-control page: "the underlying instance
  or node must have the cloud-platform OAuth scope", so no narrower set works, and Google's
  guidance is "set the full cloud-platform access scope on the instance, then control the service
  account's access using IAM roles".
- What the account may do, each grant made by the module that owns the resource:
  `roles/healthcare.hl7V2Ingest` on the HL7v2 store only (healthcare), its only Healthcare
  permission; `secretAccessor` on the three database/OpenEMR password secrets and version-adding on
  the OpenEMR key secret (identity); `roles/logging.logWriter` (identity); and
  `roles/artifactregistry.reader` on the Docker Hub mirror repository only (**project-base**, when
  its `edge_vm_service_account_email` is set). This module creates no IAM resource; a static check
  enforces that.

### Trust boundary

- **Root on the VM = whoever can change its metadata.** Anyone with
  `compute.instances.setMetadata` on the VM plus `iam.serviceAccounts.actAs` on its service account
  can change the startup script or a hook and so run anything as root on the next boot (or at
  once with a reset). That is the same boundary as the startup script itself; keep those
  permissions to the deployer and CI.
- **Every container can reach the metadata server** (169.254.169.254) and get an access token for
  the VM's account: HL7v2 ingest, reading the three database secrets, adding versions to the
  OpenEMR key secret, writing logs, reading the mirror. The MLLP adapter needs that token. **For
  T23:** OpenEMR is a large PHP application reachable from `fhir-proxy`; consider blocking
  169.254.169.254 for its container (for example an `iptables -I DOCKER-USER -s <openemr address>
  -d 169.254.169.254 -j DROP` rule in the hook) so a compromise of OpenEMR does not yield the VM's
  token.

## Secrets

No password or key is in Terraform, in the startup script, in metadata or in state:

- The only database password T21 needs, MariaDB's root password, lives in the identity module's
  `db-root-password` secret. The VM reads it at boot **with its own account**: an access token
  from the metadata server, sent to curl on standard input (not as an argument, so it does not
  show in the process list), then `secretmanager.googleapis.com/v1/<secret>/versions/latest:access`.
  The value goes into a mode-600 file and reaches MariaDB as `MARIADB_ROOT_PASSWORD_FILE`, so it
  is not in `docker inspect` either. It is never logged; the tests check this with sentinels.
- Each step of writing the file (decode, `chmod 600`, `mv` into place) is checked; a failure
  removes the temporary file and fails the step, leaving no partial secret behind.
- A secret file is fetched **once** and then kept: MariaDB takes its root password only when it
  initialises an empty data directory, so the file must keep matching the database. Rotating it
  means changing it inside MariaDB, then the file.
- Missing secrets share the one deadline: each round tries every missing secret once, so two
  missing secrets do not wait 15 minutes each in turn. If MariaDB's own secret has no version yet
  (first apply, before `scripts/secrets` from T27 has run), the script logs
  `ERROR: secret mariadb-root-password ... 404` at the deadline, keeps the adapter running, and
  leaves MariaDB stopped. After adding the version: `sudo google_metadata_script_runner startup`,
  or reset the VM.
- **For T27:** write secret versions without a trailing newline (`printf '%s'`, not `echo`).
  MariaDB's entrypoint reads `*_FILE` values with shell command substitution, which drops trailing
  newlines, but other readers may not.

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
  reads secrets from `$EDGE_SECRETS_DIR/<name>`, **stripping a trailing newline** if present.
- **Bind mounts with `--mount type=bind,...`, never `-v <path>:...`.** `-v` silently creates a
  missing source directory: before the data disk is mounted that would be an empty directory on
  the in-memory `/mnt/disks`, and OpenEMR would run its first-time setup again on it.
- Environment given to a hook: `EDGE_DATA_DIR`, `EDGE_SECRETS_DIR`, `EDGE_MARIADB_DIR`,
  `EDGE_OPENEMR_DIR`, `EDGE_STATE_DIR`, `EDGE_DOCKER_NETWORK`, `EDGE_MARIADB_CONTAINER`,
  `EDGE_MIRROR_URL`, `EDGE_BOOT_WAIT_SECONDS`, `EDGE_HOOK_TIMEOUT_SECONDS`, plus `HOME` and
  `DOCKER_CONFIG` (credential helper already configured).
- Each hook runs under `timeout` (`hook_timeout_minutes`, default 30). A non-zero exit or a
  timeout is logged as `ERROR: hook <name>: FAILED` and makes the script exit non-zero; the next
  hook still runs.
- Hooks run with the run lock's file descriptor (9) closed, so they cannot hold the lock, but they
  must not leave background processes behind: `timeout` stops only the hook itself, and a
  leftover process would run unsupervised.
- A hook runs even if an extra secret could not be copied; it must check for the files it needs.
- A hook that passes a password to OpenEMR as an environment value puts it in the container's
  configuration on the boot disk (`docker inspect`); prefer files where the image supports them.
- Instance metadata holds 256 KB per value and 512 KB in total; a precondition checks this in
  bytes (UTF-8), not characters.

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
`pd-standard`, which is billed per GB-month). With `data_disk_snapshot_days = 0` (the default)
`terraform destroy` removes the VM, both disks and the address: nothing billable is left, and a
destroyed data disk is gone.

### Snapshots of the data disk

`data_disk_snapshot_days` above 0 creates a **snapshot schedule** (a Compute Engine resource
policy, `google_compute_resource_policy.data_snapshots`) and attaches it to the data disk: one
snapshot a day at 04:00 UTC, stored in the VM's region, each kept that many days. The env roots
pass 7 in prod (real clinic data) and 0 in dev.

- **Cost**: snapshot storage, billed per GB-month of changed blocks (snapshots are incremental).
  A fresh OpenEMR database is a few hundred MB, so a week of daily snapshots is a fraction of a
  GB: cents a month (Compute Engine snapshot pricing, standard snapshots in one region; the exact
  rate is not confirmed here). Not covered by the free tier.
- **They outlive a destroy, on purpose**: `on_source_disk_delete = "KEEP_AUTO_SNAPSHOTS"` keeps
  the snapshots when the disk is deleted, so an accidental `terraform destroy` of prod can be
  undone by creating a disk from the last snapshot. The consequence: after a deliberate destroy
  they are **still billed** until deleted (`gcloud compute snapshots list --filter=
  "sourceDisk~<prefix>-edge-data" --project <project>`, then `gcloud compute snapshots delete`).
  A VM replacement (patching) keeps the disk and its schedule.
- **Restoring**: create a disk from a snapshot with the data disk's name, then apply (Terraform
  adopts nothing by itself: import the new disk, or restore onto the existing disk's contents by
  hand). Not verified.
- The deployer creates the policy and attaches it with `roles/compute.instanceAdmin.v1`
  (bootstrap), which is understood to include `compute.resourcePolicies.create` and
  `compute.disks.addResourcePolicies`; confirm on the first prod apply.

## Tests

```bash
make tf-test    # runs everything below with Google credentials removed from the environment
```

- `tests/edge_vm.tftest.hcl` (`terraform test`, `mock_provider`, `command = plan`): no
  `access_config`; machine type default and variable; dedicated service account and the
  `cloud-platform` scope only; Shielded VM; the separate disk attached read-write as `edge-data`
  and mounted by that name; disk sizes inside the free tier; metadata keys (no SSH keys, nothing
  secret-looking, OS Login on, `cos-update-strategy = update_disabled`); MariaDB's password passed
  as a file; the adapter command (S4's flags, error-message logging off by default and on when
  asked), also in the rendered script; digests; hooks and extra secrets reaching the script; the
  `boot_image` output; the metadata size limit counted in bytes (a hook of 100,000 three-byte
  characters is refused, one of 50,000 plans).
- `tests/edge_vm.tftest.hcl` also checks that the rendered script carries the wrapper file verbatim
  in its quoted here-document and the first-initialisation wait (300 by default, or the variable).
- `tests/validation.tftest.hcl`: `mariadb_init_wait_seconds` below 60, fractional, or over half of
  `boot_wait_minutes` is rejected (exactly half plans); a tag instead of a digest (adapter and MariaDB), the default
  Compute account, a store path instead of a short name, a missing MariaDB secret, a secret with a
  version, a non-COS image, a zone outside the subnet's region, and more are each rejected. The
  single-quote precondition cannot be reached through any input today (every input that reaches
  the script is validated to exclude quotes); it stays as defence in depth and is not tested.
- `tests/static_checks.sh`: fails on any `access_config`, any firewall resource, any IAM resource,
  any use of the default Compute account, any service account key or `--credentials`, any
  secret-looking metadata key or secret-valued variable, any hardcoded project, `set -x` in the
  script, or a child module.
- `tests/startup_script.sh`: shellcheck and `bash -n`; a check that the template assigns every
  setting the script reads; and behaviour tests that run the real script in a sandbox whose PATH
  holds only fakes (`tests/fakes/fake-command` as `docker`, `curl`, `mount`, `blkid`, `wipefs`,
  `mkfs.ext4`, `timeout`, `date`, `sleep`, ...). Time is faked: `sleep` and simulated timeouts
  advance a clock that `date +%s` reads, so deadline loops run instantly and each case checks how
  long the run would have taken. The fake `blkid` behaves like util-linux (exit 2 for blank,
  unreadable and unrecognised alike). Cases:
  - format decision: a truly blank disk is formatted once; exit 2 with data at the start, with a
    zeroed primary superblock but data right after it, with data only in the middle or only at the
    end, with an unreadable device, or with a wipefs signature: never formatted, never mounted;
    ext4 mounted without format; another filesystem or blkid exit 4: stopped; fsck exit 4: not
    mounted;
  - idempotency: a second run starts, stops, restarts, replaces and pulls nothing (checked on the
    fakes' call log); a reboot mounts and starts, creates nothing; a concurrent run refuses while
    the lock is held; a reboot without the disk never starts MariaDB;
  - replacement: a changed adapter or MariaDB configuration is stopped with `docker stop --time
    120` before `docker rm` (never `rm --force`); MariaDB has `--stop-timeout 120` and no published
    port;
  - time: with every curl call hanging, with `docker pull` hanging, with failing pulls, a missing
    secret or an unhealthy MariaDB, the run ends within the deadline plus a bounded overshoot, and
    no call ran without a time limit;
  - secrets: a missing MariaDB secret (no MariaDB, adapter up, no file left); two missing T23
    secrets (MariaDB still starts and becomes healthy, both retried in the same rounds, one shared
    deadline, hooks still run); a failing `chmod` (step fails, nothing left behind); sentinels
    check that neither the secret, its base64 form nor the access token appears in the log or in
    any command line;
  - adapter: exact `docker run` arguments, crash-loop detection with its log lines, still started
    when the disk is missing;
  - hooks: run after MariaDB is healthy, with the documented environment, under `timeout`, without
    the lock's file descriptor; a failing first hook does not stop the second;
  - preflight: a missing `docker` or `timeout` is named and nothing else runs;
  - MariaDB's first initialisation (T42): the container is started with `--entrypoint bash -c
    <wrapper>` and the wrapper's text is exactly `scripts/mariadb-entrypoint-wrapper.sh`; the fake
    `docker run` leaves an initialised (or, with `FAKE_MARIADB_INIT=fail`, a half-initialised) data
    directory like the real entrypoint. A failed first initialisation is reported with MariaDB's
    log lines; the next run moves the directory aside with its files, removes an older copy,
    stops and removes the leftover container and initialises again. A directory with the marker,
    and one without it that holds an `openemr` database, are left untouched; one with system
    schemas only is moved;
  - the wrapper itself, run with real bash against a stand-in entrypoint that has the same wait
    loop: a server that needs 45 tries fails with the stand-in alone (the prod failure) and
    succeeds through the wrapper, which passes the command through unchanged and changes only
    the loop bound; a server that never answers still fails (the wait is bounded); an initialised
    directory runs the official entrypoint itself; the loop found twice or not at all, or a wait
    that is not a number, is refused and nothing runs.

  shellcheck is taken from PATH or `$SHELLCHECK`; without it the lint is skipped with a warning,
  except when `CI=true`. The files are clean with shellcheck 0.9.0 (the version on CI's
  ubuntu-24.04 runner) and 0.11.0 at `-S style`.

## Not verified

Nothing in this module has been applied: no VM, disk or grant was created, and the script has
run only against fakes. Verify at T25, in this order (each step depends on the ones before):

1. **The VM boots** COS with Shielded VM on an e2-micro, and the startup script runs: serial port
   shows `pulsebridge-edge: edge VM startup: begin`. The metadata shows
   `cos-update-strategy=update_disabled`.
2. **Every command the script needs exists on COS**: the preflight step logs any missing one
   (`blkid`, `mkfs.ext4`, `fsck.ext4`, `mkswap`, `swapon`, `curl`, `dd`, `timeout`, `tr`, `wc`,
   `sha256sum`, `stat`, `flock`; `wipefs`, `fallocate` and `resize2fs` are optional). The COS pages
   show `curl`, `fsck.ext4` and `mount` in use on COS; the others are assumed (coreutils and
   util-linux).
3. **The format decision on a real disk**: the new data disk reads as zeros through `dd` on
   `/dev/disk/by-id/google-edge-data`, its size is read from `/sys/class/block/<dev>/size` (the
   block-device branch is untested: the sandbox uses a regular file), `blkid -p` exits 2,
   `wipefs --no-act` prints nothing, and the disk is formatted once; **after a reboot** it is
   mounted again without formatting ("has an ext4 filesystem; not formatting").
4. **Swap**: `swapon --show` lists the file. Swap on COS is not documented for Compute Engine VMs:
   the evidence is indirect (GKE offers node swap on COS nodes; COS release notes mention zswap).
   If `swapon` fails, the step logs an error and everything else still runs; S2 found the tuned
   stack survived without swap.
5. **Artifact Registry and gcr.io over Private Google Access**: both pulls succeed with no NAT.
   If not, turn on Cloud NAT in project-base (fallback above).
6. **The mirror grant** (now in project-base, which needs `edge_vm_service_account_email`):
   `roles/artifactregistry.reader` on the repository is enough for a pull through a remote
   repository (Docker Hub fetched on Google's side).
7. **docker-credential-gcr** writes to `$HOME/.docker` under `/var/lib/pulsebridge-edge` and
   `docker pull` uses it (the docker CLI reads `DOCKER_CONFIG`).
8. **The adapter** stays up (`mllp-adapter is up and stayed up`), the published port is reachable
   from the cuff simulator through the project-base firewall and COS's host firewall, and a test
   message gets `AA` with **only** `roles/healthcare.hl7V2Ingest` on the store (S4 proved ingest
   only with project Editor).
9. **`--logtostderr`**: start a second adapter by hand with `--logtostderr` added and see whether it
   stays up. If it does, consider adding it so INFO lines reach `docker logs` (glog accepts it;
   S2 ran it on this digest).
10. **The secret**: with a version added (T27), the file appears with mode 600; the Secret Manager
    response parsing (a `sed` over the JSON, as COS has no `jq`) works on the real response.
11. **MariaDB** initialises on the bind-mounted directory, becomes `healthy`, and
    `--health-start-interval` is accepted (it needs Docker 25 or newer; COS milestone 125 ships
    Docker 27.5.1). A configuration change replaces it with a clean shutdown (MariaDB's log shows
    "Shutdown complete", not crash recovery on the next start).
12. **Graceful shutdown on VM stop**: stop the VM and check MariaDB's log on the next boot; find
    COS's `TimeoutStopSec` for `docker.service` (`systemctl show docker -p TimeoutStopUSec`).
13. **After a reboot** both containers come back without help and the script reports all steps ok.
14. **The time limits**: `timeout` and curl's `--connect-timeout`/`--max-time` behave on COS as in
    the sandbox (for example by blocking the Secret Manager host for one boot in a test project).
15. **Cloud Logging**: the logging agent ships the startup script's journal lines (the COS page says
    it exports "some system and container logs").
16. **The crash-loop check** on a real crash: Docker's restart back-off may space restarts more
    than 15 seconds apart after a long loop; the check also catches the `restarting` status.
17. **Patching**: `terraform apply -replace=...` keeps the data disk and address, and the
    `boot_image` output reports the new image.
18. **The e2-micro free tier** applies to this VM (billing report after the first month), and the
    e2-micro list price outside it.
19. **Docker log options** (`--log-opt max-size`) are compatible with the COS logging agent.
20. **MariaDB's first initialisation through the wrapper (T42)**, on a new data disk: `docker logs
    mariadb` shows `[PulseBridge]: first initialisation: the temporary server may take up to
    300s`, then the entrypoint's "MariaDB init process done"; the startup log shows `first
    initialisation of a new database completed`. Not run on a VM: the wrapper was tested with a
    stand-in entrypoint, and its one-line change was checked against the 11.8.9 entrypoint's source
    from MariaDB's repository, not against the file inside the pinned image digest. Also check
    that the container's `/tmp` is writable and executable for the copy (it is the image's own
    directory in the container layer, not a host mount).
21. **Recovery from a half-initialised directory (T42)** on a real disk: the directory is renamed
    to `mariadb.incomplete-<time>`, the leftover container is stopped and removed, and the next
    initialisation completes. Whether the real half-initialised directory holds only `mysql`,
    `sys` and files (as the rule assumes) was reasoned from the entrypoint, not listed on prod; a
    directory with an unexpected extra directory would be left alone with a WARNING (safe, but
    then needs the manual steps in "First initialisation of MariaDB").
22. **The first boot after T42 on an existing VM** (dev, prod): the MariaDB container is replaced
    once with a clean shutdown, the wrapper logs `data directory already initialised: running the
    official entrypoint unchanged`, and the startup log says `(existing database)`.

Other assumptions about other modules (exact output names): `project-base.edge_subnet_id`,
`edge_vm_network_tag`, `mllp_port`, `dockerhub_mirror_url` (non-null: `create_dockerhub_mirror`
must stay true); `identity.service_account_emails["edge-vm"]`,
`secret_resource_ids["db-root-password"]`; `healthcare.mllp_adapter_store_flags`. project-base must
get `edge_vm_service_account_email` so it grants the mirror read access.

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
| [google_compute_address.internal](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/compute_address) | resource |
| [google_compute_disk.data](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/compute_disk) | resource |
| [google_compute_disk_resource_policy_attachment.data_snapshots](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/compute_disk_resource_policy_attachment) | resource |
| [google_compute_instance.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/compute_instance) | resource |
| [google_compute_resource_policy.data_snapshots](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/compute_resource_policy) | resource |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_boot_disk_size_gb"></a> [boot\_disk\_size\_gb](#input\_boot\_disk\_size\_gb) | Boot disk size in GB. It holds the COS system partitions, Docker's image store (OpenEMR is about 3.6 GB unpacked, spike S2) and the swap file. 20 GB boot plus 10 GB data stays inside the free tier's 30 GB-months of standard persistent disk. | `number` | `20` | no |
| <a name="input_boot_image"></a> [boot\_image](#input\_boot\_image) | Boot image: a Container-Optimized OS image or image family from the cos-cloud project. The startup script relies on COS (Docker and docker-credential-gcr preinstalled, read-only root, stateless /etc), so other images are refused. | `string` | `"projects/cos-cloud/global/images/family/cos-stable"` | no |
| <a name="input_boot_wait_minutes"></a> [boot\_wait\_minutes](#input\_boot\_wait\_minutes) | One shared deadline, counted from the start of the startup script, for everything it waits on that can be slow on first boot: image pulls, secret versions not yet added, MariaDB becoming healthy. When it passes, the script logs an error and gives up for this boot. Every network call also has its own timeout, so the deadline is overshot by at most about a minute. Hooks have their own limit (hook\_timeout\_minutes). | `number` | `15` | no |
| <a name="input_data_disk_size_gb"></a> [data\_disk\_size\_gb](#input\_data\_disk\_size\_gb) | Size in GB of the separate persistent disk for MariaDB and OpenEMR site data. A fresh OpenEMR database is a few hundred MB. Growing it later is done in place (the startup script grows the filesystem on the next boot); shrinking is not possible. | `number` | `10` | no |
| <a name="input_data_disk_snapshot_days"></a> [data\_disk\_snapshot\_days](#input\_data\_disk\_snapshot\_days) | Daily snapshots of the data disk, kept this many days; 0 (default): none. Kept snapshots outlive a destroy (KEEP\_AUTO\_SNAPSHOTS) and are billed until deleted. | `number` | `0` | no |
| <a name="input_disk_type"></a> [disk\_type](#input\_disk\_type) | Persistent disk type for both disks. pd-standard is the only type the free tier covers; pd-balanced is faster (spike S2 suspected the slow disk for the 5-minute OpenEMR first boot) but billed. | `string` | `"pd-standard"` | no |
| <a name="input_dockerhub_mirror_url"></a> [dockerhub\_mirror\_url](#input\_dockerhub\_mirror\_url) | Registry path of the Docker Hub pull-through cache from project-base (output `dockerhub_mirror_url`, <location>-docker.pkg.dev/<project>/<repository>). MariaDB (and OpenEMR, T23) are pulled from here. Read access for the VM's account is granted by project-base, which owns the repository (set its `edge_vm_service_account_email`). | `string` | n/a | yes |
| <a name="input_edge_subnet_id"></a> [edge\_subnet\_id](#input\_edge\_subnet\_id) | ID of the edge subnet from project-base (output `edge_subnet_id`, projects/<project>/regions/<region>/subnetworks/<name>). The subnet must have Private Google Access on: the VM has no external IP and pulls its images from Artifact Registry and gcr.io through it. | `string` | n/a | yes |
| <a name="input_enable_cloud_logging"></a> [enable\_cloud\_logging](#input\_enable\_cloud\_logging) | Turn on Container-Optimized OS's logging agent (metadata google-logging-enabled), so startup-script and container logs reach Cloud Logging. The VM's account has roles/logging.logWriter from the identity module. The agent uses some of the 1 GB of RAM; off still leaves the serial console. | `bool` | `true` | no |
| <a name="input_hl7v2_store_flags"></a> [hl7v2\_store\_flags](#input\_hl7v2\_store\_flags) | The four store coordinates the MLLP adapter takes as flags (healthcare output `mllp_adapter_store_flags`). Each is a short name, not a resource path: --hl7\_v2\_store\_id=<store name>, as spike S4 recorded. | <pre>object({<br/>    hl7_v2_project_id  = string<br/>    hl7_v2_location_id = string<br/>    hl7_v2_dataset_id  = string<br/>    hl7_v2_store_id    = string<br/>  })</pre> | n/a | yes |
| <a name="input_hook_timeout_minutes"></a> [hook\_timeout\_minutes](#input\_hook\_timeout\_minutes) | Longest one hook (T23) may run on one boot before it is stopped and logged as failed. OpenEMR's first boot takes about 15 minutes (spike S2), so the default leaves room. Hooks run one after another, so the worst case for all hooks is this times their number. | `number` | `30` | no |
| <a name="input_hooks"></a> [hooks](#input\_hooks) | Extra boot steps, as { <name> = <bash script> }. Each is stored in instance metadata as<br/>`pulsebridge-hook-<name>` and run by the startup script on every boot, in name order, after<br/>MariaDB is healthy. This is the seam for T23 (OpenEMR container, client registration). A hook<br/>must be idempotent and must contain no secret (metadata is readable by anyone who can view the<br/>instance); it reads secrets from the files listed in `secret_files`. | `map(string)` | `{}` | no |
| <a name="input_internal_ip"></a> [internal\_ip](#input\_internal\_ip) | Optional fixed internal IPv4 address for the VM inside the edge subnet. Null lets Google pick a free one, which is then reserved so it survives VM replacement (the cuff simulator's MLLP\_HOST and OpenEMR's site address stay valid). | `string` | `null` | no |
| <a name="input_labels"></a> [labels](#input\_labels) | Labels added to the VM, its disks and its address. | `map(string)` | `{}` | no |
| <a name="input_machine_type"></a> [machine\_type](#input\_machine\_type) | Machine type of the VM. e2-micro (2 shared vCPUs, 1 GB) is the free-tier size that spike S2 measured as enough for one reviewer at a time; e2-small eases the CPU-burst limit but is outside the free tier and billed by the hour. | `string` | `"e2-micro"` | no |
| <a name="input_mariadb_image"></a> [mariadb\_image](#input\_mariadb\_image) | MariaDB image path inside the Docker Hub mirror, pinned by digest (an optional tag before the digest is kept for readability only). Default: mariadb 11.8 as tested in spikes S1 and S2; the digest was confirmed current on Docker Hub on 2026-11-15. | `string` | `"library/mariadb:11.8@sha256:d36f18d65a0459510b8b0f643b226a6d299e3206486e2b64ff20754a6d291707"` | no |
| <a name="input_mariadb_init_wait_seconds"></a> [mariadb\_init\_wait\_seconds](#input\_mariadb\_init\_wait\_seconds) | Longest MariaDB's FIRST initialisation waits for its temporary server to start (the server that sets the root password and creates the health-check users). The official image waits 30 s, which prod's e2-micro exceeded at boot (T42); the startup script's entrypoint wrapper raises it to this. Used only on an empty data directory; an initialised database starts exactly as the image ships it. At most half of boot\_wait\_minutes, so the startup script is still waiting when the server answers. | `number` | `300` | no |
| <a name="input_mllp_adapter_image"></a> [mllp\_adapter\_image](#input\_mllp\_adapter\_image) | Google's MLLP adapter image, pinned by digest. The image has no version tags, only `latest` (spike S4), so a digest is the only pin. To check for a newer build without pulling: `crane digest gcr.io/cloud-healthcare-containers/mllp-adapter:latest`, or a HEAD request for /v2/cloud-healthcare-containers/mllp-adapter/manifests/latest on gcr.io and read Docker-Content-Digest. Default: the digest spike S4 tested, still `latest` on 2026-11-15. | `string` | `"gcr.io/cloud-healthcare-containers/mllp-adapter@sha256:92eaa0fc5fcd0eb7ff62bd28c9140dcd7582bfec20790e5b203ca8dc75c4a90f"` | no |
| <a name="input_mllp_adapter_log_error_messages"></a> [mllp\_adapter\_log\_error\_messages](#input\_mllp\_adapter\_log\_error\_messages) | Pass --log\_error\_msg=true to the MLLP adapter, so it logs the store's error text for each NACK. Off by default: upstream's flag help says "These error logs will contain sensitive data", and the logs reach Cloud Logging. Spike S4 ran with it on. Off loses only the NACK reason in the adapter's own log; the cuff simulator still receives the NACK and records its MSA-3 reason in the AuditEvent. | `bool` | `false` | no |
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
| <a name="output_boot_image"></a> [boot\_image](#output\_boot\_image) | Boot image the VM was created from, as recorded by the provider. Because the image is in ignore\_changes, this stays the creation-time image until the VM is replaced (README, "Patching the VM"). |
| <a name="output_data_disk_snapshot_policy"></a> [data\_disk\_snapshot\_policy](#output\_data\_disk\_snapshot\_policy) | Name of the data disk's daily snapshot schedule, or null when data\_disk\_snapshot\_days is 0. |
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
| <a name="output_mllp_adapter_command"></a> [mllp\_adapter\_command](#output\_mllp\_adapter\_command) | The exact command the MLLP adapter container runs: the binary path plus spike S4's flags, with error-message logging off by default. |
| <a name="output_mllp_adapter_container"></a> [mllp\_adapter\_container](#output\_mllp\_adapter\_container) | Name of the MLLP adapter container (for `docker logs mllp-adapter` on the VM). |
| <a name="output_mllp_host"></a> [mllp\_host](#output\_mllp\_host) | Host the cuff simulator sends MLLP to (same as internal\_ip; named for T22's MLLP\_HOST). |
| <a name="output_mllp_port"></a> [mllp\_port](#output\_mllp\_port) | Port the MLLP adapter is published on at mllp\_host. |
| <a name="output_openemr_data_dir"></a> [openemr\_data\_dir](#output\_openemr\_data\_dir) | Directory on the data disk reserved for T23: OpenEMR's sites directory (/var/www/localhost/htdocs/openemr/sites in the container). |
| <a name="output_secrets_dir"></a> [secrets\_dir](#output\_secrets\_dir) | Mode-700 directory on the data disk with one mode-600 file per entry of secret\_files (for example mariadb-root-password). |
| <a name="output_service_account_email"></a> [service\_account\_email](#output\_service\_account\_email) | Service account the VM runs as (passed in from the identity module). |
| <a name="output_state_dir"></a> [state\_dir](#output\_state\_dir) | Boot-disk directory for the startup script's state: docker client configuration (.docker), the run lock and the fetched hook scripts (hooks/). This is the units directory: there is no compose file on COS. |
| <a name="output_zone"></a> [zone](#output\_zone) | Zone of the edge VM and its data disk. |
<!-- END_TF_DOCS -->
