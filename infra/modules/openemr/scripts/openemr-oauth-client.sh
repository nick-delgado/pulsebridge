# shellcheck shell=bash
# shellcheck disable=SC2154 # the OPENEMR_* configuration is assigned in part 1 (the rendered template)
#
# Part 4 of hook "openemr-oauth-client": register fhir-proxy's OAuth2 client with OpenEMR, following
# spike S1's recipe, on every boot, safely repeated.
#
# What a registered client is here: OpenEMR's OAuth2 server knows a client by an ID it generates at
# registration. The client proves who it is with a JWT signed by its private key
# (`private_key_jwt`), checked against the public key (JWKS) registered with it, and gets
# `system/...` tokens through the client credentials grant: no user, no password.
#
# Every boot: if the state file on the data disk says a client was registered AND that client is
# still enabled in OpenEMR's database AND OpenEMR's certificate is the one stored with it AND
# Secret Manager's latest version of the key secret is the one this hook stored, nothing is done.
# Otherwise a new client is registered:
#
#   1. generate an RSA key pair on the VM, in a memory-backed directory, in a throwaway container;
#   2. register the client (POST /oauth2/default/registration) with the public JWKS and the six
#      system/ scopes; OpenEMR stores it disabled and answers with its client_id;
#   3. add a Secret Manager version holding the private key, the client ID and OpenEMR's
#      certificate (one document, so fhir-proxy always gets a matching set), as the VM's account,
#      which may add versions but not read them;
#   4. only then enable the client in the database (S1: a new client is disabled until an
#      administrator enables it), revoke earlier PulseBridge clients, and record the state;
#   5. remove every local copy of the private key.
#
# If step 3 fails, the new client is deleted while it is still disabled, so there is never an
# enabled client whose key is not stored; the next boot registers again from scratch.

OPENEMR_CLIENT_STATE=""
WORK_DIR=""

cleanup() {
  if [ -n "$WORK_DIR" ] && [ -d "$WORK_DIR" ]; then
    remove_sensitive "$WORK_DIR"/private.pem "$WORK_DIR"/add-version.json "$WORK_DIR"/*.tmp
    rm -rf "$WORK_DIR"
  fi
}
trap cleanup EXIT
trap 'exit 143' TERM # edge-vm's `timeout` sends TERM; exit so the EXIT trap removes the key

check_prerequisites() {
  require_data_disk
  require_secret_files "$OPENEMR_DB_SECRET"
  [ -s "$OPENEMR_TLS_CERT" ] || die "OpenEMR's TLS certificate $OPENEMR_TLS_CERT is missing: hook openemr has not completed"
  local status
  status=$(container_status "$OPENEMR_CONTAINER")
  [ "$status" = "running" ] || die "container $OPENEMR_CONTAINER is $status: hook openemr has not started it, so no client can be registered"
}

# Read key=value lines of the state file into STATE_<key> variables. Only known keys with
# well-formed values are accepted.
load_state() {
  local key value
  STATE_status="" STATE_client_id="" STATE_secret_version="" STATE_certificate_sha256="" STATE_kid=""
  [ -f "$OPENEMR_CLIENT_STATE" ] || return 0
  while IFS='=' read -r key value; do
    case "$key" in
      status) [[ "$value" =~ ^(registered|done)$ ]] && STATE_status=$value ;;
      client_id) [[ "$value" =~ ^[A-Za-z0-9_-]{16,128}$ ]] && STATE_client_id=$value ;;
      secret_version) [[ "$value" =~ ^[0-9]{1,10}$ ]] && STATE_secret_version=$value ;;
      certificate_sha256) [[ "$value" =~ ^[0-9a-f]{64}$ ]] && STATE_certificate_sha256=$value ;;
      kid) [[ "$value" =~ ^[A-Za-z0-9_-]{43}$ ]] && STATE_kid=$value ;;
    esac
  done < "$OPENEMR_CLIENT_STATE"
  return 0
}

# Write the state file atomically. It holds no secret: the client ID is plain configuration.
save_state() { # <status> <client_id> <secret_version> <certificate_sha256> <kid>
  {
    printf 'status=%s\n' "$1"
    printf 'client_id=%s\n' "$2"
    printf 'secret_version=%s\n' "$3"
    printf 'certificate_sha256=%s\n' "$4"
    printf 'kid=%s\n' "$5"
  } > "$OPENEMR_CLIENT_STATE.tmp"
  mv "$OPENEMR_CLIENT_STATE.tmp" "$OPENEMR_CLIENT_STATE"
  printf '%s\n' "$2" > "$OPENEMR_CLIENT_DIR/client_id"
}

# is_enabled of a client in OpenEMR's database: 1, 0, or empty when there is no such client.
# Returns non-zero when the query itself failed, which callers must not read as "no such client".
client_enabled() {
  local out
  out=$(printf 'SELECT is_enabled FROM oauth_clients WHERE client_id = %s;\n' "$(sql_literal "$1")" | openemr_sql) || return 1
  printf '%s' "${out%%$'\n'*}"
}

# Secret Manager's latest version of the key secret, metadata only (roles/secretmanager.viewer;
# the VM cannot read the value). Prints the version number. Returns 0 when it is ENABLED, 2 when
# there is definitely no usable latest version (none, or disabled/destroyed), 1 when it cannot
# tell (no token, no permission, timeout).
latest_secret_version() {
  local out="$OPENEMR_CLIENT_DIR/latest.response" code number
  code=$(secretmanager_call "$out" "$OPENEMR_SECRETMANAGER_URL/$OPENEMR_CLIENT_KEY_SECRET/versions/latest") || true
  case "$code" in
    200)
      number=$(version_number "$out")
      if grep -qE '"state"[[:space:]]*:[[:space:]]*"ENABLED"' "$out" && [ -n "$number" ]; then
        rm -f "$out"
        echo "$number"
        return 0
      fi
      rm -f "$out"
      echo "${number:-none}"
      return 2
      ;;
    404)
      rm -f "$out"
      echo "none"
      return 2
      ;;
    *)
      rm -f "$out"
      echo "$code"
      return 1
      ;;
  esac
}

# True when the client in the state file is still usable by fhir-proxy; logs why not otherwise.
registration_is_current() {
  local enabled cert_sha latest rc=0
  load_state
  if [ "$STATE_status" != "done" ] || [ -z "$STATE_client_id" ] || [ -z "$STATE_secret_version" ]; then
    log "no completed client registration on the data disk: registering a client"
    return 1
  fi
  enabled=$(client_enabled "$STATE_client_id") || die "cannot query OpenEMR's database for client $STATE_client_id; not registering a new client on a guess"
  case "$enabled" in
    1) ;;
    "")
      log "client $STATE_client_id is not in OpenEMR's database (was the database recreated?): registering a new client"
      return 1
      ;;
    *)
      log "client $STATE_client_id is disabled in OpenEMR (revoked?): registering a new client"
      return 1
      ;;
  esac
  cert_sha=$(sha256sum "$OPENEMR_TLS_CERT" | awk '{ print $1 }')
  if [ "$cert_sha" != "$STATE_certificate_sha256" ]; then
    log "OpenEMR's TLS certificate changed since client $STATE_client_id was stored: registering a new client so fhir-proxy gets the new certificate with it"
    return 1
  fi
  latest=$(latest_secret_version) || rc=$?
  case "$rc" in
    0)
      if [ "$latest" != "$STATE_secret_version" ]; then
        log "the key secret's latest version is $latest, not $STATE_secret_version which holds client $STATE_client_id's key: registering a new client"
        return 1
      fi
      ;;
    2)
      log "the key secret has no usable latest version ($latest), and the key cannot be uploaded again (only its holder could, and it was removed): registering a new client"
      return 1
      ;;
    *)
      warn "cannot check the key secret's latest version (result $latest; the VM needs roles/secretmanager.viewer on it, identity edge_vm_writes_openemr_key): trusting the state file"
      ;;
  esac
  log "client $STATE_client_id is registered, enabled, and its key is version $STATE_secret_version of the secret; nothing to do"
  return 0
}

# POST the registration. Prints the new client_id. The response also holds a client_secret and a
# registration_access_token that this flow never uses (S1); it is read from a mode-600 file in the
# work directory and deleted, never logged.
register() {
  local out="$WORK_DIR/registration.response" code rc=0 client_id
  code=$(openemr_request /oauth2/default/registration "$out" \
    --request POST --header 'Content-Type: application/json' \
    --data-binary "@$WORK_DIR/registration.json") || rc=$?
  if [ "$code" != "200" ] && [ "$code" != "201" ]; then
    remove_sensitive "$out"
    error "client registration failed (HTTP $code, curl exit $rc)"
    return 1
  fi
  client_id=$(sed -n 's/.*"client_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$out" | head -n 1)
  remove_sensitive "$out"
  if [[ ! "$client_id" =~ ^[A-Za-z0-9_-]{16,128}$ ]]; then
    error "the registration response has no client_id in the expected form"
    return 1
  fi
  printf '%s' "$client_id"
}

# Add a Secret Manager version from add-version.json, retrying until the deadline. A retry after a
# lost response may add a second version with the same content, which is harmless: fhir-proxy
# reads the latest, and they are equal. Prints the version number.
upload_key() {
  local out="$WORK_DIR/add-version.response" code number attempt=0
  while :; do
    attempt=$((attempt + 1))
    code=$(secretmanager_call "$out" "$OPENEMR_SECRETMANAGER_URL/$OPENEMR_CLIENT_KEY_SECRET:addVersion" \
      --request POST --header 'Content-Type: application/json' \
      --data-binary "@$WORK_DIR/add-version.json") || code="no-token"
    if [ "$code" = "200" ]; then
      number=$(version_number "$out")
      rm -f "$out"
      if [ -n "$number" ]; then
        printf '%s' "$number"
        return 0
      fi
      error "Secret Manager accepted the key but its answer names no version"
      return 1
    fi
    rm -f "$out"
    case "$code" in
      400 | 401 | 403 | 404)
        error "Secret Manager refused the new key version (HTTP $code; 403 = the VM's account may not add versions: identity edge_vm_writes_openemr_key; 404 = no such secret)"
        return 1
        ;;
    esac
    if [ "$(remaining)" -le 0 ]; then
      error "could not store the key in Secret Manager before the hook deadline ($attempt tries, last result $code)"
      return 1
    fi
    warn "storing the key in Secret Manager failed (result $code, try $attempt); retrying"
    pause
  done
}

register_client() {
  local client_id kid version cert_sha
  WORK_DIR=$(mktemp -d "$OPENEMR_RUN_DIR/pulsebridge-openemr-client.XXXXXX")
  chmod 700 "$WORK_DIR"

  # A registration that stopped half-way on an earlier boot left a disabled client; remove it.
  if [ "$STATE_status" = "registered" ] && [ -n "$STATE_client_id" ]; then
    printf 'DELETE FROM oauth_clients WHERE client_id = %s AND is_enabled = 0;\n' "$(sql_literal "$STATE_client_id")" | openemr_sql > /dev/null
    log "removed the unfinished client $STATE_client_id from an earlier boot"
  fi

  run_client_tools "$WORK_DIR" -- keygen /work
  if [ ! -s "$WORK_DIR/private.pem" ] || [ ! -s "$WORK_DIR/jwks.json" ]; then
    die "the key tool produced no key"
  fi
  kid=$(cat "$WORK_DIR/kid")
  run_client_tools "$WORK_DIR" -- register-body /work "$OPENEMR_CLIENT_NAME" \
    "$OPENEMR_SITE_ADDRESS/pulsebridge-unused-redirect" "$OPENEMR_CLIENT_SCOPES"

  client_id=$(register) || die "OpenEMR did not register the client"
  cert_sha=$(sha256sum "$OPENEMR_TLS_CERT" | awk '{ print $1 }')
  save_state registered "$client_id" "" "$cert_sha" "$kid"
  log "registered client $client_id (key ID $kid, scopes: $OPENEMR_CLIENT_SCOPES); OpenEMR keeps it disabled until it is enabled below"

  run_client_tools "$WORK_DIR" --mount "type=bind,source=$OPENEMR_TLS_CERT,target=/certs/server.crt,readonly" -- \
    bundle /work "$client_id" "$OPENEMR_SITE_ADDRESS" "$OPENEMR_CLIENT_SCOPES" /certs/server.crt
  remove_sensitive "$WORK_DIR/private.pem"

  if ! version=$(upload_key); then
    printf 'DELETE FROM oauth_clients WHERE client_id = %s AND is_enabled = 0;\n' "$(sql_literal "$client_id")" | openemr_sql > /dev/null
    rm -f "$OPENEMR_CLIENT_STATE"
    die "the key was not stored, so client $client_id was deleted while still disabled; the next boot registers again"
  fi
  remove_sensitive "$WORK_DIR/add-version.json"
  log "stored the client's key, ID and OpenEMR's certificate as version $version of $OPENEMR_CLIENT_KEY_SECRET"

  # S1 recipe step 6: enable the client. Then revoke (disable, not delete) every earlier client of
  # the same name, so exactly one PulseBridge client can get tokens.
  printf 'UPDATE oauth_clients SET is_enabled = 1 WHERE client_id = %s;\n' "$(sql_literal "$client_id")" | openemr_sql > /dev/null
  [ "$(client_enabled "$client_id")" = "1" ] || die "client $client_id is not enabled after the database update (it stays disabled; the next boot registers again)"
  printf 'UPDATE oauth_clients SET is_enabled = 0 WHERE client_name = %s AND client_id <> %s;\n' \
    "$(sql_literal "$OPENEMR_CLIENT_NAME")" "$(sql_literal "$client_id")" | openemr_sql > /dev/null
  save_state "done" "$client_id" "$version" "$cert_sha" "$kid"
  log "client $client_id enabled; earlier $OPENEMR_CLIENT_NAME clients revoked. fhir-proxy reads it from the latest version of $OPENEMR_CLIENT_KEY_SECRET"
}

main() {
  init_hook
  check_prerequisites
  mkdir -p "$OPENEMR_CLIENT_DIR"
  chmod 700 "$OPENEMR_CLIENT_DIR"
  OPENEMR_CLIENT_STATE="$OPENEMR_CLIENT_DIR/state"
  wait_until_ready
  if registration_is_current; then
    return 0
  fi
  register_client
  log "done"
}

main "$@"
