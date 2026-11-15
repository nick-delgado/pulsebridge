#!/usr/bin/env bash
# Tests of the edge VM startup script (scripts/edge-startup.sh), the riskiest part of the module:
# it formats and mounts a disk that holds the clinic database, and it cannot be run on a real VM
# here. Three layers:
#
#   1. shellcheck and `bash -n` on the script (and on these test files);
#   2. a drift check: every EDGE_* setting the script reads is assigned by the Terraform template;
#   3. behaviour: the script runs in a sandbox with PATH set to fake commands only
#      (tests/fakes/fake-command linked as docker, curl, mount, blkid, mkfs.ext4, ...), against a
#      configuration block written here, and the cases below check what it did.
#
# Usage: infra/modules/edge-vm/tests/startup_script.sh   (needs bash and coreutils; shellcheck
# from PATH or $SHELLCHECK. Without shellcheck the lint layer is skipped with a warning, except in
# CI (CI=true), where a missing shellcheck fails.)

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script="$here/scripts/edge-startup.sh"
template="$here/templates/startup-config.sh.tftpl"
fake="$here/tests/fakes/fake-command"

failures=0
pass() { echo "ok:   $*"; }
fail() {
  echo "FAIL: $*"
  failures=$((failures + 1))
}

# ---- 1. Lint ----------------------------------------------------------------------------------------

shellcheck_bin="${SHELLCHECK:-$(command -v shellcheck || true)}"
if [ -n "$shellcheck_bin" ]; then
  if "$shellcheck_bin" -S style "$script" "$here/tests/startup_script.sh" "$here/tests/static_checks.sh" "$fake"; then
    pass "shellcheck: startup script, tests and fakes are clean"
  else
    fail "shellcheck reported problems"
  fi
elif [ "${CI:-}" = "true" ]; then
  fail "shellcheck is not installed (required in CI)"
else
  echo "WARN: shellcheck not found; set SHELLCHECK=/path/to/shellcheck to lint (CI requires it)"
fi

if bash -n "$script"; then pass "bash -n: startup script parses"; else fail "bash -n: startup script does not parse"; fi

# ---- 2. Drift check between the template and the script ---------------------------------------------

# Settings the script expects from part 1 (everything it reads as EDGE_* but does not assign).
assigned_in_script=$(grep -oE '^[[:space:]]*(export[[:space:]]+)?EDGE_[A-Z_]+=' "$script" | grep -oE 'EDGE_[A-Z_]+' | sort -u)
read_in_script=$(grep -oE '\$\{?EDGE_[A-Z_]+' "$script" | grep -oE 'EDGE_[A-Z_]+' | sort -u)
needed=$(comm -23 <(echo "$read_in_script") <(echo "$assigned_in_script"))
assigned_in_template=$(grep -oE '^EDGE_[A-Z_]+=' "$template" | grep -oE 'EDGE_[A-Z_]+' | sort -u)
missing=$(comm -23 <(echo "$needed") <(echo "$assigned_in_template"))
if [ -z "$missing" ] && [ -n "$needed" ]; then
  pass "drift: the template assigns every setting the script reads ($(echo "$needed" | wc -l) settings)"
else
  fail "drift: settings the script reads but the template does not assign: ${missing:-<none found at all>}"
fi

# ---- 3. Behaviour -----------------------------------------------------------------------------------

SB=""
cleanup() {
  if [ -n "$SB" ]; then rm -rf "$SB"; fi
}
trap cleanup EXIT

# Synthetic values. The secret and the token are sentinels: they must never show up in a log.
# They are made up at run time (a fresh random part each run) so no literal that looks like a
# credential is committed, and secret scanners have nothing to flag.
FAKE_SECRET="synthetic-db-root-sentinel-$RANDOM$RANDOM"
FAKE_TOKEN="synthetic-access-token-sentinel-$RANDOM$RANDOM"
ADAPTER_IMAGE="gcr.io/cloud-healthcare-containers/mllp-adapter@sha256:92eaa0fc5fcd0eb7ff62bd28c9140dcd7582bfec20790e5b203ca8dc75c4a90f"
MARIADB_IMAGE="us-central1-docker.pkg.dev/example-project-123/pulsebridge-dockerhub/library/mariadb:11.8@sha256:d36f18d65a0459510b8b0f643b226a6d299e3206486e2b64ff20754a6d291707"
ADAPTER_COMMAND=(
  /usr/mllp_adapter/mllp_adapter
  --port=2575
  --receiver_ip=0.0.0.0
  --hl7_v2_project_id=example-project-123
  --hl7_v2_location_id=us-central1
  --hl7_v2_dataset_id=example-dataset
  --hl7_v2_store_id=example-hl7v2-store
  --log_error_msg=false
  --export_stats=false
)
BOOT_WAIT=60 # the shared deadline used by every case (seconds of fake time)
MARIADB_SECRET_ENTRY="mariadb-root-password=projects/example-project-123/secrets/pulsebridge-db-root-password"
SECRET_ENTRIES=("$MARIADB_SECRET_ENTRY")
HOOKS=""
REAL_DATE=$(command -v date)
REAL_CHMOD=$(command -v chmod)
REAL_DD=$(command -v dd)

# A fresh sandbox: fake bin directory, a 64 MiB blank fake disk, fake /proc files, and a
# configuration block like the one Terraform renders, pointing at sandbox paths.
new_sandbox() {
  cleanup
  SB=$(mktemp -d)
  mkdir -p "$SB/bin" "$SB/state/hooks" "$SB/dev/disk/by-id" "$SB/mnt/disks" "$SB/stateful"
  local c real
  for c in docker docker-credential-gcr curl blkid wipefs mkfs.ext4 fsck.ext4 resize2fs mount mkswap swapon fallocate sleep timeout date chmod dd; do
    ln -s "$fake" "$SB/bin/$c"
  done
  # The real tools the script needs, and nothing else: no real mount, mkfs or docker is on PATH.
  for c in awk base64 bash basename cat cp cut env flock grep head ls mkdir mv readlink rm sed sha256sum sort stat touch tr truncate wc; do
    real=$(command -v "$c") || {
      echo "test setup: $c not found" >&2
      exit 2
    }
    ln -s "$real" "$SB/bin/$c"
  done
  truncate -s 64M "$SB/dev/disk/by-id/google-edge-data" # a new persistent disk reads as zeros
  : > "$SB/state/proc_mounts"
  : > "$SB/state/proc_swaps"
  : > "$SB/state/calls.log"
  echo 0 > "$SB/state/clock"
  SECRET_ENTRIES=("$MARIADB_SECRET_ENTRY")
  HOOKS=""
  write_config "${ADAPTER_COMMAND[@]}"
}

write_config() {
  {
    echo "EDGE_DATA_DEVICE='$SB/dev/disk/by-id/google-edge-data'"
    echo "EDGE_DATA_DIR='$SB/mnt/disks/edge'"
    echo "EDGE_STATE_DIR='$SB/var/lib/pulsebridge-edge'"
    echo "EDGE_SWAP_FILE='$SB/stateful/pulsebridge-swapfile'"
    echo "EDGE_SWAP_SIZE_MB='2048'"
    echo "EDGE_MIRROR_HOST='us-central1-docker.pkg.dev'"
    echo "EDGE_MIRROR_URL='us-central1-docker.pkg.dev/example-project-123/pulsebridge-dockerhub'"
    echo "EDGE_MARIADB_IMAGE='$MARIADB_IMAGE'"
    echo "EDGE_MLLP_ADAPTER_IMAGE='$ADAPTER_IMAGE'"
    echo "EDGE_MLLP_PORT='2575'"
    echo "EDGE_BOOT_WAIT_SECONDS='$BOOT_WAIT'"
    echo "EDGE_HOOK_TIMEOUT_SECONDS='1800'"
    echo "EDGE_MARIADB_COMMAND=('mariadbd' '--innodb-buffer-pool-size=64M' '--max-connections=30')"
    printf "EDGE_MLLP_ADAPTER_COMMAND=("
    printf "'%s' " "$@"
    echo ")"
    printf "EDGE_SECRET_FILES=("
    printf "'%s' " "${SECRET_ENTRIES[@]}"
    echo ")"
    echo "EDGE_HOOK_NAMES=($HOOKS)"
  } > "$SB/config.sh"
}

# Overwrite <length> bytes of the fake disk at <offset> with non-zero data (no truncation).
put_data() {
  head -c "$2" /dev/urandom | tr '\000' '\001' |
    dd of="$SB/dev/disk/by-id/google-edge-data" bs=1 seek="$1" conv=notrunc status=none
}

# Run the script (config block + logic, as Terraform joins them) in the sandbox. Output goes to
# $SB/out.log; the exit code to $RC.
RC=0
run_script() {
  cat "$SB/config.sh" "$script" > "$SB/startup.sh"
  RC=0
  env -i PATH="$SB/bin" FAKE_STATE="$SB/state" FAKE_SECRET="$FAKE_SECRET" FAKE_TOKEN="$FAKE_TOKEN" \
    REAL_DATE="$REAL_DATE" REAL_CHMOD="$REAL_CHMOD" REAL_DD="$REAL_DD" \
    EDGE_PROC_MOUNTS="$SB/state/proc_mounts" EDGE_PROC_SWAPS="$SB/state/proc_swaps" \
    EDGE_POLL_SECONDS=10 EDGE_ADAPTER_CHECK_SECONDS=15 \
    "$@" bash "$SB/startup.sh" > "$SB/out.log" 2>&1 || RC=$?
}

calls() { grep -cE "$1" "$SB/state/calls.log" || true; }
elapsed() { cat "$SB/state/clock"; }

expect_rc() {
  local want=$1 what=$2
  if [ "$want" = nonzero ]; then
    if [ "$RC" -ne 0 ]; then pass "$what: exits non-zero ($RC)"; else fail "$what: expected a non-zero exit, got 0"; fi
  elif [ "$RC" -eq "$want" ]; then pass "$what: exit $RC"; else
    fail "$what: expected exit $want, got $RC"
    sed 's/^/      | /' "$SB/out.log"
  fi
}
expect_calls() { # <regex> <count> <description>
  local n
  n=$(calls "$1")
  if [ "$n" -eq "$2" ]; then pass "$3"; else fail "$3 (expected $2 call(s) matching '$1', saw $n)"; fi
}
expect_log() {
  if grep -qE -- "$1" "$SB/out.log"; then pass "$2"; else
    fail "$2 (log has no line matching '$1')"
    sed 's/^/      | /' "$SB/out.log"
  fi
}
expect_no_secret_leak() {
  local f
  for f in "$SB/out.log" "$SB/state/calls.log"; do
    if grep -qF -e "$FAKE_SECRET" -e "$FAKE_TOKEN" -e "$(printf '%s' "$FAKE_SECRET" | base64 -w0)" "$f"; then
      fail "$1: the secret or the access token appears in $(basename "$f")"
      return
    fi
  done
  pass "$1: neither the secret nor the token (nor the secret's base64) appears in the log or in any command line"
}
expect_no_format() { # <description>
  expect_calls '^mkfs\.ext4 ' 0 "$1: does not format"
  expect_calls '^mount ' 0 "$1: does not mount"
  expect_calls '^docker run .*--name mariadb' 0 "$1: does not start MariaDB"
}
# The run took at most <seconds> of (fake) time, and nothing was called without a time limit.
expect_bounded() {
  local t
  t=$(elapsed)
  if [ "$t" -le "$1" ]; then pass "$2: finished within ${1}s of fake time (took ${t}s)"; else fail "$2: took ${t}s of fake time (limit ${1}s)"; fi
  expect_calls '^HANG' 0 "$2: no call ran without a time limit"
}

safety_check() {
  # The fakes must be what runs, never a real system command.
  local c
  for c in mount mkfs.ext4 docker swapon timeout; do
    if [ "$(PATH="$SB/bin" command -v "$c")" != "$SB/bin/$c" ]; then
      echo "test setup: $c does not resolve to the fake; refusing to run" >&2
      exit 2
    fi
  done
}

# -- Case: first boot on a new, blank disk ------------------------------------------------------------
echo "== first boot, blank disk"
new_sandbox
safety_check
run_script
expect_rc 0 "first boot"
expect_calls '^mkfs\.ext4 ' 1 "formats the blank disk exactly once"
expect_calls '^mount -t ext4 -o discard,defaults .*/google-edge-data .*/mnt/disks/edge$' 1 "mounts the disk by its stable device name"
expect_calls '^mkswap ' 1 "creates the swap file"
expect_calls '^swapon .*/pulsebridge-swapfile$' 1 "turns the swap file on"
expect_calls '^docker-credential-gcr configure-docker --registries=us-central1-docker\.pkg\.dev$' 1 "configures the credential helper for the mirror host"
expect_calls '^timeout [0-9]+ docker pull ' 2 "pulls the two images, each under a time limit"
expect_calls '^docker run ' 2 "starts two containers"
expect_calls 'secretmanager.*/versions/latest:access' 1 "reads the secret once"
expect_calls '^curl --silent --connect-timeout 10 --max-time 60 ' "$(calls '^curl ')" "every curl call has a connect timeout and a total time limit"
expect_no_secret_leak "first boot"

# The adapter: exactly the run options, the pinned image, the binary path and S4's flags.
mapfile -t got < "$SB/state/containers/mllp-adapter/args"
expected_adapter_args=(--detach --name mllp-adapter --label "${got[4]}"
  --restart unless-stopped --publish 2575:2575 --log-opt max-size=10m --log-opt max-file=3
  "$ADAPTER_IMAGE" "${ADAPTER_COMMAND[@]}")
if [ "${got[*]}" = "${expected_adapter_args[*]}" ] && [ "${#got[@]}" -eq "${#expected_adapter_args[@]}" ]; then
  pass "adapter: docker run with --restart unless-stopped, the digest-pinned image and exactly the configured flags"
else
  fail "adapter: unexpected docker run arguments:"
  printf '      | %s\n' "${got[@]}"
fi
if [[ "${got[4]}" =~ ^pulsebridge\.config=[0-9a-f]{16}$ ]]; then pass "adapter: carries a configuration-hash label"; else fail "adapter: label is '${got[4]}'"; fi

mapfile -t got < "$SB/state/containers/mariadb/args"
joined=$(printf '%s\n' "${got[@]}")
missing_args=0
for want in "--restart" "unless-stopped" "--stop-timeout" "120" "--network" "pulsebridge-edge" \
  "type=bind,source=$SB/mnt/disks/edge/mariadb,target=/var/lib/mysql" \
  "type=bind,source=$SB/mnt/disks/edge/secrets/mariadb-root-password,target=/run/secrets/mariadb-root-password,readonly" \
  "MARIADB_ROOT_PASSWORD_FILE=/run/secrets/mariadb-root-password" "$MARIADB_IMAGE" "mariadbd" "--innodb-buffer-pool-size=64M"; do
  if ! printf '%s\n' "$joined" | grep -qxF -- "$want"; then
    fail "mariadb: docker run lacks '$want'"
    missing_args=1
  fi
done
if [ "$missing_args" = 0 ]; then pass "mariadb: restart policy, 120 s stop timeout, network, bind mounts, password file, image and flags"; fi
if printf '%s\n' "$joined" | grep -q 'MARIADB_ROOT_PASSWORD='; then
  fail "mariadb: the password is passed as an environment value"
else
  pass "mariadb: password as a file only"
fi
if printf '%s\n' "$joined" | grep -qE '^(--publish|-p|--publish-all|-P)$|^--publish=|^--network=?host$'; then
  fail "mariadb: published on a host port (or on the host network)"
else
  pass "mariadb: never published on a host port"
fi

secret_file="$SB/mnt/disks/edge/secrets/mariadb-root-password"
if [ "$(cat "$secret_file")" = "$FAKE_SECRET" ] && [ "$(stat -c %a "$secret_file")" = 600 ] && [ "$(stat -c %a "$SB/mnt/disks/edge/secrets")" = 700 ]; then
  pass "secret: decoded into a mode-600 file in a mode-700 directory"
else
  fail "secret: file content or permissions wrong"
fi
if find "$SB/mnt/disks/edge/secrets" -name '*.tmp' | grep -q .; then fail "secret: temporary files left behind"; else pass "secret: no temporary response files left behind"; fi
expect_log 'mllp-adapter is up and stayed up' "checks that the adapter stayed up"

# -- Case: second run on the same boot (re-run by hand) -----------------------------------------------
echo "== second run, nothing to do"
: > "$SB/state/calls.log"
run_script
expect_rc 0 "second run"
expect_calls '^mkfs\.ext4 ' 0 "second run formats nothing"
expect_calls '^mount ' 0 "second run does not mount again"
expect_calls '^(mkswap|swapon|fallocate) ' 0 "second run leaves swap alone"
expect_calls '^docker (run|start|stop|restart|rm|pull|kill) ' 0 "second run starts, stops, restarts, replaces and pulls nothing"
expect_calls '^docker container inspect mariadb$' 1 "second run did look at MariaDB (so 'nothing' is a decision, not a skipped step)"
expect_calls 'secretmanager' 0 "second run does not read the secret again"
expect_no_secret_leak "second run"

# -- Case: a second run while the first still holds the lock --------------------------------------------
echo "== concurrent run"
: > "$SB/state/calls.log"
exec 8> "$SB/var/lib/pulsebridge-edge/startup.lock"
flock 8
run_script
exec 8>&-
expect_rc nonzero "concurrent run"
expect_log 'another run of the startup script holds .*startup.lock' "refuses to run while another run holds the lock"
expect_calls '^(docker|mount|mkfs\.ext4|swapon) ' 0 "does nothing while the lock is held"

# -- Case: reboot (mounts and swap gone, containers stopped, disk and swap file kept) -----------------
echo "== reboot"
: > "$SB/state/calls.log"
: > "$SB/state/proc_mounts"
: > "$SB/state/proc_swaps"
echo exited > "$SB/state/containers/mariadb/status"
echo exited > "$SB/state/containers/mllp-adapter/status"
run_script
expect_rc 0 "reboot"
expect_calls '^mkfs\.ext4 ' 0 "reboot does not format the disk that already has a filesystem"
expect_calls '^mount ' 1 "reboot mounts the disk again"
expect_calls '^mkswap ' 0 "reboot reuses the existing swap file"
expect_calls '^swapon ' 1 "reboot turns swap on again"
expect_calls '^docker start (mariadb|mllp-adapter)$' 2 "reboot starts both stopped containers"
expect_calls '^docker run ' 0 "reboot creates no new container"

# -- Case: reboot with the data disk gone (detached). The sandbox keeps the old files under the mount
# point, which a real VM would not (/mnt/disks is emptied on boot): MariaDB must still not start
# on them. Two guards protect this (main's gate on the disk step and start_mariadb's own mount
# check); this case fails only if both are broken.
echo "== reboot without the data disk"
cp -a "$SB" "$SB.keep"
: > "$SB/state/calls.log"
: > "$SB/state/proc_mounts"
echo exited > "$SB/state/containers/mariadb/status"
rm "$SB/dev/disk/by-id/google-edge-data"
run_script
expect_rc nonzero "reboot without disk"
expect_calls '^docker (start|run) .*mariadb' 0 "does not start MariaDB on files that are not on the mounted disk"
rm -rf "$SB"
mv "$SB.keep" "$SB"

# -- Case: configuration change (a new adapter flag or digest) -----------------------------------------
echo "== adapter configuration changed"
: > "$SB/state/calls.log"
write_config "${ADAPTER_COMMAND[@]/--log_error_msg=false/--log_error_msg=true}"
run_script
expect_rc 0 "configuration change"
expect_calls '^docker stop --time 120 mllp-adapter$' 1 "stops the old adapter gracefully (SIGTERM, 120 s before SIGKILL)"
expect_calls '^docker rm mllp-adapter$' 1 "then removes it"
expect_calls '^docker rm --force' 0 "never force-removes a running container"
if [ "$(grep -nE '^docker stop --time 120 mllp-adapter$' "$SB/state/calls.log" | cut -d: -f1)" -lt "$(grep -nE '^docker rm mllp-adapter$' "$SB/state/calls.log" | cut -d: -f1)" ]; then
  pass "stop comes before rm"
else
  fail "rm came before stop"
fi
expect_calls '^docker run .*--name mllp-adapter' 1 "creates it again with the new flags"
expect_calls '^docker (stop|rm|run) .*mariadb' 0 "leaves MariaDB alone"

echo "== MariaDB configuration changed"
: > "$SB/state/calls.log"
sed -i "s/'--max-connections=30'/'--max-connections=31'/" "$SB/config.sh"
run_script
expect_rc 0 "MariaDB configuration change"
expect_calls '^docker stop --time 120 mariadb$' 1 "stops MariaDB gracefully before replacing it"
expect_calls '^docker rm --force' 0 "never force-removes MariaDB"

# -- Case: the disk already has a filesystem on first boot --------------------------------------------
echo "== first boot, disk with an ext4 filesystem"
new_sandbox
echo ext4 > "$SB/state/fstype"
put_data 1024 4096
run_script
expect_rc 0 "existing filesystem"
expect_calls '^mkfs\.ext4 ' 0 "never formats a disk that has a filesystem"
expect_calls '^mount ' 1 "mounts it"

echo "== first boot, disk with another filesystem (xfs)"
new_sandbox
echo xfs > "$SB/state/fstype"
run_script
expect_rc nonzero "xfs"
expect_no_format "xfs"

echo "== first boot, blkid exit 4 (usage error)"
new_sandbox
echo ERR > "$SB/state/fstype"
run_script
expect_rc nonzero "blkid exit 4"
expect_no_format "blkid exit 4"

# -- Cases: blkid says "nothing found" (exit 2) but the disk is NOT provably blank ------------------------
# util-linux returns 2 for a blank device, an unreadable one and unrecognised contents alike.
echo "== blkid exit 2, unrecognised data at the start"
new_sandbox
put_data 0 65536
run_script
expect_rc nonzero "unrecognised data"
expect_no_format "unrecognised data"
expect_log 'blkid recognises no filesystem, but the disk is not blank or could not be read. NOT formatting' "explains why it did not format and how to investigate"
expect_log 'e2fsck -n -b 32768' "points at the read-only backup-superblock check"

echo "== blkid exit 2, primary superblock zeroed but data right after it"
new_sandbox
put_data 4096 1048576 # block group descriptors and inode tables; bytes 0-4095 stay zero
run_script
expect_rc nonzero "zeroed superblock"
expect_no_format "zeroed primary superblock"

echo "== blkid exit 2, data only in the middle of the disk"
new_sandbox
put_data $((32 * 1048576)) 4096
run_script
expect_no_format "data in the middle"

echo "== blkid exit 2, data only at the end of the disk"
new_sandbox
put_data $((64 * 1048576 - 8192)) 4096
run_script
expect_no_format "data at the end"

echo "== blkid exit 2, disk cannot be read"
new_sandbox
rm "$SB/dev/disk/by-id/google-edge-data"
mkdir "$SB/dev/disk/by-id/google-edge-data" # exists, but reading it fails like an I/O error
run_script
expect_rc nonzero "unreadable disk"
expect_no_format "unreadable disk"

for mode in error short; do
  echo "== blkid exit 2, disk reports its size but reading it gives: $mode"
  new_sandbox
  run_script FAKE_DD_READ="$mode"
  expect_rc nonzero "read $mode"
  expect_no_format "read $mode"
done

echo "== blkid exit 2, reads as zeros but wipefs sees a signature"
new_sandbox
echo "DEVICE OFFSET TYPE UUID LABEL" > "$SB/state/wipefs_signature"
run_script
expect_no_format "wipefs signature"

echo "== fsck reports errors it could not fix"
new_sandbox
echo ext4 > "$SB/state/fstype"
run_script FAKE_FSCK_RC=4
expect_rc nonzero "fsck exit 4"
expect_calls '^mount ' 0 "does not mount a filesystem fsck could not fix"
expect_log "fsck.ext4 found problems .* \\(exit 4\\); not mounting it" "says fsck failed"

# -- Case: the data disk is missing --------------------------------------------------------------------
echo "== data disk missing"
new_sandbox
rm "$SB/dev/disk/by-id/google-edge-data"
run_script
expect_rc nonzero "missing disk"
expect_log 'ERROR: data disk .*google-edge-data not found' "logs clearly that the data disk is missing"
expect_log 'ERROR: .*finished with failed steps: data-disk' "names the failed step"
expect_calls '^mkfs\.ext4 ' 0 "formats nothing when the disk is missing"
expect_calls '^docker run .*--name mariadb' 0 "does not start MariaDB without its disk"
expect_calls '^docker run .*--name mllp-adapter' 1 "still starts the MLLP adapter (it needs no disk)"

# -- Case: the adapter crash-loops ----------------------------------------------------------------------
echo "== adapter crash-loop"
new_sandbox
run_script FAKE_ADAPTER_CRASH=1
expect_rc nonzero "crash-looping adapter"
expect_log 'ERROR: mllp-adapter is crash-looping or down' "logs that the adapter is crash-looping"
expect_log 'mllp-adapter\| Usage of /usr/mllp_adapter/mllp_adapter' "shows the adapter's last log lines"
expect_log 'finished with failed steps: mllp-adapter-check' "names the failed check"
expect_calls '^docker run .*--name mariadb' 1 "MariaDB is still started"

# -- Case: the MariaDB secret has no version yet --------------------------------------------------------
echo "== MariaDB secret without a version"
new_sandbox
run_script FAKE_SECRET_MISSING=db-root-password
expect_rc nonzero "secret missing"
expect_log 'ERROR: secret mariadb-root-password .* before the boot deadline .*404' "logs which secret is missing and why"
n=$(calls 'secretmanager.*/versions/latest:access')
if [ "$n" -ge 3 ]; then pass "retries until the shared deadline ($n tries)"; else fail "gave up too early ($n tries)"; fi
expect_calls '^docker run .*--name mariadb' 0 "does not start MariaDB without its password"
expect_calls '^docker run .*--name mllp-adapter' 1 "still starts the MLLP adapter"
if [ -e "$SB/mnt/disks/edge/secrets/mariadb-root-password" ]; then fail "an empty secret file was left"; else pass "writes no secret file"; fi
expect_bounded $((BOOT_WAIT + 30)) "secret missing"
expect_no_secret_leak "secret missing"

# -- Case: extra secrets (T23's) missing: MariaDB must not depend on them ----------------------------------
echo "== two extra secrets missing"
new_sandbox
SECRET_ENTRIES=("$MARIADB_SECRET_ENTRY"
  "openemr-db-password=projects/example-project-123/secrets/pulsebridge-openemr-db-password"
  "openemr-admin-password=projects/example-project-123/secrets/pulsebridge-openemr-admin-password")
HOOKS="'openemr'"
write_config "${ADAPTER_COMMAND[@]}"
printf 'echo "hook ran" >> "%s"\n' "$SB/state/calls.log" > "$SB/state/hooks/openemr"
run_script FAKE_SECRET_MISSING=openemr-db-password,openemr-admin-password
expect_rc nonzero "extra secrets missing"
expect_calls '^docker run .*--name mariadb' 1 "MariaDB still starts with only its own secret"
expect_log 'MariaDB is healthy' "MariaDB becomes healthy"
expect_log 'ERROR: secret openemr-db-password ' "names the first missing secret"
expect_log 'ERROR: secret openemr-admin-password ' "names the second missing secret"
expect_log 'finished with failed steps: other-secrets' "the failed step is other-secrets, not mariadb"
expect_calls '^hook ran$' 1 "hooks still run (a hook decides what it needs)"
expect_bounded $((BOOT_WAIT + 30)) "two missing secrets share one deadline"
n_db=$(calls 'secrets/pulsebridge-openemr-db-password/versions')
n_admin=$(calls 'secrets/pulsebridge-openemr-admin-password/versions')
if [ "$n_db" -ge 2 ] && [ "$n_admin" -eq "$n_db" ]; then
  pass "both missing secrets were retried in the same rounds ($n_db each)"
else
  fail "missing secrets were not retried together (db $n_db, admin $n_admin)"
fi

# -- Case: writing the secret file fails ------------------------------------------------------------------
echo "== chmod of the secret file fails"
new_sandbox
run_script FAKE_CHMOD_FAIL=1
expect_rc nonzero "chmod failure"
expect_log 'secret mariadb-root-password .*chmod-failed' "reports the failed chmod"
expect_calls '^docker run .*--name mariadb' 0 "does not start MariaDB"
if find "$SB/mnt/disks/edge/secrets" -name '*.tmp' -o -name 'mariadb-root-password' | grep -q .; then
  fail "a secret file or temporary file was left behind"
else
  pass "leaves no secret file or temporary file behind"
fi
expect_no_secret_leak "chmod failure"

# -- Cases: a blackholed network. Every wait must end at the deadline, not hours later ------------------
echo "== every curl call hangs"
new_sandbox
run_script FAKE_CURL_HANG=1
expect_rc nonzero "curl hangs"
expect_bounded $((BOOT_WAIT + 120)) "curl hangs"
expect_calls '^docker run .*--name mariadb' 0 "no MariaDB without its secret"

echo "== docker pull hangs"
new_sandbox
run_script FAKE_HANG="docker pull"
expect_rc nonzero "pull hangs"
expect_bounded $((BOOT_WAIT + 120)) "pull hangs"
expect_log 'ERROR: could not pull .*before the boot deadline.*Fallback: project-base enable_cloud_nat' "logs the pull failure with the NAT fallback"
expect_calls '^docker run ' 0 "starts nothing it could not pull"

echo "== image pull fails"
new_sandbox
run_script FAKE_PULL_FAIL=1
expect_rc nonzero "pull failure"
expect_bounded $((BOOT_WAIT + 30)) "pull failure"
expect_calls '^docker run ' 0 "starts nothing it could not pull"

# -- Case: MariaDB never becomes healthy -----------------------------------------------------------------
echo "== MariaDB unhealthy"
new_sandbox
HOOKS="'openemr'"
write_config "${ADAPTER_COMMAND[@]}"
printf 'echo "hook ran" >> "%s"\n' "$SB/state/calls.log" > "$SB/state/hooks/openemr"
run_script FAKE_MARIADB_HEALTH=starting
expect_rc nonzero "unhealthy MariaDB"
expect_log 'ERROR: MariaDB is not healthy by the boot deadline' "logs that MariaDB is not healthy"
expect_calls '^hook ran$' 0 "does not run hooks before MariaDB is healthy"
expect_bounded $((BOOT_WAIT + 30)) "unhealthy MariaDB"

# -- Case: hooks (the seam for T23) -----------------------------------------------------------------------
echo "== hooks"
new_sandbox
HOOKS="'a-fails' 'b-openemr'"
write_config "${ADAPTER_COMMAND[@]}"
echo 'exit 3' > "$SB/state/hooks/a-fails"
cat > "$SB/state/hooks/b-openemr" << HOOK
echo "hook ran with \$EDGE_SECRETS_DIR \$EDGE_DOCKER_NETWORK \$EDGE_MARIADB_CONTAINER \$EDGE_OPENEMR_DIR \$EDGE_HOOK_TIMEOUT_SECONDS" >> "$SB/state/calls.log"
if { true >&9; } 2> /dev/null; then echo "hook holds fd 9" >> "$SB/state/calls.log"; fi
HOOK
run_script
expect_rc nonzero "a failing hook"
expect_log 'ERROR: hook a-fails: FAILED \(exit 3\)' "reports the failing hook"
expect_calls "^hook ran with $SB/mnt/disks/edge/secrets pulsebridge-edge mariadb $SB/mnt/disks/edge/openemr 1800\$" 1 "the next hook still runs, with the documented environment"
expect_calls '^timeout 1800 bash .*/hooks/b-openemr\.sh$' 1 "each hook runs under its time limit"
expect_calls '^hook holds fd 9$' 0 "hooks do not inherit the run lock's file descriptor"
if [ "$(grep -nE '^docker run .*--name mariadb' "$SB/state/calls.log" | cut -d: -f1)" -lt "$(grep -n '^hook ran' "$SB/state/calls.log" | cut -d: -f1)" ]; then
  pass "hooks run after MariaDB is started and healthy"
else
  fail "a hook ran before MariaDB"
fi

# -- Case: a command the script needs is missing ----------------------------------------------------------
for missing_cmd in docker timeout; do
  echo "== missing command: $missing_cmd"
  new_sandbox
  rm "$SB/bin/$missing_cmd"
  run_script
  expect_rc nonzero "missing $missing_cmd"
  expect_log "commands missing on this image: $missing_cmd" "names the missing command"
  expect_calls '^(mkfs\.ext4|mount|swapon) ' 0 "does nothing else"
done

echo
if [ "$failures" -eq 0 ]; then
  echo "startup script tests: all passed"
else
  echo "startup script tests: $failures failure(s)"
fi
[ "$failures" -eq 0 ]
