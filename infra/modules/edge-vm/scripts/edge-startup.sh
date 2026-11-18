# shellcheck shell=bash
# shellcheck disable=SC2154 # the EDGE_* configuration is assigned in part 1 (the rendered template)
#
# Part 2 of 2 of the edge VM startup script: the logic. Terraform appends this file unchanged to
# the configuration block rendered from templates/startup-config.sh.tftpl, so this file contains no
# Terraform interpolation and can be linted and tested on its own (tests/startup_script.sh).
#
# What it does, on every boot, in this order:
#   1. mounts the data disk at EDGE_DATA_DIR, formatting it ONLY if it is provably blank;
#   2. turns on the swap file (COS keeps no /etc/fstab across boots, so this runs every boot);
#   3. waits for Docker and configures docker-credential-gcr for the Docker Hub mirror;
#   4. starts Google's MLLP adapter and checks a few seconds later that it stays up;
#   5. copies the secrets it needs from Secret Manager to files on the data disk (once);
#   6. starts MariaDB with spike S2's low-memory settings and waits until it is healthy; a data
#      directory left half-initialised by an earlier, failed first initialisation is moved aside
#      first, and the first initialisation gets a longer wait for its temporary server (T42);
#   7. runs the hook scripts (T23: OpenEMR) from instance metadata.
#
# Safe to run again: every step checks what is already there first. Nothing is formatted twice,
# a container that is already running with the same configuration is left alone, and a secret file
# that exists is kept. The adapter does not depend on the data disk or on Secret Manager, so a
# problem there still leaves the HL7v2 path up. Any failed step makes the script exit non-zero
# after the others have run, with an ERROR line naming the step.
#
# Time: everything that waits for something slow shares ONE deadline (EDGE_BOOT_WAIT_SECONDS from
# the start of the run), and every network call has its own timeout, so a blackholed network
# cannot hold the script (and its lock) for hours. Hooks have their own per-hook limit.
#
# Secrets: secrets are read from Secret Manager into mode-600 files and MariaDB gets its password
# as a file (MARIADB_ROOT_PASSWORD_FILE). No secret appears in a log line, in a command line or in
# a container's environment. The access token is passed to curl on standard input, not as an
# argument, so it does not show in the process list either.
# There is deliberately no `set -x` anywhere in this file.

set -uo pipefail

# ---- Test seams. The defaults are the real VM; tests/startup_script.sh points them at a sandbox.
EDGE_PROC_MOUNTS="${EDGE_PROC_MOUNTS:-/proc/mounts}"
EDGE_PROC_SWAPS="${EDGE_PROC_SWAPS:-/proc/swaps}"
EDGE_SYS_BLOCK="${EDGE_SYS_BLOCK:-/sys/class/block}"
EDGE_METADATA_URL="${EDGE_METADATA_URL:-http://metadata.google.internal/computeMetadata/v1}"
EDGE_SECRETMANAGER_URL="${EDGE_SECRETMANAGER_URL:-https://secretmanager.googleapis.com/v1}"
EDGE_POLL_SECONDS="${EDGE_POLL_SECONDS:-10}"                   # pause between retries
EDGE_ADAPTER_CHECK_SECONDS="${EDGE_ADAPTER_CHECK_SECONDS:-15}" # how long the adapter must stay up

# ---- Limits for single operations (seconds).
EDGE_DISK_WAIT_SECONDS=30     # for the data disk's /dev/disk/by-id link to appear
EDGE_DOCKER_WAIT_SECONDS=60   # for the Docker daemon to answer
EDGE_PULL_TIMEOUT_SECONDS=300 # one `docker pull` attempt
EDGE_STOP_TIMEOUT_SECONDS=120 # SIGTERM to SIGKILL when a container is stopped (MariaDB flushes)
# Every curl call: give up connecting after 10 s and on the whole transfer after 60 s. Without
# these, a blackholed connection waits libcurl's default 300 s connect timeout and a stalled
# transfer waits forever.
EDGE_CURL=(curl --silent --connect-timeout 10 --max-time 60)

# ---- Layout. Data on the data disk survives VM replacement; state on the boot disk does not need to.
EDGE_MARIADB_DIR="$EDGE_DATA_DIR/mariadb" # MariaDB data directory (/var/lib/mysql)
EDGE_SECRETS_DIR="$EDGE_DATA_DIR/secrets" # mode 700; one mode-600 file per secret
EDGE_OPENEMR_DIR="$EDGE_DATA_DIR/openemr" # for T23: OpenEMR's sites directory
EDGE_HOOKS_DIR="$EDGE_STATE_DIR/hooks"    # hook scripts fetched from metadata on each boot
EDGE_DOCKER_NETWORK="pulsebridge-edge"    # user-defined bridge: containers reach "mariadb" by name
EDGE_MARIADB_CONTAINER="mariadb"
EDGE_ADAPTER_CONTAINER="mllp-adapter"
EDGE_CONFIG_LABEL="pulsebridge.config" # container label holding a hash of its run arguments
EDGE_MARIADB_SECRET="mariadb-root-password"
# The file the official MariaDB entrypoint writes into the data directory during a first
# initialisation (create_healthcheck_users); healthcheck.sh logs in with it. Its presence is the
# marker of a first initialisation that got as far as creating the users (T42).
EDGE_MARIADB_INIT_MARKER=".my-healthcheck.cnf"
EDGE_MARIADB_FRESH=0 # set to 1 by prepare_mariadb_datadir when MariaDB will initialise a new database

# COS mounts / read-only and does not let anything write /root, so docker-credential-gcr and the
# docker CLI keep their configuration under the (stateful) state directory instead.
export HOME="$EDGE_STATE_DIR"
export DOCKER_CONFIG="$EDGE_STATE_DIR/.docker"

FAILED_STEPS=()
EDGE_DEADLINE=0 # set in main: start of the run + EDGE_BOOT_WAIT_SECONDS

# ---- Logging and time ----------------------------------------------------------------------------

log() { printf '%s pulsebridge-edge: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
warn() { log "WARNING: $*"; }
error() { log "ERROR: $*" >&2; }
die() {
  error "$*"
  exit 1
}

now() { date +%s; }

# Seconds since the start of this run.
elapsed() { echo $(($(now) - EDGE_DEADLINE + EDGE_BOOT_WAIT_SECONDS)); }

# Seconds left before the shared deadline (0 when it has passed).
remaining() {
  local left=$((EDGE_DEADLINE - $(now)))
  if [ "$left" -lt 0 ]; then left=0; fi
  echo "$left"
}

# The smaller of two numbers.
min() { if [ "$1" -lt "$2" ]; then echo "$1"; else echo "$2"; fi; }

# Run one step in a subshell with errexit on, so any failing command inside it ends that step
# (and only that step). Never call run_step inside `if`, `&&` or `||`: bash switches errexit off
# for everything a condition runs, which would let a failing command pass silently.
run_step() {
  local name=$1 rc
  shift
  log "step $name: start"
  (
    set -euo pipefail
    "$@"
  )
  rc=$?
  if [ "$rc" -eq 0 ]; then
    log "step $name: ok"
  else
    error "step $name: FAILED (exit $rc)"
    FAILED_STEPS+=("$name")
  fi
  return "$rc"
}

is_mounted() {
  awk -v dir="$1" '$2 == dir { found = 1 } END { exit !found }' "$EDGE_PROC_MOUNTS"
}

# ---- Step: preflight -------------------------------------------------------------------------------

require_commands() {
  local missing=() cmd
  for cmd in "$@"; do
    command -v "$cmd" > /dev/null 2>&1 || missing+=("$cmd")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    die "commands missing on this image: ${missing[*]}"
  fi
}

# ---- Step: data disk -------------------------------------------------------------------------------

# Size of a block device (from sysfs, in 512-byte sectors) or, in the test sandbox, of a file.
device_size_bytes() {
  local dev=$1 sectors
  if [ -b "$dev" ]; then
    sectors=$(cat "$EDGE_SYS_BLOCK/$(basename "$dev")/size") || return 1
    echo $((sectors * 512))
  else
    stat -c %s "$dev"
  fi
}

# True only if <count> MiB starting at MiB <offset> can be read in full and are all zero bytes.
region_is_zero() {
  local dev=$1 offset=$2 count=$3 bytes nonzero
  bytes=$(dd if="$dev" bs=1M skip="$offset" count="$count" status=none | wc -c) || return 1
  [ "$bytes" -eq $((count * 1048576)) ] || return 1
  nonzero=$(dd if="$dev" bs=1M skip="$offset" count="$count" status=none | tr -d '\000' | wc -c) || return 1
  [ "$nonzero" -eq 0 ]
}

# A positive check that the disk is blank, needed before mkfs because blkid's "nothing found"
# (exit 2) is also what it returns when the device cannot be opened, on a read error, and for
# contents it does not recognise, such as an ext4 filesystem whose primary superblock is damaged
# but still recoverable from a backup. Blank means: the device reads, its first, middle and last
# 4 MiB are all zero bytes (a new persistent disk reads as zeros), and wipefs, if present, sees no
# signature anywhere. Anything else is "not blank". Errors are handled explicitly because the
# caller runs this as a condition, where errexit does not apply.
disk_is_blank() {
  local dev=$1 size mib offset signatures
  size=$(device_size_bytes "$dev") || return 1
  mib=$((size / 1048576))
  if [ "$mib" -lt 16 ]; then
    return 1
  fi
  for offset in 0 $((mib / 2 - 2)) $((mib - 4)); do
    region_is_zero "$dev" "$offset" 4 || return 1
  done
  if command -v wipefs > /dev/null 2>&1; then
    signatures=$(wipefs --no-act "$dev" 2>&1) || return 1
    [ -z "$signatures" ] || return 1
  fi
  return 0
}

mount_data_disk() {
  local dev fstype rc deadline

  # Compute Engine attaches the disk before boot; udev creates the /dev/disk/by-id link shortly
  # after. Wait briefly, then refuse to go on: MariaDB's data must never land on the boot disk.
  deadline=$(($(now) + EDGE_DISK_WAIT_SECONDS))
  while [ ! -e "$EDGE_DATA_DEVICE" ]; do
    if [ "$(now)" -ge "$deadline" ]; then
      die "data disk $EDGE_DATA_DEVICE not found after ${EDGE_DISK_WAIT_SECONDS}s. Is the persistent disk attached with that device name? MariaDB and hooks will not start without it."
    fi
    sleep 2
  done
  dev=$(readlink -f "$EDGE_DATA_DEVICE")

  if is_mounted "$EDGE_DATA_DIR"; then
    log "data disk already mounted at $EDGE_DATA_DIR"
  else
    # blkid -p probes the device itself. Exit 0: a filesystem was found. Exit 2: nothing was
    # recognised, which can mean blank, unreadable, or damaged; mkfs only after disk_is_blank
    # proves blank. Anything else: stop. A wrong mkfs would wipe the clinic database.
    rc=0
    fstype=$(blkid -p -o value -s TYPE "$dev") || rc=$?
    case "$rc" in
      0)
        if [ "$fstype" != "ext4" ]; then
          die "data disk $dev holds '${fstype:-something without a filesystem type}', not ext4; refusing to format or mount it"
        fi
        log "data disk $dev has an ext4 filesystem; not formatting"
        ;;
      2)
        if disk_is_blank "$dev"; then
          log "data disk $dev is blank (no signature, reads as zeros): formatting it as ext4 (first boot only)"
          # Options from Google's "Format and mount a non-boot disk": no reserved blocks,
          # initialise inode tables now rather than in the background, and discard unused blocks.
          mkfs.ext4 -q -m 0 -E lazy_itable_init=0,lazy_journal_init=0,discard -L edge-data "$dev"
        else
          die "data disk $dev: blkid recognises no filesystem, but the disk is not blank or could not be read. NOT formatting it. Investigate before anything else: 'sudo blkid -p $dev', 'sudo dumpe2fs -h $dev' and, for a damaged primary superblock, 'sudo e2fsck -n -b 32768 $dev' (read-only check against a backup superblock). The data may be recoverable."
        fi
        ;;
      *)
        die "blkid could not read $dev (exit $rc); not formatting and not mounting it"
        ;;
    esac

    # /etc/fstab does not survive a reboot on COS, so there is no boot-time fsck; check here.
    # fsck exit 0 = clean, 1 = errors fixed; anything higher needs a person.
    rc=0
    fsck.ext4 -p "$dev" || rc=$?
    if [ "$rc" -gt 1 ]; then
      die "fsck.ext4 found problems on $dev it could not fix (exit $rc); not mounting it. Run 'sudo fsck.ext4 $dev' by hand."
    fi

    mkdir -p "$EDGE_DATA_DIR"
    mount -t ext4 -o discard,defaults "$dev" "$EDGE_DATA_DIR"
    is_mounted "$EDGE_DATA_DIR" || die "mount returned success but $EDGE_DATA_DIR is not a mount point"
    log "data disk $dev mounted at $EDGE_DATA_DIR"
  fi

  # If the disk was made bigger in Terraform, grow the filesystem to match (online; a no-op
  # otherwise).
  if command -v resize2fs > /dev/null 2>&1; then
    resize2fs "$dev" > /dev/null 2>&1 || warn "resize2fs on $dev failed; the filesystem keeps its size"
  fi

  mkdir -p "$EDGE_MARIADB_DIR" "$EDGE_OPENEMR_DIR" "$EDGE_SECRETS_DIR"
  chmod 700 "$EDGE_SECRETS_DIR"
}

# ---- Step: swap -------------------------------------------------------------------------------------

enable_swap() {
  local want have=0 tmp="$EDGE_SWAP_FILE.tmp"

  if [ "$EDGE_SWAP_SIZE_MB" -eq 0 ]; then
    log "swap is switched off (swap_size_mb = 0)"
    return 0
  fi
  if awk -v f="$EDGE_SWAP_FILE" '$1 == f { found = 1 } END { exit !found }' "$EDGE_PROC_SWAPS"; then
    log "swap file $EDGE_SWAP_FILE already in use"
    return 0
  fi

  want=$((EDGE_SWAP_SIZE_MB * 1024 * 1024))
  if [ -f "$EDGE_SWAP_FILE" ]; then
    have=$(stat -c %s "$EDGE_SWAP_FILE")
  fi
  if [ "$have" -ne "$want" ]; then
    log "creating a ${EDGE_SWAP_SIZE_MB} MiB swap file at $EDGE_SWAP_FILE"
    rm -f "$tmp"
    if command -v fallocate > /dev/null 2>&1; then
      fallocate -l "${EDGE_SWAP_SIZE_MB}M" "$tmp"
    else
      dd if=/dev/zero of="$tmp" bs=1M count="$EDGE_SWAP_SIZE_MB" status=none
    fi
    chmod 600 "$tmp"
    mkswap "$tmp" > /dev/null
    mv "$tmp" "$EDGE_SWAP_FILE"
  fi
  chmod 600 "$EDGE_SWAP_FILE"
  swapon "$EDGE_SWAP_FILE"
  log "swap file $EDGE_SWAP_FILE enabled"
}

# ---- Step: docker -----------------------------------------------------------------------------------

prepare_docker() {
  local deadline

  mkdir -p "$DOCKER_CONFIG"
  chmod 700 "$DOCKER_CONFIG"

  deadline=$(($(now) + EDGE_DOCKER_WAIT_SECONDS))
  until timeout 10 docker info > /dev/null 2>&1; do
    if [ "$(now)" -ge "$deadline" ]; then
      die "the Docker daemon did not answer within ${EDGE_DOCKER_WAIT_SECONDS}s"
    fi
    sleep 2
  done

  # docker-credential-gcr is preinstalled on COS. Registering it for the mirror's host makes
  # `docker pull <location>-docker.pkg.dev/...` authenticate as the VM's service account through
  # the metadata server; no key file or password is involved. gcr.io (the MLLP adapter) is a
  # public image and needs no credentials.
  timeout 30 docker-credential-gcr configure-docker --registries="$EDGE_MIRROR_HOST" > /dev/null

  if ! timeout 30 docker network inspect "$EDGE_DOCKER_NETWORK" > /dev/null 2>&1; then
    timeout 30 docker network create --driver bridge "$EDGE_DOCKER_NETWORK" > /dev/null
    log "created docker network $EDGE_DOCKER_NETWORK"
  fi
}

# Pull an image unless it is present, retrying until the shared deadline. Each attempt is capped
# by `timeout`, so a stalled pull cannot hang the script.
pull_image() {
  local ref=$1 attempt=0 left
  if docker image inspect "$ref" > /dev/null 2>&1; then
    log "image already present: $ref"
    return 0
  fi
  while :; do
    left=$(remaining)
    if [ "$left" -le 0 ]; then
      error "could not pull $ref before the boot deadline (${EDGE_BOOT_WAIT_SECONDS}s, $attempt tries). Check Private Google Access on the edge subnet and, for the mirror, the VM account's roles/artifactregistry.reader (granted by project-base). Fallback: project-base enable_cloud_nat = true."
      return 1
    fi
    attempt=$((attempt + 1))
    if timeout "$(min "$EDGE_PULL_TIMEOUT_SECONDS" "$left")" docker pull --quiet "$ref" > /dev/null; then
      log "pulled $ref"
      return 0
    fi
    if [ $((attempt % 6)) -eq 1 ]; then
      warn "pulling $ref failed or timed out (try $attempt, $(remaining)s left); retrying"
    fi
    sleep "$(min "$EDGE_POLL_SECONDS" "$(remaining)")"
  done
}

# A short hash of a container's run arguments, stored as a label, so a later run can tell whether
# the running container still matches the configuration (same image digest, same flags).
config_hash() {
  printf '%s\0' "$@" | sha256sum | awk '{ print substr($1, 1, 16) }'
}

container_state() {
  docker container inspect --format '{{.State.Status}} {{.RestartCount}}' "$1"
}

# Create the container if it does not exist, replace it if its configuration changed, start it if
# it is stopped, and otherwise leave it alone. Arguments after the name are `docker run` options,
# the image and its command. A container that is replaced is first stopped gracefully (SIGTERM,
# then up to EDGE_STOP_TIMEOUT_SECONDS before SIGKILL), so MariaDB can flush and shut down cleanly;
# it is never removed with --force while running.
ensure_container() {
  local name=$1 hash current state
  shift
  hash=$(config_hash "$@")

  if docker container inspect "$name" > /dev/null 2>&1; then
    current=$(docker container inspect --format "{{index .Config.Labels \"$EDGE_CONFIG_LABEL\"}}" "$name")
    if [ "$current" = "$hash" ]; then
      state=$(container_state "$name")
      case "${state%% *}" in
        running | restarting)
          log "container $name is ${state%% *} with the current configuration; nothing to do"
          ;;
        *)
          log "container $name is ${state%% *}; starting it"
          docker start "$name" > /dev/null
          ;;
      esac
      return 0
    fi
    log "container $name has an older configuration (image or flags changed); stopping it gracefully and replacing it"
    docker stop --time "$EDGE_STOP_TIMEOUT_SECONDS" "$name" > /dev/null
    docker rm "$name" > /dev/null
  fi

  log "creating container $name"
  docker run --detach --name "$name" --label "$EDGE_CONFIG_LABEL=$hash" "$@" > /dev/null
}

# ---- Step: MLLP adapter -----------------------------------------------------------------------------

start_mllp_adapter() {
  pull_image "$EDGE_MLLP_ADAPTER_IMAGE"
  # --restart unless-stopped: Docker restarts it after a crash and after every reboot, unless
  # someone stopped it by hand. --publish puts the adapter's port 2575 on the VM's address; the
  # project-base firewall admits only the serverless subnet to it. Bridge networking (not host)
  # because Docker's published ports pass COS's locked-down host firewall, which drops other
  # inbound traffic.
  ensure_container "$EDGE_ADAPTER_CONTAINER" \
    --restart unless-stopped \
    --publish "$EDGE_MLLP_PORT:2575" \
    --log-opt max-size=10m --log-opt max-file=3 \
    "$EDGE_MLLP_ADAPTER_IMAGE" \
    "${EDGE_MLLP_ADAPTER_COMMAND[@]}"
}

# Spike S4: a wrong flag makes the adapter print its usage and exit with status 2, and Docker then
# restarts it in a loop without anyone noticing. Watch it for a few seconds and say so loudly.
check_mllp_adapter() {
  local before after state status
  state=$(container_state "$EDGE_ADAPTER_CONTAINER")
  before=${state##* }
  sleep "$EDGE_ADAPTER_CHECK_SECONDS"
  state=$(container_state "$EDGE_ADAPTER_CONTAINER")
  status=${state%% *}
  after=${state##* }

  if [ "$status" = "running" ] && [ "$after" -eq "$before" ]; then
    log "mllp-adapter is up and stayed up for ${EDGE_ADAPTER_CHECK_SECONDS}s (port $EDGE_MLLP_PORT)"
    return 0
  fi
  error "mllp-adapter is crash-looping or down: status '$status', restart count $before -> $after over ${EDGE_ADAPTER_CHECK_SECONDS}s. A wrong flag shows as a usage message below; an API error as 'failed to handle message'."
  docker logs --tail 20 "$EDGE_ADAPTER_CONTAINER" 2>&1 | sed 's/^/  mllp-adapter| /' >&2 || true
  return 1
}

# ---- Step: secrets ----------------------------------------------------------------------------------

# Print an access token for the VM's service account (from the metadata server) on stdout, for
# the caller to capture. Never logged.
access_token() {
  local json
  json=$("${EDGE_CURL[@]}" --fail --header 'Metadata-Flavor: Google' \
    "$EDGE_METADATA_URL/instance/service-accounts/default/token") || return 1
  printf '%s' "$json" | sed -n 's/.*"access_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
}

# One attempt to copy the latest version of a secret into "$EDGE_SECRETS_DIR/<name>" (mode 600).
# Prints the HTTP status on failure. Every command's result is checked explicitly, because the
# caller runs this inside a condition, where errexit does not apply.
fetch_secret_once() {
  local name=$1 resource=$2
  local dest="$EDGE_SECRETS_DIR/$name"
  local response="$dest.response.tmp" decoded="$dest.decoded.tmp"
  local token code data

  umask 077
  token=$(access_token) || token=""
  if [ -z "$token" ]; then
    echo "no-token"
    return 1
  fi
  code=$(printf 'Authorization: Bearer %s\n' "$token" |
    "${EDGE_CURL[@]}" --header @- --output "$response" --write-out '%{http_code}' \
      "$EDGE_SECRETMANAGER_URL/$resource/versions/latest:access") || code=000
  token=""
  if [ "$code" != "200" ]; then
    rm -f "$response"
    echo "$code"
    return 1
  fi

  # The response is {"name": "...", "payload": {"data": "<base64>", "dataCrc32c": "..."}}.
  data=$(sed -n 's/.*"data"[[:space:]]*:[[:space:]]*"\([A-Za-z0-9+/=]*\)".*/\1/p' "$response" | head -n 1)
  rm -f "$response"
  if [ -z "$data" ]; then
    echo "no-payload"
    return 1
  fi
  if ! printf '%s' "$data" | base64 -d > "$decoded" 2> /dev/null || [ ! -s "$decoded" ]; then
    data=""
    rm -f "$decoded"
    echo "undecodable-or-empty"
    return 1
  fi
  data=""
  if ! chmod 600 "$decoded"; then
    rm -f "$decoded"
    echo "chmod-failed"
    return 1
  fi
  if ! mv "$decoded" "$dest"; then
    rm -f "$decoded"
    echo "write-failed"
    return 1
  fi
  return 0
}

# Fill missing secret files, all of them sharing the boot deadline: each round tries every secret
# still missing once, so one missing secret never makes the others wait in turn. A file that
# exists is kept: MariaDB took its root password when it initialised its data directory, so a
# newer secret version would not match the database (rotating it is a manual step).
# Usage: fetch_secrets only <name>   or   fetch_secrets except <name>
fetch_secrets() {
  local mode=$1 selected=$2 entry name resource result round=0 pending=() still=()
  for entry in "${EDGE_SECRET_FILES[@]}"; do
    name=${entry%%=*}
    if { [ "$mode" = only ] && [ "$name" != "$selected" ]; } ||
      { [ "$mode" = except ] && [ "$name" = "$selected" ]; }; then
      continue
    fi
    if [ -s "$EDGE_SECRETS_DIR/$name" ]; then
      log "secret file $name already present; keeping it"
    else
      pending+=("$entry")
    fi
  done

  while [ "${#pending[@]}" -gt 0 ]; do
    round=$((round + 1))
    still=()
    for entry in "${pending[@]}"; do
      name=${entry%%=*}
      resource=${entry#*=}
      if result=$(fetch_secret_once "$name" "$resource"); then
        log "secret $name written to $EDGE_SECRETS_DIR/$name (mode 600)"
      else
        still+=("$entry")
        if [ "$(remaining)" -le 0 ]; then
          error "secret $name ($resource) could not be copied before the boot deadline (last result: $result; 404 = no version added yet, 403 = no access). Add a version (scripts/secrets, T27), then run: sudo google_metadata_script_runner startup"
        elif [ $((round % 6)) -eq 1 ]; then
          warn "secret $name not readable yet (result $result: 404 = no version added yet, 403 = no access); retrying until the boot deadline"
        fi
      fi
    done
    pending=("${still[@]}")
    if [ "${#pending[@]}" -eq 0 ]; then
      return 0
    fi
    if [ "$(remaining)" -le 0 ]; then
      return 1
    fi
    sleep "$(min "$EDGE_POLL_SECONDS" "$(remaining)")"
  done
}

# ---- Step: MariaDB ----------------------------------------------------------------------------------

# What is in a MariaDB data directory, as one word on stdout (T42):
#   empty       nothing there (or no directory): MariaDB will initialise a new database;
#   complete    the init marker is there: a first initialisation got as far as creating its users;
#   user-data   no marker, but a database directory other than MariaDB's own system schemas
#               (an OpenEMR database, say): real data, whatever the marker says;
#   incomplete  none of the above: files of a first initialisation that stopped part-way.
# The user-data rule is the second, positive signal that keeps a real database safe even if it
# lacks the marker (a data directory made by an image older than the health-check users, or a
# marker deleted by hand). System schemas: mysql, sys, performance_schema, test; names starting
# with '#' or '.' are server or entrypoint internals. Runs in a subshell so shopt stays local.
mariadb_datadir_state() (
  local dir=$1 entry name entries=() user_dbs=()
  shopt -s nullglob dotglob
  [ -d "$dir" ] && entries=("$dir"/*)
  if [ "${#entries[@]}" -eq 0 ]; then
    echo empty
    return 0
  fi
  if [ -e "$dir/$EDGE_MARIADB_INIT_MARKER" ]; then
    echo complete
    return 0
  fi
  for entry in "${entries[@]}"; do
    [ -d "$entry" ] || continue
    name=$(basename "$entry")
    case "$name" in
      mysql | sys | performance_schema | test | '#'* | .*) ;;
      *) user_dbs+=("$name") ;;
    esac
  done
  if [ "${#user_dbs[@]}" -gt 0 ]; then
    echo "user-data ${user_dbs[*]}"
  else
    echo incomplete
  fi
)

# Before MariaDB starts: decide from the data directory whether it holds a database, will get a
# new one, or holds the remains of a failed first initialisation (T42). Only the last is changed:
# the directory is moved aside (never deleted) to mariadb.incomplete-<UTC time> and an empty one
# is made, so the entrypoint initialises again on this run. Older mariadb.incomplete-* copies are
# removed (each was judged incomplete in the same way). A container left from the failed boot is
# stopped and removed first, so the new container starts on the new, empty directory (a running
# container keeps the directory it was started with). That container cannot be serving: without
# the marker file its health check cannot log in.
prepare_mariadb_datadir() {
  local state aside old
  state=$(mariadb_datadir_state "$EDGE_MARIADB_DIR")
  case "$state" in
    empty)
      EDGE_MARIADB_FRESH=1
      log "MariaDB data directory $EDGE_MARIADB_DIR is empty: MariaDB will initialise a new database (first boot)"
      ;;
    complete)
      log "MariaDB data directory $EDGE_MARIADB_DIR holds an initialised database ($EDGE_MARIADB_INIT_MARKER present); using it as it is"
      ;;
    user-data*)
      warn "MariaDB data directory $EDGE_MARIADB_DIR has no $EDGE_MARIADB_INIT_MARKER (the file a completed first initialisation leaves) but holds the database(s) ${state#user-data }: treating it as a real database and NOT moving it. If MariaDB does not become healthy, a person must look (README, 'First initialisation of MariaDB')."
      ;;
    incomplete)
      aside="$EDGE_DATA_DIR/mariadb.incomplete-$(date -u +%Y%m%dT%H%M%SZ)"
      [ ! -e "$aside" ] || die "cannot move the half-initialised MariaDB data directory aside: $aside already exists"
      log "MariaDB data directory $EDGE_MARIADB_DIR is HALF-INITIALISED (no $EDGE_MARIADB_INIT_MARKER and no user database: an earlier first initialisation stopped part-way): moving it aside to $aside and initialising a new database"
      if docker container inspect "$EDGE_MARIADB_CONTAINER" > /dev/null 2>&1; then
        log "removing container $EDGE_MARIADB_CONTAINER left from the failed initialisation, so the new one starts on the empty directory"
        docker stop --time "$EDGE_STOP_TIMEOUT_SECONDS" "$EDGE_MARIADB_CONTAINER" > /dev/null
        docker rm "$EDGE_MARIADB_CONTAINER" > /dev/null
      fi
      mv "$EDGE_MARIADB_DIR" "$aside"
      mkdir -p "$EDGE_MARIADB_DIR"
      for old in "$EDGE_DATA_DIR"/mariadb.incomplete-*; do
        if [ "$old" != "$aside" ] && [ -d "$old" ]; then
          rm -rf -- "$old"
          log "removed an older half-initialised copy, $old"
        fi
      done
      EDGE_MARIADB_FRESH=1
      ;;
    *)
      die "could not classify the MariaDB data directory $EDGE_MARIADB_DIR ('$state'); not starting MariaDB"
      ;;
  esac
}

start_mariadb() {
  local password_file="$EDGE_SECRETS_DIR/$EDGE_MARIADB_SECRET"
  is_mounted "$EDGE_DATA_DIR" || die "the data disk is not mounted; not starting MariaDB"
  [ -s "$password_file" ] || die "$password_file is missing; not starting MariaDB"

  pull_image "$EDGE_MARIADB_IMAGE"
  prepare_mariadb_datadir
  # The data directory and the password are bind mounts from the data disk. A bind mount whose
  # source is missing makes `docker start` fail instead of creating an empty directory, so after
  # a reboot Docker cannot start MariaDB on an empty boot-disk directory before the disk is
  # mounted; this script starts it once the mount is in place.
  # No --publish: MariaDB is reachable only on the pulsebridge-edge network, never on the VM's
  # address. --stop-timeout gives it time to shut down cleanly on `docker stop` and when the
  # Docker daemon stops at VM shutdown (the daemon waits up to the largest container stop timeout
  # plus a few seconds). The health check is the one from spike S2's compose file.
  # The entrypoint (T42): `--entrypoint bash` with `-c <wrapper>` runs scripts/mariadb-entrypoint-
  # wrapper.sh (delivered in the configuration block) in front of the image's own entrypoint. The
  # wrapper gives a first initialisation's temporary server PULSEBRIDGE_INIT_WAIT_SECONDS to start
  # instead of 30 s, and runs the official entrypoint unchanged on an initialised data directory.
  # The words after the wrapper are $0 (a name for error messages) and the command, as before.
  # Docker runs this same entrypoint again on every restart (--restart unless-stopped).
  ensure_container "$EDGE_MARIADB_CONTAINER" \
    --restart unless-stopped \
    --stop-timeout "$EDGE_STOP_TIMEOUT_SECONDS" \
    --network "$EDGE_DOCKER_NETWORK" \
    --mount "type=bind,source=$EDGE_MARIADB_DIR,target=/var/lib/mysql" \
    --mount "type=bind,source=$password_file,target=/run/secrets/mariadb-root-password,readonly" \
    --env MARIADB_ROOT_PASSWORD_FILE=/run/secrets/mariadb-root-password \
    --health-cmd "/usr/local/bin/healthcheck.sh --su-mysql --connect --innodb_initialized" \
    --health-start-period 2m --health-start-interval 10s \
    --health-interval 1m --health-timeout 10s --health-retries 3 \
    --log-opt max-size=10m --log-opt max-file=3 \
    --env "PULSEBRIDGE_INIT_WAIT_SECONDS=$EDGE_MARIADB_INIT_WAIT_SECONDS" \
    --entrypoint bash \
    "$EDGE_MARIADB_IMAGE" \
    -c "$EDGE_MARIADB_ENTRYPOINT_WRAPPER" pulsebridge-mariadb-entrypoint \
    "${EDGE_MARIADB_COMMAND[@]}"

  wait_for_mariadb
}

wait_for_mariadb() {
  local health
  while :; do
    health=$(docker container inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$EDGE_MARIADB_CONTAINER")
    if [ "$health" = "healthy" ]; then
      if [ "$EDGE_MARIADB_FRESH" = 1 ]; then
        log "MariaDB is healthy: first initialisation of a new database completed ($(elapsed)s into this run)"
      else
        log "MariaDB is healthy (existing database)"
      fi
      return 0
    fi
    if [ "$(remaining)" -le 0 ]; then
      if [ "$EDGE_MARIADB_FRESH" = 1 ]; then
        error "MariaDB is not healthy by the boot deadline (health: $health): its FIRST INITIALISATION did not complete. If it stopped before creating its users (no .my-healthcheck.cnf), the next run of this script moves the data directory aside and initialises again (sudo google_metadata_script_runner startup, or a reboot). Last lines of its log:"
      else
        error "MariaDB is not healthy by the boot deadline (health: $health). Last lines of its log:"
      fi
      # MariaDB's log holds no secret: the password reaches it as a file, and the entrypoint
      # prints a password only for MARIADB_RANDOM_ROOT_PASSWORD, which is not used.
      docker logs --tail 20 "$EDGE_MARIADB_CONTAINER" 2>&1 | sed 's/^/  mariadb| /' >&2 || true
      return 1
    fi
    sleep "$(min "$EDGE_POLL_SECONDS" "$(remaining)")"
  done
}

# ---- Step: hooks (the seam for T23) ------------------------------------------------------------------

# Each hook is a bash script stored in instance metadata as pulsebridge-hook-<name>. It runs as
# root after MariaDB is healthy, on every boot, so it must be idempotent. It gets the layout below
# in its environment and reads any secret from a file in EDGE_SECRETS_DIR. Each hook runs under
# `timeout` (EDGE_HOOK_TIMEOUT_SECONDS) with the run lock's file descriptor closed, so a stray
# background process cannot keep the lock. A failing hook does not stop the next one.
run_hooks() {
  local name file rc failed=0

  if [ "${#EDGE_HOOK_NAMES[@]}" -eq 0 ]; then
    log "no hooks configured"
    return 0
  fi
  mkdir -p "$EDGE_HOOKS_DIR"
  chmod 700 "$EDGE_HOOKS_DIR"
  export EDGE_DATA_DIR EDGE_SECRETS_DIR EDGE_MARIADB_DIR EDGE_OPENEMR_DIR EDGE_STATE_DIR \
    EDGE_DOCKER_NETWORK EDGE_MARIADB_CONTAINER EDGE_MIRROR_URL EDGE_BOOT_WAIT_SECONDS \
    EDGE_HOOK_TIMEOUT_SECONDS

  for name in "${EDGE_HOOK_NAMES[@]}"; do
    file="$EDGE_HOOKS_DIR/$name.sh"
    if ! "${EDGE_CURL[@]}" --fail --header 'Metadata-Flavor: Google' --output "$file.tmp" \
      "$EDGE_METADATA_URL/instance/attributes/pulsebridge-hook-$name"; then
      rm -f "$file.tmp"
      error "hook $name: could not read metadata key pulsebridge-hook-$name"
      failed=1
      continue
    fi
    mv "$file.tmp" "$file"
    log "hook $name: running (limit ${EDGE_HOOK_TIMEOUT_SECONDS}s)"
    rc=0
    timeout "$EDGE_HOOK_TIMEOUT_SECONDS" bash "$file" 9>&- || rc=$?
    if [ "$rc" -eq 0 ]; then
      log "hook $name: ok"
    elif [ "$rc" -eq 124 ]; then
      error "hook $name: FAILED (stopped after ${EDGE_HOOK_TIMEOUT_SECONDS}s)"
      failed=1
    else
      error "hook $name: FAILED (exit $rc)"
      failed=1
    fi
  done
  return "$failed"
}

# ---- Main -------------------------------------------------------------------------------------------

main() {
  local disk_rc docker_rc mariadb_rc

  log "edge VM startup: begin"
  EDGE_DEADLINE=$(($(now) + EDGE_BOOT_WAIT_SECONDS))
  mkdir -p "$EDGE_STATE_DIR"
  chmod 700 "$EDGE_STATE_DIR"

  # One run at a time: someone may re-run the script by hand while the boot run is still waiting.
  exec 9> "$EDGE_STATE_DIR/startup.lock"
  if command -v flock > /dev/null 2>&1 && ! flock --nonblock 9; then
    die "another run of the startup script holds $EDGE_STATE_DIR/startup.lock; exiting"
  fi

  run_step preflight require_commands \
    awk base64 basename blkid cat curl dd docker docker-credential-gcr flock fsck.ext4 mkfs.ext4 \
    mkswap mount readlink sed sha256sum stat swapon timeout tr wc
  if [ "${#FAILED_STEPS[@]}" -gt 0 ]; then
    die "edge VM startup: cannot continue without the commands above"
  fi

  run_step data-disk mount_data_disk
  disk_rc=$?
  run_step swap enable_swap
  run_step docker prepare_docker
  docker_rc=$?

  if [ "$docker_rc" -eq 0 ]; then
    # The HL7v2 path first: it needs neither the data disk nor a secret.
    run_step mllp-adapter start_mllp_adapter
    run_step mllp-adapter-check check_mllp_adapter
  else
    error "Docker is not ready: the MLLP adapter and MariaDB were not started"
  fi

  if [ "$disk_rc" -eq 0 ] && [ "$docker_rc" -eq 0 ]; then
    # MariaDB needs only its own secret, fetched first: a missing extra secret (for a hook, T23)
    # never keeps MariaDB down or eats into its wait.
    run_step mariadb-secret fetch_secrets only "$EDGE_MARIADB_SECRET"
    if [ -s "$EDGE_SECRETS_DIR/$EDGE_MARIADB_SECRET" ]; then
      run_step mariadb start_mariadb
      mariadb_rc=$?
    else
      mariadb_rc=1
      error "the MariaDB root password is missing: MariaDB and hooks were not started"
    fi
    # The other secrets share what is left of the deadline.
    run_step other-secrets fetch_secrets except "$EDGE_MARIADB_SECRET"
    if [ "$mariadb_rc" -eq 0 ]; then
      run_step hooks run_hooks
    elif [ -s "$EDGE_SECRETS_DIR/$EDGE_MARIADB_SECRET" ]; then
      error "MariaDB is not healthy: hooks were not run"
    fi
  elif [ "$disk_rc" -ne 0 ]; then
    error "the data disk is not available: MariaDB and hooks were not started"
  fi

  if [ "${#FAILED_STEPS[@]}" -gt 0 ]; then
    error "edge VM startup: finished with failed steps: ${FAILED_STEPS[*]}"
    exit 1
  fi
  log "edge VM startup: done, all steps ok"
}

main "$@"
