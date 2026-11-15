#!/usr/bin/env bash
# Tests of the two OpenEMR boot hooks, the part of this module that does real work. They cannot run
# on a real VM here, so three layers:
#
#   1. the hooks are rendered by Terraform itself (`terraform console`, no provider, no
#      credentials), so the scripts tested are byte for byte what edge-vm would store in metadata;
#   2. shellcheck at -S style on the rendered hooks, this file and the fakes; `php -l` on the helper;
#   3. behaviour: each hook runs in a sandbox with PATH set to fake commands only
#      (tests/fakes/fake-command linked as docker, curl, iptables, timeout, date, sleep, shred).
#      The PHP helper runs for real (the fake `docker run --rm --entrypoint php` calls the real php
#      on mapped paths), so the key, the JWKS and the certificate the tests inspect are real.
#
# Usage: infra/modules/openemr/tests/hooks.sh   (needs bash, coreutils, terraform, php, openssl and
# python3; shellcheck from PATH or $SHELLCHECK. Without shellcheck the lint is skipped with a
# warning, and without php or openssl the behaviour layer is skipped, except in CI (CI=true), where
# either missing fails, as in edge-vm's tests.)

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fake="$here/tests/fakes/fake-command"

failures=0
pass() { echo "ok:   $*"; }
fail() {
  echo "FAIL: $*"
  failures=$((failures + 1))
}
finish() {
  if [ "$failures" -gt 0 ]; then
    echo "$failures check(s) failed"
    exit 1
  fi
  echo "all checks passed"
  exit 0
}

TMP=$(mktemp -d)
SB=""
# shellcheck disable=SC2317 # called through the EXIT trap
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

# ---- 1. Render the hooks with Terraform ---------------------------------------------------------

# Synthetic values only. Google credentials are removed for the console call (this module has no
# provider, so nothing could use them anyway).
IP=10.10.0.10
SECRET_PREFIX="projects/example-project-123/secrets/pb"
TF_VARS=(
  -var "internal_ip=$IP"
  -var "dockerhub_mirror_url=us-central1-docker.pkg.dev/example-project-123/pulsebridge-dockerhub"
  -var "secret_ids={db_password=\"$SECRET_PREFIX-openemr-db-password\",admin_password=\"$SECRET_PREFIX-openemr-admin-password\",client_private_key=\"$SECRET_PREFIX-openemr-client-private-key\"}"
)
render() { # <directory> [extra -var ...]
  local dir=$1 name
  shift
  mkdir -p "$dir"
  for name in openemr openemr-oauth-client; do
    printf 'base64encode(local.hooks["%s"])\n' "$name" |
      env -u GOOGLE_APPLICATION_CREDENTIALS -u GOOGLE_CREDENTIALS -u GOOGLE_OAUTH_ACCESS_TOKEN \
        -u GOOGLE_IMPERSONATE_SERVICE_ACCOUNT -u GCP_SA_KEY_B64 -u CLOUDSDK_CORE_PROJECT \
        -u GOOGLE_CLOUD_PROJECT -u GOOGLE_PROJECT CLOUDSDK_CONFIG=/nonexistent GCE_METADATA_HOST=127.0.0.1:9 \
        terraform -chdir="$here" console "${TF_VARS[@]}" "$@" | tr -d '"\r\n' | base64 -d > "$dir/$name.sh"
    [ -s "$dir/$name.sh" ] || {
      echo "could not render hook $name" >&2
      exit 2
    }
  done
}
render "$TMP/default"
render "$TMP/no-block" -var "block_metadata_server=false"
pass "rendered both hooks with terraform console (default, and block_metadata_server = false)"

# ---- 2. Lint --------------------------------------------------------------------------------------

shellcheck_bin="${SHELLCHECK:-$(command -v shellcheck || true)}"
if [ -n "$shellcheck_bin" ]; then
  if "$shellcheck_bin" -S style "$TMP/default/openemr.sh" "$TMP/default/openemr-oauth-client.sh" \
    "$TMP/no-block/openemr.sh" "$here/tests/hooks.sh" "$here/tests/static_checks.sh" "$fake"; then
    pass "shellcheck -S style: rendered hooks, tests and fakes are clean ($("$shellcheck_bin" --version | sed -n 's/^version: //p'))"
  else
    fail "shellcheck reported problems"
  fi
elif [ "${CI:-}" = "true" ]; then
  fail "shellcheck is not installed (required in CI)"
else
  echo "WARN: shellcheck not found; set SHELLCHECK=/path/to/shellcheck to lint (CI requires it)"
fi
for f in "$TMP/default/openemr.sh" "$TMP/default/openemr-oauth-client.sh"; do
  if bash -n "$f"; then pass "bash -n: $(basename "$f") parses"; else fail "bash -n: $(basename "$f") does not parse"; fi
done

REAL_PHP=$(command -v php || true)
REAL_OPENSSL=$(command -v openssl || true)
if [ -z "$REAL_PHP" ] || [ -z "$REAL_OPENSSL" ] || ! command -v python3 > /dev/null; then
  if [ "${CI:-}" = "true" ]; then
    fail "php, openssl and python3 are required for the behaviour tests in CI"
  else
    echo "WARN: php, openssl or python3 not found; skipping the behaviour tests (CI requires them)"
  fi
  finish
fi
if "$REAL_PHP" -l "$here/scripts/client-tools.php" > /dev/null; then pass "php -l: client-tools.php parses"; else fail "php -l: client-tools.php"; fi

# ---- 3. Behaviour ---------------------------------------------------------------------------------

# Sentinels: made up at run time, with a fresh random part, so no credential-like literal is
# committed. Each must never appear in a log or in any command line.
ROOT_PW="synthetic-root-pw-sentinel-$RANDOM$RANDOM"
DB_PW="synthetic-db-pw-sentinel-$RANDOM$RANDOM"
ADMIN_PW="synthetic-admin-pw-sentinel-$RANDOM$RANDOM"
FAKE_TOKEN="synthetic-access-token-sentinel-$RANDOM$RANDOM"
FAKE_CLIENT_SECRET="synthetic-client-secret-sentinel-$RANDOM$RANDOM"
FAKE_REGISTRATION_TOKEN="synthetic-registration-token-sentinel-$RANDOM$RANDOM"
HOOK_TIMEOUT=2400 # EDGE_HOOK_TIMEOUT_SECONDS (40 minutes, this module's default)
TLS_TARGET=/etc/ssl/certs/selfsigned.cert.pem
REAL_DATE=$(command -v date)
HOOKS="$TMP/default"

new_sandbox() {
  SB=$(mktemp -d "$TMP/sb.XXXXXX")
  local c real d="$SB/mnt/disks/edge"
  mkdir -p "$SB/bin" "$SB/state/containers/mariadb" "$SB/run" "$d/secrets" "$d/openemr" "$d/mariadb" "$SB/var/lib/pulsebridge-edge"
  for c in docker curl iptables timeout date sleep shred; do
    ln -s "$fake" "$SB/bin/$c"
  done
  # The real tools the hooks need, and nothing else.
  for c in awk base64 basename bash cat chmod cmp cp cut find grep head ls mkdir mktemp mv rm rmdir sed sha256sum sort tail touch tr wc; do
    real=$(command -v "$c") || {
      echo "test setup: $c not found" >&2
      exit 2
    }
    ln -s "$real" "$SB/bin/$c"
  done
  echo "/dev/sdb $d ext4 rw,discard 0 0" > "$SB/state/proc_mounts"
  # Secret files as edge-vm writes them; with a trailing newline, which the hook must strip.
  printf '%s\n' "$ROOT_PW" > "$d/secrets/mariadb-root-password"
  printf '%s\n' "$DB_PW" > "$d/secrets/openemr-db-password"
  printf '%s\n' "$ADMIN_PW" > "$d/secrets/openemr-admin-password"
  echo running > "$SB/state/containers/mariadb/status"
  echo healthy > "$SB/state/containers/mariadb/health"
  : > "$SB/state/calls.log"
  echo 0 > "$SB/state/clock"
  mkdir -p "$SB/state/db"
  printf 'rest_fhir_api\t0\nrest_system_scopes_api\t0\nsite_addr_oath\t\nrest_api\t0\n' > "$SB/state/db/globals"
}

RC=0
run_hook() { # <hook name> [VAR=value ...]
  local name=$1 d="$SB/mnt/disks/edge"
  shift
  RC=0
  echo "0" > "$SB/state/clock"
  env -i PATH="$SB/bin" FAKE_STATE="$SB/state" REAL_DATE="$REAL_DATE" REAL_PHP="$REAL_PHP" \
    FAKE_DB_PASSWORD="$DB_PW" FAKE_TOKEN="$FAKE_TOKEN" FAKE_CLIENT_SECRET="$FAKE_CLIENT_SECRET" \
    FAKE_REGISTRATION_TOKEN="$FAKE_REGISTRATION_TOKEN" FAKE_TLS_TARGET="$TLS_TARGET" \
    HOME="$SB/var/lib/pulsebridge-edge" DOCKER_CONFIG="$SB/var/lib/pulsebridge-edge/.docker" \
    EDGE_DATA_DIR="$d" EDGE_SECRETS_DIR="$d/secrets" EDGE_MARIADB_DIR="$d/mariadb" \
    EDGE_OPENEMR_DIR="$d/openemr" EDGE_STATE_DIR="$SB/var/lib/pulsebridge-edge" \
    EDGE_DOCKER_NETWORK=pulsebridge-edge EDGE_MARIADB_CONTAINER=mariadb \
    EDGE_MIRROR_URL=us-central1-docker.pkg.dev/example-project-123/pulsebridge-dockerhub \
    EDGE_BOOT_WAIT_SECONDS=900 EDGE_HOOK_TIMEOUT_SECONDS="$HOOK_TIMEOUT" \
    OPENEMR_PROC_MOUNTS="$SB/state/proc_mounts" OPENEMR_RUN_DIR="$SB/run" OPENEMR_POLL_SECONDS=10 \
    "$@" bash "$HOOKS/$name.sh" >> "$SB/out.log" 2>&1 || RC=$?
}
boot() { # both hooks, in edge-vm's order
  run_hook openemr "$@"
  RC1=$RC
  run_hook openemr-oauth-client "$@"
  RC2=$RC
}

calls() { grep -cE -- "$1" "$SB/state/calls.log" || true; }
elapsed() { cat "$SB/state/clock"; }
db_rows() { cat "$SB/state/db/oauth_clients" 2> /dev/null || true; }
enabled_clients() { awk -F '\t' '$3 == 1 { print $1 }' "$SB/state/db/oauth_clients" 2> /dev/null || true; }
uploads() { find "$SB/state/sm" -maxdepth 1 -name 'v[0-9]*' ! -name '*.state' 2> /dev/null | wc -l; }
registrations() { find "$SB/state/registrations" -maxdepth 1 -name '*.json' 2> /dev/null | wc -l; }
state_value() { sed -n "s/^$1=//p" "$SB/mnt/disks/edge/openemr-client/state" 2> /dev/null || true; }

expect_rc() { # <want: number or nonzero> <rc> <description>
  if [ "$1" = nonzero ]; then
    if [ "$2" -ne 0 ]; then pass "$3: exits non-zero ($2)"; else fail "$3: expected a non-zero exit, got 0"; fi
  elif [ "$2" -eq "$1" ]; then pass "$3: exit $2"; else
    fail "$3: expected exit $1, got $2"
    sed 's/^/      | /' "$SB/out.log"
  fi
}
expect_eq() { # <got> <want> <description>
  if [ "$1" = "$2" ]; then pass "$3"; else fail "$3 (got '$1', want '$2')"; fi
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
expect_no_hang() {
  expect_calls '^HANG' 0 "$1: no call ran without a time limit"
  local t
  t=$(elapsed)
  if [ "$t" -le "$HOOK_TIMEOUT" ]; then pass "$1: ended within the hook limit (${t}s of fake time <= ${HOOK_TIMEOUT}s)"; else fail "$1: took ${t}s, over the hook limit ${HOOK_TIMEOUT}s"; fi
}
# The private key's body lines, from every bundle the fake Secret Manager received (test side).
key_lines() {
  local v
  for v in "$SB"/state/sm/v[0-9]*; do
    [ -e "$v" ] || continue
    case "$v" in *.state) continue ;; esac
    python3 -I -c 'import base64,json,sys
doc = json.loads(base64.b64decode(json.load(open(sys.argv[1]))["payload"]["data"]))
for line in doc["private_key_pem"].splitlines():
    if line and not line.startswith("-----"):
        print(line)' "$v"
  done
}
expect_no_leak() {
  local f found=0 pattern_file="$TMP/patterns"
  printf '%s\n' "$ROOT_PW" "$DB_PW" "$ADMIN_PW" "$FAKE_TOKEN" "$FAKE_CLIENT_SECRET" "$FAKE_REGISTRATION_TOKEN" > "$pattern_file"
  key_lines >> "$pattern_file"
  for f in "$SB/out.log" "$SB/state/calls.log" "$HOOKS/openemr.sh" "$HOOKS/openemr-oauth-client.sh"; do
    if grep -qF -f "$pattern_file" "$f"; then
      fail "$1: a password, token, client secret or private-key line appears in $(basename "$f")"
      found=1
    fi
  done
  [ "$found" -eq 1 ] || pass "$1: no password, token, client secret or private-key line in the log, in any command line or in the hooks"
}
# No file left anywhere in the sandbox (data disk, run directory, state) holds the client's private
# key, except the fake Secret Manager's store.
expect_key_removed() {
  local pattern_file="$TMP/keypatterns" hits
  key_lines > "$pattern_file"
  if [ ! -s "$pattern_file" ]; then
    fail "$1: no uploaded key to look for"
    return
  fi
  hits=$(grep -rlF -f "$pattern_file" "$SB" --exclude-dir=sm || true)
  if [ -z "$hits" ]; then pass "$1: no local copy of the private key is left (data disk, run directory, logs)"; else fail "$1: private key found in: $hits"; fi
  if [ -z "$(ls -A "$SB/run")" ]; then pass "$1: the run directory is empty"; else fail "$1: left in the run directory: $(ls -A "$SB/run")"; fi
}

openemr_args() { cat "$SB/state/containers/openemr/args" 2> /dev/null || true; }

# -- Case: first boot -------------------------------------------------------------------------------
echo "== first boot: start OpenEMR, register the client"
new_sandbox
boot
expect_rc 0 "$RC1" "hook openemr on first boot"
expect_rc 0 "$RC2" "hook openemr-oauth-client on first boot"
expect_calls '^docker pull --quiet us-central1-docker\.pkg\.dev/example-project-123/pulsebridge-dockerhub/openemr/openemr:8\.5\.0-2026-11-12@sha256:f9536d0bcad7956d21471e73b91690215ad00ee4aed9448770e5012df15318cd$' 1 "pulls the image once, through the mirror, by digest"
expect_calls '^timeout [0-9]+ docker pull ' 1 "the pull runs under timeout"
args=$(openemr_args)
expect_eq "$(printf '%s\n' "$args" | grep -cx -- '--publish' || true)" 1 "exactly one --publish"
expect_eq "$(printf '%s\n' "$args" | grep -A1 -x -- '--publish' | tail -n 1)" "443:443" "publishes HTTPS only: 443:443"
expect_eq "$(printf '%s\n' "$args" | grep -cE -- '(^|:)80(:|$)' || true)" 0 "port 80 is not published"
expect_eq "$(printf '%s\n' "$args" | grep -cxE -- '-v|--volume|-p' || true)" 0 "no -v, --volume or -p"
expect_eq "$(printf '%s\n' "$args" | grep -A1 -x -- '--mount' | grep -c '^type=bind,source=' || true)" 4 "four bind mounts with --mount type=bind (sites, Apache settings, certificate, key)"
expect_eq "$(printf '%s\n' "$args" | grep -c "^type=bind,source=$SB/mnt/disks/edge/openemr,target=/var/www/localhost/htdocs/openemr/sites$" || true)" 1 "sites directory from the data disk"
expect_eq "$(printf '%s\n' "$args" | grep -c "^type=bind,source=$SB/var/lib/pulsebridge-edge/openemr/apache-lowmem.conf,target=/etc/apache2/conf.d/zz-lowmem.conf,readonly$" || true)" 1 "Apache settings from the state directory, read-only"
expect_eq "$(printf '%s\n' "$args" | grep -A1 -x -- '--restart' | tail -n 1)" "unless-stopped" "--restart unless-stopped"
expect_eq "$(printf '%s\n' "$args" | grep -A1 -x -- '--stop-timeout' | tail -n 1)" "60" "a graceful stop timeout"
expect_eq "$(printf '%s\n' "$args" | grep -A1 -x -- '--network' | tail -n 1)" "pulsebridge-edge" "on the edge network, next to MariaDB"
for s in "OPENEMR_SETTING_rest_fhir_api=1" "OPENEMR_SETTING_rest_system_scopes_api=1" "OPENEMR_SETTING_site_addr_oath=https://$IP" "MYSQL_HOST=mariadb" "MYSQL_USER=openemr" "OE_USER=admin" MYSQL_ROOT_PASS MYSQL_PASS OE_PASS; do
  expect_eq "$(printf '%s\n' "$args" | grep -cx -- "$s" || true)" 1 "--env $s"
done
expect_eq "$(printf '%s\n' "$args" | grep -c 'rest_api' || true)" 0 "the standard rest_api is not set"
expect_eq "$(printf '%s\n' "$args" | grep -cE '^(MYSQL_ROOT_PASS|MYSQL_PASS|OE_PASS)=' || true)" 0 "passwords are passed by name only, never as name=value arguments"
expect_eq "$(cat "$SB/state/containers/openemr/env-values")" "$(printf 'MYSQL_ROOT_PASS=%s\nMYSQL_PASS=%s\nOE_PASS=%s' "$ROOT_PW" "$DB_PW" "$ADMIN_PW")" "the container receives the three passwords, trailing newlines stripped"
expect_calls '^iptables -w 10 -I DOCKER-USER -i br-0123456789ab -d 169\.254\.169\.254/32 -j DROP$' 1 "adds the metadata-server block for the edge bridge once"
if [ "$(grep -n '^iptables -w 10 -I' "$SB/state/calls.log" | head -n 1 | cut -d: -f1)" -lt "$(grep -n '^docker run --detach --name openemr' "$SB/state/calls.log" | cut -d: -f1)" ]; then
  pass "the metadata block is in place before OpenEMR is created"
else
  fail "OpenEMR was created before the metadata block"
fi
expect_eq "$(grep -c '^MaxRequestWorkers *4$' "$SB/var/lib/pulsebridge-edge/openemr/apache-lowmem.conf" || true)" 1 "Apache capped at four workers"
if [ -s "$SB/mnt/disks/edge/openemr/default/sqlconf.php" ]; then pass "sites directory seeded from the image"; else fail "sites directory not seeded"; fi
if [ ! -e "$SB/mnt/disks/edge/.openemr-sites-seed" ]; then pass "no seed directory left behind"; else fail "the seed directory was left behind"; fi
expect_eq "$("$REAL_OPENSSL" x509 -in "$SB/mnt/disks/edge/openemr-tls/server.crt" -noout -ext subjectAltName | tail -n 1 | tr -d ' ')" "IPAddress:$IP" "the certificate names the VM's address"
expect_calls '^curl .*--cacert '"$SB"'/mnt/disks/edge/openemr-tls/server\.crt .*https://10\.10\.0\.10/meta/health/readyz$' "$(calls '/meta/health/readyz$')" "every readiness poll verifies OpenEMR's certificate (--cacert)"
expect_calls 'INSECURE' 0 "TLS verification is never switched off"
expect_log 'OPENEMR_SETTING_\* took effect' "the settings check passes when the variables took effect"
expect_calls '^docker run --rm --network none .*client-tools\.php keygen' 1 "generates one key pair, in a container with no network"
expect_eq "$(registrations)" 1 "registers one client"
reg="$SB/state/registrations/1.json"
if python3 -I - "$reg" << 'PY'; then pass "registration body: private client, private_key_jwt, client_credentials only, S1's six scopes, a public-only JWKS whose kid is its RFC 7638 thumbprint"; else fail "registration body is not as S1's recipe"; fi
import base64, hashlib, json, sys
body = json.load(open(sys.argv[1]))
assert body["application_type"] == "private", body
assert body["token_endpoint_auth_method"] == "private_key_jwt", body
assert body["grant_types"] == ["client_credentials"], body
assert body["scope"].split() == ["system/Patient.read", "system/Patient.write", "system/Encounter.read",
                                 "system/Encounter.write", "system/Observation.read", "system/Observation.write"], body
assert body["client_name"] == "pulsebridge-fhir-proxy"
keys = body["jwks"]["keys"]
assert len(keys) == 1
k = keys[0]
assert set(k) == {"kty", "kid", "use", "alg", "n", "e"}, k  # no private members (d, p, q, dp, dq, qi)
assert k["kty"] == "RSA" and k["alg"] == "RS384" and k["use"] == "sig"
thumb = hashlib.sha256(('{"e":"%s","kty":"RSA","n":"%s"}' % (k["e"], k["n"])).encode()).digest()
assert base64.urlsafe_b64encode(thumb).rstrip(b"=").decode() == k["kid"]
PY
expect_eq "$(uploads)" 1 "stores the key in Secret Manager once"
client=$(enabled_clients)
expect_eq "$(enabled_clients | wc -l)" 1 "exactly one enabled client in OpenEMR"
expect_eq "$(state_value status)" "done" "state file: done"
expect_eq "$(state_value client_id)" "$client" "state file holds the enabled client's ID"
expect_eq "$(cat "$SB/mnt/disks/edge/openemr-client/client_id")" "$client" "client_id file holds the client ID (plain configuration)"
expect_eq "$(state_value secret_version)" "1" "state file records the version it stored"
# The bundle: the key matches the registered public key; the client ID and certificate are the
# ones OpenEMR and the VM have.
python3 -I - "$SB/state/sm/v1" "$TMP/bundle-key.pem" << 'PY' > "$TMP/bundle.txt"
import base64, json, sys
doc = json.loads(base64.b64decode(json.load(open(sys.argv[1]))["payload"]["data"]))
open(sys.argv[2], "w").write(doc["private_key_pem"])
for f in ("format", "client_id", "kid", "alg", "token_endpoint_auth_method", "site_addr_oath", "token_url", "fhir_base_url", "scope"):
    print(f"{f}={doc[f]}")
open(sys.argv[2] + ".crt", "w").write(doc["server_certificate_pem"])
PY
bundle() { sed -n "s/^$1=//p" "$TMP/bundle.txt"; }
expect_eq "$(bundle format)" "pulsebridge.openemr-client.v1" "bundle: format"
expect_eq "$(bundle client_id)" "$client" "bundle: the enabled client's ID"
expect_eq "$(bundle kid)" "$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["jwks"]["keys"][0]["kid"])' "$reg")" "bundle: the registered key ID"
expect_eq "$(bundle site_addr_oath)|$(bundle token_url)|$(bundle fhir_base_url)" "https://$IP|https://$IP/oauth2/default/token|https://$IP/apis/default/fhir" "bundle: site address, token URL (the assertion's aud) and FHIR base"
modulus=$("$REAL_OPENSSL" pkey -in "$TMP/bundle-key.pem" -noout -text 2> /dev/null | awk '/^modulus:/{on=1;next} /^publicExponent/{on=0} on' | tr -d ' :\n' | sed 's/^00//')
jwk_n=$(python3 -I -c 'import base64,json,sys; n=json.load(open(sys.argv[1]))["jwks"]["keys"][0]["n"]; print(base64.urlsafe_b64decode(n+"="*(-len(n)%4)).hex())' "$reg")
expect_eq "$modulus" "$jwk_n" "bundle: the private key belongs to the registered public key"
if cmp -s "$TMP/bundle-key.pem.crt" "$SB/mnt/disks/edge/openemr-tls/server.crt"; then pass "bundle: carries the certificate OpenEMR serves"; else fail "bundle: certificate differs from OpenEMR's"; fi
rm -f "$TMP/bundle-key.pem" "$TMP/bundle-key.pem.crt"
expect_no_leak "first boot"
expect_key_removed "first boot"
expect_no_hang "first boot"

# -- Case: second run changes nothing --------------------------------------------------------------
echo "== second run (same boot, or the startup script re-run by hand)"
: > "$SB/state/calls.log"
before_db=$(db_rows)
before_state=$(cat "$SB/mnt/disks/edge/openemr-client/state")
boot
expect_rc 0 "$RC1" "hook openemr, second run"
expect_rc 0 "$RC2" "hook openemr-oauth-client, second run"
expect_calls '^docker (run|start|stop|rm|pull) ' 0 "second run: no docker run, start, stop, rm or pull"
expect_calls '^iptables -w 10 -I ' 0 "second run: the metadata rule is not added again"
expect_eq "$(wc -l < "$SB/state/iptables.rules")" 1 "one metadata rule in DOCKER-USER"
expect_calls '/oauth2/default/registration' 0 "second run: no registration"
expect_calls ':addVersion' 0 "second run: no new key version"
expect_eq "$(db_rows)" "$before_db" "second run: OpenEMR's client table unchanged"
expect_eq "$(cat "$SB/mnt/disks/edge/openemr-client/state")" "$before_state" "second run: state file unchanged"
expect_log 'nothing to do' "second run says there is nothing to do"
expect_no_leak "second run"

# -- Case: reboot ---------------------------------------------------------------------------------
echo "== reboot: iptables emptied, OpenEMR not started by Docker (its bind mounts were missing)"
: > "$SB/state/iptables.rules"
echo exited > "$SB/state/containers/openemr/status"
: > "$SB/state/calls.log"
boot
expect_rc 0 "$RC1" "hook openemr after a reboot"
expect_rc 0 "$RC2" "hook openemr-oauth-client after a reboot"
expect_calls '^iptables -w 10 -I ' 1 "after a reboot the metadata rule is added again"
expect_calls '^docker start openemr$' 1 "after a reboot the existing container is started, not recreated"
expect_calls '^docker run --detach' 0 "after a reboot nothing is created"
if [ "$(grep -n '^iptables -w 10 -I' "$SB/state/calls.log" | cut -d: -f1)" -lt "$(grep -n '^docker start openemr' "$SB/state/calls.log" | cut -d: -f1)" ]; then
  pass "after a reboot the rule is in place before OpenEMR starts"
else
  fail "after a reboot OpenEMR started before the metadata rule"
fi
expect_eq "$(registrations)" 1 "after a reboot no new registration"

# -- Case: a changed certificate replaces the container gracefully and re-registers ------------------
echo "== certificate replaced (its files deleted): graceful container replacement, new client"
old_client=$client
rm -rf "$SB/mnt/disks/edge/openemr-tls"
: > "$SB/state/calls.log"
boot
expect_rc 0 "$RC1" "hook openemr with a new certificate"
expect_rc 0 "$RC2" "hook openemr-oauth-client with a new certificate"
expect_calls '^docker stop --time 60 openemr$' 1 "the old container is stopped gracefully"
expect_calls '^docker rm openemr$' 1 "then removed without --force"
expect_calls 'FORCED REMOVE' 0 "never docker rm --force"
expect_calls '^docker run --detach --name openemr' 1 "and created again"
expect_log 'certificate changed' "the registration hook notices the new certificate"
expect_eq "$(registrations)" 2 "a new client is registered"
expect_eq "$(uploads)" 2 "its key is stored as a new version"
new_client=$(enabled_clients)
expect_eq "$(enabled_clients | wc -l)" 1 "exactly one enabled client"
if [ "$new_client" != "$old_client" ]; then pass "the enabled client is the new one"; else fail "the old client is still the enabled one"; fi
expect_eq "$(awk -F '\t' -v c="$old_client" '$1 == c { print $3 }' "$SB/state/db/oauth_clients")" "0" "the earlier client is revoked (disabled, kept)"
expect_eq "$(state_value secret_version)" "2" "state file records version 2"
expect_key_removed "re-registration"
expect_no_leak "re-registration"

# -- Case: the database was wiped ----------------------------------------------------------------
echo "== OpenEMR's database recreated (client gone), marker still on the disk"
: > "$SB/state/db/oauth_clients"
boot
expect_rc 0 "$RC2" "registration after the database was wiped"
expect_log 'not in OpenEMR.s database' "says the client is missing from the database"
expect_eq "$(registrations)" 3 "registers again"
expect_eq "$(enabled_clients | wc -l)" 1 "one enabled client again"
expect_eq "$(enabled_clients)" "$(state_value client_id)" "the state file names the enabled client"

# -- Case: the secret has no usable version, marker still on the disk ------------------------------
echo "== the key secret's versions were destroyed"
for v in "$SB"/state/sm/v*.state; do echo DESTROYED > "$v"; done
boot
expect_rc 0 "$RC2" "registration after the secret lost its versions"
expect_log 'no usable latest version' "says the secret has no usable version"
expect_eq "$(registrations)" 4 "registers again (the old key cannot be uploaded again)"
expect_eq "$(state_value secret_version)" "$(uploads)" "the state file records the newest version"

# -- Case: Secret Manager metadata cannot be read ---------------------------------------------------
echo "== the versions/latest metadata read is refused (403)"
: > "$SB/state/calls.log"
run_hook openemr-oauth-client FAKE_SM_GET=403
expect_rc 0 "$RC" "registration hook with no viewer access"
expect_log 'cannot check the key secret' "warns that it cannot check"
expect_eq "$(registrations)" 4 "does not re-register on a guess"

# -- Case: the database cannot be queried during the check ----------------------------------------
echo "== the database query hangs while checking the registered client"
run_hook openemr-oauth-client "FAKE_HANG=docker exec"
expect_rc nonzero "$RC" "registration hook when the database does not answer"
expect_log 'cannot query OpenEMR.s database' "says it cannot query the database"
expect_eq "$(registrations)" 4 "does not register a new client because a query failed"
expect_no_hang "database hang during the check"

# -- Case: the settings variables have no effect (spike S2) ----------------------------------------
echo "== OPENEMR_SETTING_* ignored by the image; rest_api found on"
new_sandbox
printf 'rest_fhir_api\t0\nrest_system_scopes_api\t0\nsite_addr_oath\t\nrest_api\t1\n' > "$SB/state/db/globals"
run_hook openemr FAKE_SETTINGS_ENV=ignored
expect_rc 0 "$RC" "hook openemr when the variables are ignored"
expect_log 'did not take effect' "logs that the variables did not take effect"
expect_log 'applied through the database: .*rest_fhir_api' "logs what it wrote to the database"
expect_eq "$(sort "$SB/state/db/globals" | tr '\t' '=' | tr '\n' ' ')" "rest_api=0 rest_fhir_api=1 rest_system_scopes_api=1 site_addr_oath=https://$IP " "globals end as configured, rest_api off"
expect_log 'metadata 200' "the FHIR API answers afterwards"
: > "$SB/state/calls.log"
: > "$SB/out.log"
run_hook openemr FAKE_SETTINGS_ENV=ignored
expect_rc 0 "$RC" "second run with the variables ignored"
if grep -q 'did not take effect' "$SB/out.log"; then fail "the database write is repeated on the second run"; else pass "second run: the settings are already right, nothing is written"; fi

# -- Case: refuse to start OpenEMR ---------------------------------------------------------------------
for scenario in mariadb-unhealthy disk-missing secret-missing iptables-fails; do
  echo "== $scenario: OpenEMR must not start"
  new_sandbox
  extra=()
  case "$scenario" in
    mariadb-unhealthy) echo starting > "$SB/state/containers/mariadb/health" ;;
    disk-missing) : > "$SB/state/proc_mounts" ;;
    secret-missing) rm "$SB/mnt/disks/edge/secrets/openemr-admin-password" ;;
    iptables-fails) extra=(FAKE_IPTABLES_FAIL=1) ;;
  esac
  boot "${extra[@]}"
  expect_rc nonzero "$RC1" "$scenario: hook openemr"
  expect_calls '^docker run --detach --name openemr' 0 "$scenario: OpenEMR is never created"
  expect_calls '^docker start openemr' 0 "$scenario: OpenEMR is never started"
  expect_rc nonzero "$RC2" "$scenario: hook openemr-oauth-client"
  expect_eq "$(registrations)" 0 "$scenario: no client is registered"
  case "$scenario" in
    mariadb-unhealthy) expect_log 'MariaDB \(mariadb\) is not healthy' "names the unhealthy database" ;;
    disk-missing) expect_log 'data disk is not mounted' "names the missing disk" ;;
    secret-missing) expect_log 'secret files missing .*openemr-admin-password' "names the missing secret file" ;;
    iptables-fails) expect_log 'could not add the iptables rule' "names the failed rule" ;;
  esac
  if [ "$scenario" != iptables-fails ]; then expect_calls '^timeout [0-9]+ docker pull' 0 "$scenario: nothing is pulled"; fi
  expect_no_leak "$scenario"
done

# -- Case: block_metadata_server = false ----------------------------------------------------------
echo "== block_metadata_server = false"
new_sandbox
HOOKS="$TMP/no-block"
run_hook openemr
HOOKS="$TMP/default"
expect_rc 0 "$RC" "hook openemr without the metadata block"
expect_calls '^iptables' 0 "no iptables call when the block is off"
expect_log 'block_metadata_server is off' "warns that the metadata server is reachable"

# -- Case: time limits -----------------------------------------------------------------------------
for scenario in never-ready curl-hangs pull-hangs sql-hangs wrong-certificate; do
  echo "== $scenario: bounded by the hook deadline"
  new_sandbox
  case "$scenario" in
    never-ready) extra=(FAKE_NEVER_READY=1) ;;
    curl-hangs) extra=(FAKE_CURL_HANG=1) ;;
    pull-hangs) extra=("FAKE_HANG=docker pull") ;;
    sql-hangs) extra=("FAKE_HANG=docker exec") ;;
    wrong-certificate) extra=(FAKE_TLS_WRONG_CERT=1) ;;
  esac
  run_hook openemr "${extra[@]}"
  expect_rc nonzero "$RC" "$scenario: hook openemr"
  expect_no_hang "$scenario"
  case "$scenario" in
    never-ready | curl-hangs) expect_log 'did not become ready by the hook deadline' "$scenario: says OpenEMR did not become ready" ;;
    pull-hangs)
      expect_log 'could not pull' "pull-hangs: says the pull did not finish"
      expect_calls '^docker run --detach' 0 "pull-hangs: nothing is started"
      ;;
    sql-hangs)
      # The hanging fake logs HANG when called without `timeout` (checked above); here: it was
      # reached, through timeout.
      if [ "$(calls '^timeout 60 docker exec')" -ge 1 ]; then pass "sql-hangs: the database call ran under timeout 60"; else fail "sql-hangs: no database call under timeout"; fi
      ;;
    wrong-certificate)
      expect_log 'TLS verification of OpenEMR failed' "wrong-certificate: names the TLS failure"
      expect_eq "$(grep -c 'TLS verification of OpenEMR failed' "$SB/out.log")" 1 "wrong-certificate: reported once, not on every poll"
      expect_calls 'INSECURE' 0 "wrong-certificate: no fallback to --insecure"
      ;;
  esac
done

echo "== readiness after a few polls"
new_sandbox
run_hook openemr FAKE_READY_AFTER=5
expect_rc 0 "$RC" "hook openemr when OpenEMR needs five polls"
expect_log 'readyz 200 after 6 polls' "polls until ready"

# -- Case: the key upload fails ------------------------------------------------------------------------
for code in 403 503; do
  echo "== key upload answers $code"
  new_sandbox
  run_hook openemr
  run_hook openemr-oauth-client "FAKE_UPLOAD_FAIL=$code"
  expect_rc nonzero "$RC" "upload $code: hook openemr-oauth-client"
  expect_eq "$(enabled_clients | wc -l)" 0 "upload $code: no enabled client without a stored key"
  expect_eq "$(db_rows | wc -l)" 0 "upload $code: the new, disabled client is deleted"
  if [ ! -e "$SB/mnt/disks/edge/openemr-client/state" ]; then pass "upload $code: no state file"; else fail "upload $code: a state file was left"; fi
  if [ -z "$(ls -A "$SB/run")" ]; then pass "upload $code: the run directory is empty"; else fail "upload $code: left in the run directory: $(ls -A "$SB/run")"; fi
  expect_no_hang "upload $code"
  expect_no_leak "upload $code"
  if [ "$code" = 503 ]; then
    expect_log 'before the hook deadline' "upload 503: retried until the deadline"
  else
    expect_calls ':addVersion' 1 "upload 403: a refusal is not retried"
  fi
  run_hook openemr-oauth-client
  expect_rc 0 "$RC" "upload $code, next boot: registration succeeds"
  expect_eq "$(enabled_clients | wc -l)" 1 "upload $code, next boot: one enabled client"
  expect_eq "$(uploads)" 1 "upload $code, next boot: one stored key"
  expect_key_removed "upload $code, next boot"
done

# -- Case: registration without OpenEMR running --------------------------------------------------------
echo "== registration hook with OpenEMR not running"
new_sandbox
run_hook openemr-oauth-client
expect_rc nonzero "$RC" "registration hook without OpenEMR"
expect_eq "$(registrations)" 0 "no registration attempt"
expect_calls 'keygen' 0 "no key is generated"

# -- Case: not run by edge-vm --------------------------------------------------------------------------
echo "== hook run without the edge-vm environment"
new_sandbox
RC=0
env -i PATH="$SB/bin" FAKE_STATE="$SB/state" REAL_DATE="$REAL_DATE" bash "$HOOKS/openemr.sh" > "$SB/out.log" 2>&1 || RC=$?
expect_rc nonzero "$RC" "hook without EDGE_* variables"
expect_log 'EDGE_DATA_DIR is not set' "names the missing variable"
expect_calls '^docker' 0 "does nothing else"

finish
