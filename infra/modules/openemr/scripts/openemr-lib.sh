# shellcheck shell=bash
# shellcheck disable=SC2154 # the OPENEMR_* configuration is assigned in part 1 (the rendered template)
#
# Part 3 of each OpenEMR hook: functions shared by both hooks. Terraform appends this file unchanged
# after the configuration block, so it contains no Terraform interpolation.
#
# Rules every function here follows (edge-vm README, "The seam with T23"):
#   - no secret in a log line, in a command line or in this script: passwords are read from the
#     files edge-vm copies from Secret Manager, and reach a program through its environment or a
#     mode-600 file, never as an argument (arguments are visible to every user in `ps`);
#   - every network call has its own time limit, and everything that waits shares one deadline,
#     a minute before edge-vm's `timeout` would stop the hook, so the hook can say why it gave up;
#   - nothing is left running in the background when the hook ends.
# There is deliberately no `set -x` anywhere in these files.

set -euo pipefail

# ---- Test seams. The defaults are the real VM; tests/hooks.sh points them at a sandbox.
OPENEMR_PROC_MOUNTS="${OPENEMR_PROC_MOUNTS:-/proc/mounts}"
OPENEMR_METADATA_URL="${OPENEMR_METADATA_URL:-http://metadata.google.internal/computeMetadata/v1}"
OPENEMR_SECRETMANAGER_URL="${OPENEMR_SECRETMANAGER_URL:-https://secretmanager.googleapis.com/v1}"
OPENEMR_RUN_DIR="${OPENEMR_RUN_DIR:-/run}" # memory-backed (tmpfs) on COS: keys in transit never touch a disk
OPENEMR_POLL_SECONDS="${OPENEMR_POLL_SECONDS:-10}"

# ---- Names shared with edge-vm and between the two hooks.
OPENEMR_CONTAINER="openemr"
OPENEMR_CONFIG_LABEL="pulsebridge.config" # the same label-hash idea as edge-vm's containers
OPENEMR_DB_NAME="openemr"                 # the image's default database name
OPENEMR_ADMIN_USER="admin"                # the one account the image's first-time setup requires
OPENEMR_SITES_TARGET="/var/www/localhost/htdocs/openemr/sites"
OPENEMR_STOP_TIMEOUT_SECONDS=60  # SIGTERM to SIGKILL on `docker stop`
OPENEMR_PULL_TIMEOUT_SECONDS=900 # one `docker pull` attempt (S2: the 3.6 GB image took 489 s)
OPENEMR_DEADLINE_MARGIN_SECONDS=60
METADATA_SERVER_IP="169.254.169.254"
MARIADB_ROOT_SECRET="mariadb-root-password" # edge-vm's own secret file
OPENEMR_DB_SECRET="openemr-db-password"
OPENEMR_ADMIN_SECRET="openemr-admin-password"

# Every curl call: give up connecting after 10 s and on the whole transfer after 60 s.
OPENEMR_CURL=(curl --silent --connect-timeout 10 --max-time 60)

OPENEMR_DEADLINE=0

# ---- Logging and time ---------------------------------------------------------------------------

log() { printf '%s pulsebridge-openemr[%s]: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$OPENEMR_HOOK_NAME" "$*"; }
warn() { log "WARNING: $*"; }
error() { log "ERROR: $*" >&2; }
die() {
  error "$*"
  exit 1
}

now() { date +%s; }

remaining() {
  local left=$((OPENEMR_DEADLINE - $(now)))
  if [ "$left" -lt 0 ]; then left=0; fi
  echo "$left"
}

min() { if [ "$1" -lt "$2" ]; then echo "$1"; else echo "$2"; fi; }

# Pause before the next attempt, never past the deadline.
pause() { sleep "$(min "$OPENEMR_POLL_SECONDS" "$(remaining)")"; }

# ---- Start of every hook ------------------------------------------------------------------------

init_hook() {
  local name
  for name in EDGE_DATA_DIR EDGE_SECRETS_DIR EDGE_OPENEMR_DIR EDGE_STATE_DIR EDGE_DOCKER_NETWORK \
    EDGE_MARIADB_CONTAINER EDGE_HOOK_TIMEOUT_SECONDS; do
    if [ -z "${!name:-}" ]; then
      die "$name is not set: this hook must be run by the edge-vm startup script"
    fi
  done
  OPENEMR_DEADLINE=$(($(now) + EDGE_HOOK_TIMEOUT_SECONDS - OPENEMR_DEADLINE_MARGIN_SECONDS))
  OPENEMR_TLS_DIR="$EDGE_DATA_DIR/openemr-tls"       # OpenEMR's HTTPS certificate and key
  OPENEMR_CLIENT_DIR="$EDGE_DATA_DIR/openemr-client" # the registered client's state (no key)
  OPENEMR_TLS_CERT="$OPENEMR_TLS_DIR/server.crt"
  umask 077
  log "begin (gives up at the hook deadline, in $(remaining)s)"
}

# ---- Preconditions ------------------------------------------------------------------------------

is_mounted() {
  awk -v dir="$1" '$2 == dir { found = 1 } END { exit !found }' "$OPENEMR_PROC_MOUNTS"
}

require_data_disk() {
  is_mounted "$EDGE_DATA_DIR" || die "the data disk is not mounted at $EDGE_DATA_DIR: not touching OpenEMR (its site files and database live there)"
}

require_mariadb_healthy() {
  local health
  health=$(docker container inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$EDGE_MARIADB_CONTAINER" 2> /dev/null) || health="missing"
  [ "$health" = "healthy" ] || die "MariaDB ($EDGE_MARIADB_CONTAINER) is not healthy (health: $health): not starting OpenEMR, which exits if its database is not ready (spike S1)"
}

# Each named secret file must exist and be non-empty. Prints nothing about the contents.
require_secret_files() {
  local name missing=()
  for name in "$@"; do
    [ -s "$EDGE_SECRETS_DIR/$name" ] || missing+=("$name")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    die "secret files missing in $EDGE_SECRETS_DIR: ${missing[*]} (add a version to the Secret Manager secret, scripts/secrets from T27, then re-run the startup script)"
  fi
}

# The value of a secret file with trailing newlines removed (command substitution drops them), for
# the caller to capture into a variable. Never logged.
secret_value() {
  local value
  value=$(cat "$EDGE_SECRETS_DIR/$1")
  [ -n "$value" ] || return 1
  printf '%s' "$value"
}

container_status() {
  docker container inspect --format '{{.State.Status}}' "$1" 2> /dev/null || echo "missing"
}

# ---- Containers ---------------------------------------------------------------------------------

# Pull an image unless it is present, retrying until the deadline; each attempt is under `timeout`.
pull_image() {
  local ref=$1 attempt=0 left
  if docker image inspect "$ref" > /dev/null 2>&1; then
    log "image already present: $ref"
    return 0
  fi
  while :; do
    left=$(remaining)
    if [ "$left" -le 0 ]; then
      error "could not pull $ref before the hook deadline ($attempt tries). Check the VM account's read access to the Docker Hub mirror (project-base) and Private Google Access."
      return 1
    fi
    attempt=$((attempt + 1))
    log "pulling $ref (try $attempt; S2: about 8 minutes the first time)"
    if timeout "$(min "$OPENEMR_PULL_TIMEOUT_SECONDS" "$left")" docker pull --quiet "$ref" > /dev/null; then
      log "pulled $ref"
      return 0
    fi
    warn "pulling $ref failed or timed out (try $attempt, $(remaining)s left)"
    pause
  done
}

config_hash() {
  printf '%s\0' "$@" | sha256sum | awk '{ print substr($1, 1, 16) }'
}

# edge-vm's pattern: create the container if missing; if its configuration hash changed, stop it
# gracefully (SIGTERM, then up to OPENEMR_STOP_TIMEOUT_SECONDS before SIGKILL) and remove it without
# --force, then create it again; start it if it is stopped; otherwise leave it alone.
ensure_container() {
  local name=$1 hash current status
  shift
  hash=$(config_hash "$@")
  if docker container inspect "$name" > /dev/null 2>&1; then
    current=$(docker container inspect --format "{{index .Config.Labels \"$OPENEMR_CONFIG_LABEL\"}}" "$name")
    if [ "$current" = "$hash" ]; then
      status=$(container_status "$name")
      case "$status" in
        running | restarting) log "container $name is $status with the current configuration; nothing to do" ;;
        *)
          log "container $name is $status; starting it"
          docker start "$name" > /dev/null
          ;;
      esac
      return 0
    fi
    log "container $name has an older configuration (image, flags, certificate or Apache settings changed); stopping it gracefully and replacing it"
    docker stop --time "$OPENEMR_STOP_TIMEOUT_SECONDS" "$name" > /dev/null
    docker rm "$name" > /dev/null
  fi
  log "creating container $name"
  docker run --detach --name "$name" --label "$OPENEMR_CONFIG_LABEL=$hash" "$@" > /dev/null
}

# Run the PHP helper in a throwaway container of the OpenEMR image, with no network at all.
# Usage: run_client_tools <host dir mounted at /work> [extra docker options --] <tool arguments...>
# The tool arguments are never secret (directories, the address, the client ID, scopes).
run_client_tools() {
  local work=$1 extra=()
  shift
  while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
    extra+=("$1")
    shift
  done
  [ "${1:-}" = "--" ] && shift
  openemr_write_client_tools "$work/client-tools.php"
  timeout 120 docker run --rm --network none --user 0:0 --entrypoint php \
    --mount "type=bind,source=$work,target=/work" "${extra[@]}" \
    "$OPENEMR_IMAGE" /work/client-tools.php "$@"
}

# ---- OpenEMR over HTTPS -------------------------------------------------------------------------

# One HTTPS request to OpenEMR at the address fhir-proxy uses, verifying OpenEMR's certificate
# against the one this module generated (never --insecure). Prints the HTTP status (000 when no
# response); returns curl's exit code, so callers can tell a TLS failure from a timeout.
# Usage: openemr_request <path> <output file> [curl options...]
openemr_request() {
  local path=$1 out=$2
  shift 2
  "${OPENEMR_CURL[@]}" --cacert "$OPENEMR_TLS_CERT" --output "$out" --write-out '%{http_code}' "$@" \
    "$OPENEMR_SITE_ADDRESS$path"
}

# Poll OpenEMR's readiness endpoint until it answers 200 or the deadline passes. Spike S2: about
# 5 minutes of first-time setup on the e2-micro (after the image pull), 43 s after a reboot.
wait_until_ready() {
  local code rc tls_reported=0 polls=0
  log "waiting for $OPENEMR_SITE_ADDRESS/meta/health/readyz (first boot: about 5 minutes of setup)"
  while :; do
    rc=0
    code=$(openemr_request /meta/health/readyz /dev/null --max-time 30) || rc=$?
    polls=$((polls + 1))
    if [ "$code" = "200" ]; then
      log "OpenEMR is ready (readyz 200 after $polls polls)"
      return 0
    fi
    case "$rc" in
      35 | 51 | 58 | 59 | 60 | 77 | 83 | 90 | 91)
        if [ "$tls_reported" -eq 0 ]; then
          error "TLS verification of OpenEMR failed (curl exit $rc): it is not serving the certificate in $OPENEMR_TLS_CERT. Check tls_mount_targets against the image (README, \"TLS\")."
          tls_reported=1
        fi
        ;;
    esac
    if [ "$(remaining)" -le 0 ]; then
      error "OpenEMR did not become ready by the hook deadline (last status $code, curl exit $rc, $polls polls). See: docker logs $OPENEMR_CONTAINER"
      return 1
    fi
    pause
  done
}

# ---- The OpenEMR database -----------------------------------------------------------------------

# Run SQL from standard input against OpenEMR's database as OpenEMR's own user (not root), through
# the MariaDB container's client. The password reaches the client as MYSQL_PWD in its environment:
# `docker exec --env MYSQL_PWD` with no value copies it from this process's environment, so it is
# never an argument of docker or of the client.
openemr_sql() {
  (
    MYSQL_PWD=$(secret_value "$OPENEMR_DB_SECRET")
    export MYSQL_PWD
    timeout 60 docker exec -i --env MYSQL_PWD "$EDGE_MARIADB_CONTAINER" \
      mariadb --protocol=TCP --host=127.0.0.1 --user="$OPENEMR_DB_USER" \
      --batch --skip-column-names "$OPENEMR_DB_NAME"
  )
}

# The mariadb command-line client has no bound parameters, so every value placed in a SQL string
# literal is first checked to contain only characters that need no escaping (no quote, no
# backslash). The values are the client ID OpenEMR generated, the client name and the settings.
sql_literal() {
  if [[ ! "$1" =~ ^[A-Za-z0-9:/._-]*$ ]]; then
    error "refusing to put an unexpected value into SQL"
    return 1
  fi
  printf "'%s'" "$1"
}

# ---- Secret Manager -----------------------------------------------------------------------------

# An access token for the VM's service account, from the metadata server, on stdout. Never logged.
access_token() {
  local json
  json=$("${OPENEMR_CURL[@]}" --fail --header 'Metadata-Flavor: Google' \
    "$OPENEMR_METADATA_URL/instance/service-accounts/default/token") || return 1
  printf '%s' "$json" | sed -n 's/.*"access_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
}

# One Secret Manager call with the token on standard input (`--header @-`), so it is not in the
# process list. Prints the HTTP status. Usage: secretmanager_call <output file> <url> [curl options]
secretmanager_call() {
  local out=$1 url=$2 token code
  shift 2
  token=$(access_token) || token=""
  if [ -z "$token" ]; then
    echo "no-token"
    return 1
  fi
  code=$(printf 'Authorization: Bearer %s\n' "$token" |
    "${OPENEMR_CURL[@]}" --header @- --output "$out" --write-out '%{http_code}' "$@" "$url") || code=000
  token=""
  echo "$code"
}

# The version number in a Secret Manager response ("name": ".../versions/<n>").
version_number() {
  sed -n 's/.*"name"[[:space:]]*:[[:space:]]*"[^"]*\/versions\/\([0-9][0-9]*\)".*/\1/p' "$1" | head -n 1
}

# Remove files that held key material. On COS the run directory is a memory-backed filesystem, so
# overwriting first matters little, but costs nothing.
remove_sensitive() {
  local f
  for f in "$@"; do
    [ -e "$f" ] || continue
    if command -v shred > /dev/null 2>&1; then
      shred -u "$f" 2> /dev/null || rm -f "$f"
    else
      rm -f "$f"
    fi
  done
}
