# shellcheck shell=bash
#
# PulseBridge wrapper around the official MariaDB image's entrypoint (task T42). It runs INSIDE the
# MariaDB container, as root, as `bash -c <this text> pulsebridge-mariadb-entrypoint <command...>`
# (scripts/edge-startup.sh, start_mariadb). The image itself is not changed: this only decides
# which copy of the entrypoint to run, and passes the command through unchanged.
#
# Why: on a new, empty data directory the official entrypoint initialises the database, then
# starts a temporary server (to set the root password and create the health-check users) and
# waits for it only 30 seconds, `for i in {30..0}` in docker_temp_server_start, with no setting to
# change that. On prod's e2-micro that server took over 33 s at boot, the entrypoint gave up
# ("Unable to start server.") and left a half-initialised data directory behind.
#
# What it does:
#   - data directory already initialised (it has the `mysql` system schema directory, the same
#     test the entrypoint uses for DATABASE_ALREADY_EXISTS): run the official entrypoint,
#     unchanged. The first initialisation is the only place the short wait matters.
#   - otherwise (first initialisation): copy the entrypoint with the loop bound raised from 30 to
#     PULSEBRIDGE_INIT_WAIT_SECONDS, and run the copy. Only if the loop text is found EXACTLY once;
#     otherwise refuse and exit non-zero, so an image update that changed the entrypoint is
#     noticed at its first initialisation instead of silently getting the 30-second wait back.
#
# The copy lives in the container's own writable layer (/tmp), is rewritten on every container
# start, and is executable, because the entrypoint re-executes itself as the mysql user with
# `exec gosu mysql "${BASH_SOURCE[0]}"`.
#
# The PULSEBRIDGE_ENTRYPOINT, PULSEBRIDGE_PATCHED_ENTRYPOINT and PULSEBRIDGE_DATADIR overrides are
# test seams (tests/startup_script.sh); the container never sets them. No secret passes through
# here: the root password reaches the entrypoint as a file (MARIADB_ROOT_PASSWORD_FILE).

set -euo pipefail

official="${PULSEBRIDGE_ENTRYPOINT:-/usr/local/bin/docker-entrypoint.sh}"
patched="${PULSEBRIDGE_PATCHED_ENTRYPOINT:-/tmp/pulsebridge-docker-entrypoint.sh}"
datadir="${PULSEBRIDGE_DATADIR:-/var/lib/mysql}"
wait_seconds="${PULSEBRIDGE_INIT_WAIT_SECONDS:-}"

# The line in docker_temp_server_start that bounds the wait (mariadb-docker 11.8/docker-entrypoint.sh).
loop_text='for i in {30..0}; do'

# Same shape as the entrypoint's own mysql_note / mysql_error lines, so they read together in
# `docker logs mariadb`.
note() { printf '%s [Note] [PulseBridge]: %s\n' "$(date --rfc-3339=seconds)" "$*"; }
refuse() {
  printf '%s [ERROR] [PulseBridge]: %s\n' "$(date --rfc-3339=seconds)" "$*" >&2
  exit 1
}

if [ -d "$datadir/mysql" ]; then
  note "data directory already initialised: running the official entrypoint unchanged"
  exec "$official" "$@"
fi

if ! [[ "$wait_seconds" =~ ^[1-9][0-9]*$ ]]; then
  refuse "PULSEBRIDGE_INIT_WAIT_SECONDS must be a whole number of seconds; refusing to initialise"
fi

# grep -c counts matching lines; -F: the braces and dots are literal text, not a pattern.
found=$(grep -cF -- "$loop_text" "$official" || true)
if [ "$found" != 1 ]; then
  refuse "expected '$loop_text' exactly once in $official, found it ${found:-0} times: the image's entrypoint has changed, so the longer first-initialisation wait cannot be applied. Refusing to initialise (fix scripts/mariadb-entrypoint-wrapper.sh for the new entrypoint, or pin the previous image)."
fi

# Replace only the bound; every other byte of the entrypoint stays as the image ships it.
sed "s/for i in {30\.\.0}; do/for i in {${wait_seconds}..0}; do/" "$official" > "$patched.tmp"
if [ "$(grep -cF -- "for i in {${wait_seconds}..0}; do" "$patched.tmp" || true)" != 1 ] ||
  [ "$(grep -cF -- "$loop_text" "$patched.tmp" || true)" != 0 ]; then
  rm -f "$patched.tmp"
  refuse "rewriting the wait loop in a copy of $official did not give exactly one new loop; refusing to initialise"
fi
chmod 755 "$patched.tmp"
mv -f "$patched.tmp" "$patched"

note "first initialisation: the temporary server may take up to ${wait_seconds}s to start (the official entrypoint allows 30s)"
exec "$patched" "$@"
