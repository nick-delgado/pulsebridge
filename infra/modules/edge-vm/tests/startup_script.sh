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
  --log_error_msg=true
  --export_stats=false
)

# A fresh sandbox: fake bin directory, fake device, fake /proc files, and a configuration block
# like the one Terraform renders, pointing at sandbox paths.
new_sandbox() {
  cleanup
  SB=$(mktemp -d)
  mkdir -p "$SB/bin" "$SB/state/hooks" "$SB/dev/disk/by-id" "$SB/mnt/disks" "$SB/stateful"
  local c real
  for c in docker docker-credential-gcr curl blkid mkfs.ext4 fsck.ext4 resize2fs mount mkswap swapon fallocate sleep; do
    ln -s "$fake" "$SB/bin/$c"
  done
  # The real tools the script needs, and nothing else: no real mount, mkfs or docker is on PATH.
  for c in awk base64 bash basename cat chmod cp cut date dd env flock grep head ls mkdir mv readlink rm sed sha256sum sort stat touch tr truncate; do
    real=$(command -v "$c") || {
      echo "test setup: $c not found" >&2
      exit 2
    }
    ln -s "$real" "$SB/bin/$c"
  done
  : > "$SB/dev/disk/by-id/google-edge-data"
  : > "$SB/state/proc_mounts"
  : > "$SB/state/proc_swaps"
  : > "$SB/state/calls.log"
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
    echo "EDGE_BOOT_WAIT_SECONDS='60'"
    echo "EDGE_MARIADB_COMMAND=('mariadbd' '--innodb-buffer-pool-size=64M' '--max-connections=30')"
    printf "EDGE_MLLP_ADAPTER_COMMAND=("
    printf "'%s' " "$@"
    echo ")"
    echo "EDGE_SECRET_FILES=('mariadb-root-password=projects/example-project-123/secrets/pulsebridge-db-root-password')"
    echo "EDGE_HOOK_NAMES=(${HOOKS:-})"
  } > "$SB/config.sh"
}

# Run the script (config block + logic, as Terraform joins them) in the sandbox. Output goes to
# $SB/out.log; the exit code to $RC.
RC=0
run_script() {
  cat "$SB/config.sh" "$script" > "$SB/startup.sh"
  RC=0
  env -i PATH="$SB/bin" FAKE_STATE="$SB/state" FAKE_SECRET="$FAKE_SECRET" FAKE_TOKEN="$FAKE_TOKEN" \
    EDGE_PROC_MOUNTS="$SB/state/proc_mounts" EDGE_PROC_SWAPS="$SB/state/proc_swaps" \
    EDGE_POLL_SECONDS=10 EDGE_ADAPTER_CHECK_SECONDS=15 \
    "$@" bash "$SB/startup.sh" > "$SB/out.log" 2>&1 || RC=$?
}

calls() { grep -cE "$1" "$SB/state/calls.log" || true; }

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

safety_check() {
  # The fakes must be what runs, never a real system command.
  local c
  for c in mount mkfs.ext4 docker swapon; do
    if [ "$(PATH="$SB/bin" command -v "$c")" != "$SB/bin/$c" ]; then
      echo "test setup: $c does not resolve to the fake; refusing to run" >&2
      exit 2
    fi
  done
}

# -- Case: first boot on a new, empty disk -----------------------------------------------------------
echo "== first boot, empty disk"
new_sandbox
safety_check
run_script
expect_rc 0 "first boot"
expect_calls '^mkfs\.ext4 ' 1 "formats the empty disk exactly once"
expect_calls '^mount -t ext4 -o discard,defaults .*/google-edge-data .*/mnt/disks/edge$' 1 "mounts the disk by its stable device name"
expect_calls '^mkswap ' 1 "creates the swap file"
expect_calls '^swapon .*/pulsebridge-swapfile$' 1 "turns the swap file on"
expect_calls '^docker-credential-gcr configure-docker --registries=us-central1-docker\.pkg\.dev$' 1 "configures the credential helper for the mirror host"
expect_calls '^docker pull ' 2 "pulls the two images"
expect_calls '^docker run ' 2 "starts two containers"
expect_calls 'secretmanager.*/versions/latest:access' 1 "reads the secret once"
expect_no_secret_leak "first boot"

# The adapter: exactly the run options, the pinned image, the binary path and the S4 flags.
mapfile -t got < "$SB/state/containers/mllp-adapter/args"
expected_adapter_args=(--detach --name mllp-adapter --label "${got[4]}"
  --restart unless-stopped --publish 2575:2575 --log-opt max-size=10m --log-opt max-file=3
  "$ADAPTER_IMAGE" "${ADAPTER_COMMAND[@]}")
if [ "${got[*]}" = "${expected_adapter_args[*]}" ] && [ "${#got[@]}" -eq "${#expected_adapter_args[@]}" ]; then
  pass "adapter: docker run with --restart unless-stopped, the digest-pinned image and exactly the S4 flags"
else
  fail "adapter: unexpected docker run arguments:"
  printf '      | %s\n' "${got[@]}"
fi
if [[ "${got[4]}" =~ ^pulsebridge\.config=[0-9a-f]{16}$ ]]; then pass "adapter: carries a configuration-hash label"; else fail "adapter: label is '${got[4]}'"; fi

mapfile -t got < "$SB/state/containers/mariadb/args"
joined=$(printf '%s\n' "${got[@]}")
for want in "--restart" "unless-stopped" "--network" "pulsebridge-edge" \
  "type=bind,source=$SB/mnt/disks/edge/mariadb,target=/var/lib/mysql" \
  "type=bind,source=$SB/mnt/disks/edge/secrets/mariadb-root-password,target=/run/secrets/mariadb-root-password,readonly" \
  "MARIADB_ROOT_PASSWORD_FILE=/run/secrets/mariadb-root-password" "$MARIADB_IMAGE" "mariadbd" "--innodb-buffer-pool-size=64M"; do
  if printf '%s\n' "$joined" | grep -qxF -- "$want"; then :; else fail "mariadb: docker run lacks '$want'"; fi
done
if printf '%s\n' "$joined" | grep -q 'MARIADB_ROOT_PASSWORD='; then
  fail "mariadb: the password is passed as an environment value"
else
  pass "mariadb: data and password are bind mounts from the data disk; password as a file only"
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
expect_calls '^docker (run|start|rm|pull) ' 0 "second run starts, replaces and pulls nothing"
expect_calls 'secretmanager' 0 "second run does not read the secret again"
expect_no_secret_leak "second run"

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
write_config "${ADAPTER_COMMAND[@]/--log_error_msg=true/--log_error_msg=false}"
run_script
expect_rc 0 "configuration change"
expect_calls '^docker rm --force mllp-adapter$' 1 "replaces the adapter whose configuration changed"
expect_calls '^docker run .*--name mllp-adapter' 1 "creates it again with the new flags"
expect_calls '^docker (rm|run) .*mariadb' 0 "leaves MariaDB alone"

# -- Case: the disk already has a filesystem on first boot --------------------------------------------
echo "== first boot, disk with an ext4 filesystem"
new_sandbox
echo ext4 > "$SB/state/fstype"
run_script
expect_rc 0 "existing filesystem"
expect_calls '^mkfs\.ext4 ' 0 "never formats a disk that has a filesystem"
expect_calls '^mount ' 1 "mounts it"

for other in xfs ERR; do
  echo "== first boot, disk with blkid result '$other'"
  new_sandbox
  echo "$other" > "$SB/state/fstype"
  run_script
  expect_rc nonzero "blkid says $other"
  expect_calls '^mkfs\.ext4 ' 0 "does not format when blkid says $other"
  expect_calls '^mount ' 0 "does not mount when blkid says $other"
  expect_calls '^docker run .*--name mariadb' 0 "does not start MariaDB when blkid says $other"
done

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

# -- Case: the secret has no version yet ---------------------------------------------------------------
echo "== secret without a version"
new_sandbox
run_script FAKE_SECRET_MISSING=1
expect_rc nonzero "secret missing"
expect_log 'ERROR: secret mariadb-root-password .* no readable version .*HTTP status 404' "logs which secret is missing and why"
expect_calls 'secretmanager.*/versions/latest:access' 6 "retries for the boot wait (60 s at 10 s)"
expect_calls '^docker run .*--name mariadb' 0 "does not start MariaDB without its password"
expect_calls '^docker run .*--name mllp-adapter' 1 "still starts the MLLP adapter"
if [ -e "$SB/mnt/disks/edge/secrets/mariadb-root-password" ]; then fail "an empty secret file was left"; else pass "writes no secret file"; fi
expect_no_secret_leak "secret missing"

# -- Case: images cannot be pulled (no route to Artifact Registry) ---------------------------------------
echo "== image pull fails"
new_sandbox
run_script FAKE_PULL_FAIL=1
expect_rc nonzero "pull failure"
expect_log 'ERROR: could not pull .*Fallback: project-base enable_cloud_nat' "logs the pull failure with the NAT fallback"
expect_calls '^docker run ' 0 "starts nothing it could not pull"

# -- Case: MariaDB never becomes healthy -----------------------------------------------------------------
echo "== MariaDB unhealthy"
new_sandbox
HOOKS="'openemr'"
write_config "${ADAPTER_COMMAND[@]}"
printf 'echo "hook ran" >> "%s"\n' "$SB/state/calls.log" > "$SB/state/hooks/openemr"
run_script FAKE_MARIADB_HEALTH=starting
expect_rc nonzero "unhealthy MariaDB"
expect_log 'ERROR: MariaDB is not healthy' "logs that MariaDB is not healthy"
expect_calls '^hook ran$' 0 "does not run hooks before MariaDB is healthy"

# -- Case: hooks (the seam for T23) -----------------------------------------------------------------------
echo "== hooks"
new_sandbox
HOOKS="'openemr'"
write_config "${ADAPTER_COMMAND[@]}"
cat > "$SB/state/hooks/openemr" << EOF
echo "hook ran with \$EDGE_SECRETS_DIR \$EDGE_DOCKER_NETWORK \$EDGE_MARIADB_CONTAINER \$EDGE_OPENEMR_DIR" >> "$SB/state/calls.log"
EOF
run_script
expect_rc 0 "hook"
expect_calls "^hook ran with $SB/mnt/disks/edge/secrets pulsebridge-edge mariadb $SB/mnt/disks/edge/openemr\$" 1 "runs the hook with the documented environment"
if [ "$(grep -nE '^docker run .*--name mariadb' "$SB/state/calls.log" | cut -d: -f1)" -lt "$(grep -n '^hook ran' "$SB/state/calls.log" | cut -d: -f1)" ]; then
  pass "the hook runs after MariaDB is started and healthy"
else
  fail "the hook ran before MariaDB"
fi
echo 'exit 3' > "$SB/state/hooks/openemr"
run_script
expect_rc nonzero "failing hook"
expect_log 'ERROR: hook openemr: FAILED \(exit 3\)' "reports a failing hook"
HOOKS=""

# -- Case: a command the script needs is missing ----------------------------------------------------------
echo "== missing command"
new_sandbox
rm "$SB/bin/docker"
run_script
expect_rc nonzero "missing command"
expect_log 'commands missing on this image: docker' "names the missing command"
expect_calls '^(mkfs\.ext4|mount|swapon) ' 0 "does nothing else"

echo
if [ "$failures" -eq 0 ]; then
  echo "startup script tests: all passed"
else
  echo "startup script tests: $failures failure(s)"
fi
[ "$failures" -eq 0 ]
