# shellcheck shell=bash
# shellcheck disable=SC2154 # the EDGE_* configuration is assigned in part 1 (the rendered template)
#
# Part 2 of 2 of the edge VM startup script: the logic. Terraform appends this file unchanged to
# the configuration block rendered from templates/startup-config.sh.tftpl, so this file contains no
# Terraform interpolation and can be linted and tested on its own (tests/startup_script.sh).
#
# What it does, on every boot, in this order:
#   1. mounts the data disk at EDGE_DATA_DIR, formatting it ONLY if it has no filesystem;
#   2. turns on the swap file (COS keeps no /etc/fstab across boots, so this runs every boot);
#   3. waits for Docker and configures docker-credential-gcr for the Docker Hub mirror;
#   4. starts Google's MLLP adapter and checks a few seconds later that it stays up;
#   5. copies the secrets it needs from Secret Manager to files on the data disk (once);
#   6. starts MariaDB with spike S2's low-memory settings and waits until it is healthy;
#   7. runs the hook scripts (T23: OpenEMR) from instance metadata.
#
# Safe to run again: every step checks what is already there first. Nothing is formatted twice,
# a container that is already running with the same configuration is left alone, and a secret file
# that exists is kept. The adapter does not depend on the data disk or on Secret Manager, so a
# problem there still leaves the HL7v2 path up. Any failed step makes the script exit non-zero
# after the others have run, with an ERROR line naming the step.
#
# Secrets: the only secret this script handles is read from Secret Manager into a mode-600 file
# and passed to MariaDB as a file (MARIADB_ROOT_PASSWORD_FILE). It never appears in a log line,
# in a command line or in a container's environment. The access token is passed to curl on
# standard input, not as an argument, so it does not show in the process list either.
# There is deliberately no `set -x` anywhere in this file.

set -uo pipefail

# ---- Test seams. The defaults are the real VM; tests/startup_script.sh points them at a sandbox.
EDGE_PROC_MOUNTS="${EDGE_PROC_MOUNTS:-/proc/mounts}"
EDGE_PROC_SWAPS="${EDGE_PROC_SWAPS:-/proc/swaps}"
EDGE_METADATA_URL="${EDGE_METADATA_URL:-http://metadata.google.internal/computeMetadata/v1}"
EDGE_SECRETMANAGER_URL="${EDGE_SECRETMANAGER_URL:-https://secretmanager.googleapis.com/v1}"
EDGE_POLL_SECONDS="${EDGE_POLL_SECONDS:-10}"         # pause between retries
EDGE_ADAPTER_CHECK_SECONDS="${EDGE_ADAPTER_CHECK_SECONDS:-15}" # how long the adapter must stay up

# ---- Layout. Data on the data disk survives VM replacement; state on the boot disk does not need to.
EDGE_MARIADB_DIR="$EDGE_DATA_DIR/mariadb"   # MariaDB data directory (/var/lib/mysql)
EDGE_SECRETS_DIR="$EDGE_DATA_DIR/secrets"   # mode 700; one mode-600 file per secret
EDGE_OPENEMR_DIR="$EDGE_DATA_DIR/openemr"   # for T23: OpenEMR's sites directory
EDGE_HOOKS_DIR="$EDGE_STATE_DIR/hooks"      # hook scripts fetched from metadata on each boot
EDGE_DOCKER_NETWORK="pulsebridge-edge"      # user-defined bridge: containers reach "mariadb" by name
EDGE_MARIADB_CONTAINER="mariadb"
EDGE_ADAPTER_CONTAINER="mllp-adapter"
EDGE_CONFIG_LABEL="pulsebridge.config"      # container label holding a hash of its run arguments

# COS mounts / read-only and does not let anything write /root, so docker-credential-gcr and the
# docker CLI keep their configuration under the (stateful) state directory instead.
export HOME="$EDGE_STATE_DIR"
export DOCKER_CONFIG="$EDGE_STATE_DIR/.docker"

FAILED_STEPS=()

# ---- Logging -------------------------------------------------------------------------------------

log() { printf '%s pulsebridge-edge: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
warn() { log "WARNING: $*"; }
error() { log "ERROR: $*" >&2; }
die() {
  error "$*"
  exit 1
}

# Number of retries that fit in a number of seconds at EDGE_POLL_SECONDS each (at least 1).
attempts_for() {
  local n=$((($1 + EDGE_POLL_SECONDS - 1) / EDGE_POLL_SECONDS))
  if [ "$n" -lt 1 ]; then n=1; fi
  echo "$n"
}

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

mount_data_disk() {
  local dev fstype rc attempt=0

  # Compute Engine attaches the disk before boot; udev creates the /dev/disk/by-id link shortly
  # after. Wait briefly, then refuse to go on: MariaDB's data must never land on the boot disk.
  while [ ! -e "$EDGE_DATA_DEVICE" ]; do
    attempt=$((attempt + 1))
    if [ "$attempt" -gt 6 ]; then
      die "data disk $EDGE_DATA_DEVICE not found. Is the persistent disk attached with that device name? MariaDB and hooks will not start without it."
    fi
    sleep 5
  done
  dev=$(readlink -f "$EDGE_DATA_DEVICE")

  if is_mounted "$EDGE_DATA_DIR"; then
    log "data disk already mounted at $EDGE_DATA_DIR"
  else
    # blkid -p probes the device itself. Exit 0: a filesystem was found. Exit 2: nothing was found,
    # which on this disk means it is new and empty. Anything else is a read error, and a read
    # error must never lead to mkfs: that would wipe the clinic database.
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
        log "data disk $dev has no filesystem: formatting it as ext4 (first boot only)"
        # Options from Google's "Format and mount a non-boot disk": no reserved blocks, initialise
        # inode tables now rather than in the background, and discard unused blocks.
        mkfs.ext4 -q -m 0 -E lazy_itable_init=0,lazy_journal_init=0,discard -L edge-data "$dev"
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
      die "fsck.ext4 found problems on $dev it could not fix (exit $rc); not mounting it"
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
  local attempt=0

  mkdir -p "$DOCKER_CONFIG"
  chmod 700 "$DOCKER_CONFIG"

  until docker info > /dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -gt 30 ]; then
      die "the Docker daemon is not answering"
    fi
    sleep 2
  done

  # docker-credential-gcr is preinstalled on COS. Registering it for the mirror's host makes
  # `docker pull <location>-docker.pkg.dev/...` authenticate as the VM's service account through
  # the metadata server; no key file or password is involved. gcr.io (the MLLP adapter) is a
  # public image and needs no credentials.
  docker-credential-gcr configure-docker --registries="$EDGE_MIRROR_HOST" > /dev/null

  if ! docker network inspect "$EDGE_DOCKER_NETWORK" > /dev/null 2>&1; then
    docker network create --driver bridge "$EDGE_DOCKER_NETWORK" > /dev/null
    log "created docker network $EDGE_DOCKER_NETWORK"
  fi
}

pull_image() {
  local ref=$1 attempt=0 max
  if docker image inspect "$ref" > /dev/null 2>&1; then
    log "image already present: $ref"
    return 0
  fi
  max=$(attempts_for "$EDGE_BOOT_WAIT_SECONDS")
  until docker pull --quiet "$ref" > /dev/null; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge "$max" ]; then
      error "could not pull $ref after $attempt tries. Check Private Google Access on the edge subnet and, for the mirror, the VM account's roles/artifactregistry.reader. Fallback: project-base enable_cloud_nat = true."
      return 1
    fi
    if [ $((attempt % 6)) -eq 1 ]; then
      warn "pulling $ref failed (try $attempt of $max); retrying"
    fi
    sleep "$EDGE_POLL_SECONDS"
  done
  log "pulled $ref"
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
# the image and its command.
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
    log "container $name has an older configuration (image or flags changed); replacing it"
    docker rm --force "$name" > /dev/null
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
  json=$(curl --silent --fail --header 'Metadata-Flavor: Google' \
    "$EDGE_METADATA_URL/instance/service-accounts/default/token") || return 1
  printf '%s' "$json" | sed -n 's/.*"access_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
}

# Copy the latest version of one secret into "$EDGE_SECRETS_DIR/<name>" (mode 600). Retries for
# EDGE_BOOT_WAIT_SECONDS: on a first apply the secret exists but its version is added afterwards
# by scripts/secrets (T27). Errors are handled explicitly here, because the caller runs it inside
# `||`, where errexit does not apply.
fetch_secret() {
  local name=$1 resource=$2
  local dest="$EDGE_SECRETS_DIR/$name"
  local response="$dest.response.tmp" decoded="$dest.decoded.tmp"
  local token code data attempt=0 max

  max=$(attempts_for "$EDGE_BOOT_WAIT_SECONDS")
  umask 077
  while :; do
    attempt=$((attempt + 1))
    code=000
    token=$(access_token) || token=""
    if [ -n "$token" ]; then
      code=$(printf 'Authorization: Bearer %s\n' "$token" |
        curl --silent --header @- --output "$response" --write-out '%{http_code}' \
          "$EDGE_SECRETMANAGER_URL/$resource/versions/latest:access") || code=000
    fi
    token=""
    if [ "$code" = "200" ]; then
      break
    fi
    rm -f "$response"
    if [ "$attempt" -ge "$max" ]; then
      error "secret $name ($resource) has no readable version after $attempt tries (last HTTP status $code). Add a version (scripts/secrets, T27), then run: sudo google_metadata_script_runner startup"
      return 1
    fi
    if [ $((attempt % 6)) -eq 1 ]; then
      warn "secret $name not readable yet (HTTP $code: 404 = no version added yet, 403 = no access); retrying for up to ${EDGE_BOOT_WAIT_SECONDS}s"
    fi
    sleep "$EDGE_POLL_SECONDS"
  done

  # The response is {"name": "...", "payload": {"data": "<base64>", "dataCrc32c": "..."}}.
  data=$(sed -n 's/.*"data"[[:space:]]*:[[:space:]]*"\([A-Za-z0-9+/=]*\)".*/\1/p' "$response" | head -n 1)
  rm -f "$response"
  if [ -z "$data" ]; then
    error "secret $name: the Secret Manager response had no payload"
    return 1
  fi
  if ! printf '%s' "$data" | base64 -d > "$decoded" 2> /dev/null || [ ! -s "$decoded" ]; then
    data=""
    rm -f "$decoded"
    error "secret $name: the payload could not be decoded or is empty"
    return 1
  fi
  data=""
  chmod 600 "$decoded"
  mv "$decoded" "$dest"
  log "secret $name written to $dest (mode 600)"
}

fetch_secrets() {
  local entry name resource failed=0
  for entry in "${EDGE_SECRET_FILES[@]}"; do
    name=${entry%%=*}
    resource=${entry#*=}
    if [ -s "$EDGE_SECRETS_DIR/$name" ]; then
      # Kept as is: MariaDB took its root password when it initialised its data directory, so a
      # newer secret version would not match the database. Rotating it is a manual step.
      log "secret file $name already present; keeping it"
      continue
    fi
    fetch_secret "$name" "$resource" || failed=1
  done
  return "$failed"
}

# ---- Step: MariaDB ----------------------------------------------------------------------------------

start_mariadb() {
  local password_file="$EDGE_SECRETS_DIR/mariadb-root-password"
  is_mounted "$EDGE_DATA_DIR" || die "the data disk is not mounted; not starting MariaDB"
  [ -s "$password_file" ] || die "$password_file is missing; not starting MariaDB"

  pull_image "$EDGE_MARIADB_IMAGE"
  # The data directory and the password are bind mounts from the data disk. A bind mount whose
  # source is missing makes `docker start` fail instead of creating an empty directory, so after
  # a reboot Docker cannot start MariaDB on an empty boot-disk directory before the disk is
  # mounted; this script starts it once the mount is in place.
  # The health check is the one from spike S2's compose file; T23 waits for "healthy".
  ensure_container "$EDGE_MARIADB_CONTAINER" \
    --restart unless-stopped \
    --network "$EDGE_DOCKER_NETWORK" \
    --mount "type=bind,source=$EDGE_MARIADB_DIR,target=/var/lib/mysql" \
    --mount "type=bind,source=$password_file,target=/run/secrets/mariadb-root-password,readonly" \
    --env MARIADB_ROOT_PASSWORD_FILE=/run/secrets/mariadb-root-password \
    --health-cmd "/usr/local/bin/healthcheck.sh --su-mysql --connect --innodb_initialized" \
    --health-start-period 2m --health-start-interval 10s \
    --health-interval 1m --health-timeout 10s --health-retries 3 \
    --log-opt max-size=10m --log-opt max-file=3 \
    "$EDGE_MARIADB_IMAGE" \
    "${EDGE_MARIADB_COMMAND[@]}"

  wait_for_mariadb
}

wait_for_mariadb() {
  local attempt=0 max health
  max=$(attempts_for "$EDGE_BOOT_WAIT_SECONDS")
  while :; do
    health=$(docker container inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$EDGE_MARIADB_CONTAINER")
    if [ "$health" = "healthy" ]; then
      log "MariaDB is healthy"
      return 0
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge "$max" ]; then
      error "MariaDB is not healthy after ${EDGE_BOOT_WAIT_SECONDS}s (health: $health); see: docker logs $EDGE_MARIADB_CONTAINER"
      return 1
    fi
    sleep "$EDGE_POLL_SECONDS"
  done
}

# ---- Step: hooks (the seam for T23) ------------------------------------------------------------------

# Each hook is a bash script stored in instance metadata as pulsebridge-hook-<name>. It runs as
# root after MariaDB is healthy, on every boot, so it must be idempotent. It gets the layout below
# in its environment and reads any secret from a file in EDGE_SECRETS_DIR.
run_hooks() {
  local name file rc failed=0

  if [ "${#EDGE_HOOK_NAMES[@]}" -eq 0 ]; then
    log "no hooks configured"
    return 0
  fi
  mkdir -p "$EDGE_HOOKS_DIR"
  chmod 700 "$EDGE_HOOKS_DIR"
  export EDGE_DATA_DIR EDGE_SECRETS_DIR EDGE_MARIADB_DIR EDGE_OPENEMR_DIR EDGE_STATE_DIR \
    EDGE_DOCKER_NETWORK EDGE_MARIADB_CONTAINER EDGE_MIRROR_URL EDGE_BOOT_WAIT_SECONDS

  for name in "${EDGE_HOOK_NAMES[@]}"; do
    file="$EDGE_HOOKS_DIR/$name.sh"
    if ! curl --silent --fail --header 'Metadata-Flavor: Google' --output "$file.tmp" \
      "$EDGE_METADATA_URL/instance/attributes/pulsebridge-hook-$name"; then
      rm -f "$file.tmp"
      error "hook $name: could not read metadata key pulsebridge-hook-$name"
      failed=1
      continue
    fi
    mv "$file.tmp" "$file"
    log "hook $name: running"
    rc=0
    bash "$file" || rc=$?
    if [ "$rc" -eq 0 ]; then
      log "hook $name: ok"
    else
      error "hook $name: FAILED (exit $rc)"
      failed=1
    fi
  done
  return "$failed"
}

# ---- Main -------------------------------------------------------------------------------------------

main() {
  local disk_rc docker_rc secrets_rc mariadb_rc

  log "edge VM startup: begin"
  mkdir -p "$EDGE_STATE_DIR"
  chmod 700 "$EDGE_STATE_DIR"

  # One run at a time: someone may re-run the script by hand while the boot run is still waiting.
  exec 9> "$EDGE_STATE_DIR/startup.lock"
  if command -v flock > /dev/null 2>&1 && ! flock --nonblock 9; then
    die "another run of the startup script holds $EDGE_STATE_DIR/startup.lock; exiting"
  fi

  run_step preflight require_commands \
    awk base64 blkid curl docker docker-credential-gcr fsck.ext4 mkfs.ext4 mkswap mount \
    readlink sed sha256sum stat swapon
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
    run_step secrets fetch_secrets
    secrets_rc=$?
    if [ "$secrets_rc" -eq 0 ]; then
      run_step mariadb start_mariadb
      mariadb_rc=$?
      if [ "$mariadb_rc" -eq 0 ]; then
        run_step hooks run_hooks
      else
        error "MariaDB is not healthy: hooks were not run"
      fi
    else
      error "secrets are missing: MariaDB and hooks were not started"
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
