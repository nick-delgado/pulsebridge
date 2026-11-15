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
# A registration moves through four states, recorded in a state file on the data disk (no secret
# in it), so a boot that stops part-way is resumed, not redone:
#
#   registered  the client exists in OpenEMR, disabled; its key exists only in memory on this boot
#   uploaded    the key, client ID and certificate are stored as Secret Manager version <n>
#   enabled     the client is enabled in OpenEMR's database
#   done        earlier PulseBridge clients are revoked; nothing left to do
#
# Each boot first decides what to do (plan_action):
#   - done, and the client is still enabled, the certificate and scopes are unchanged and Secret
#     Manager's latest version is the recorded one: nothing;
#   - uploaded / enabled, with the same checks: finish (enable, then revoke), no new client and no
#     new version;
#   - anything else (no state, registered, database wiped, version gone, certificate or scopes
#     changed): register a new client.
#
# Registering:
#   1. generate an RSA key pair on the VM, in a memory-backed directory, in a throwaway container;
#   2. register the client (POST /oauth2/default/registration) with the public JWKS and the
#      configured system/ scopes; OpenEMR stores it disabled and answers with its client_id;
#   3. add ONE Secret Manager version holding the private key, the client ID and OpenEMR's
#      certificate, as the VM's account (which may add versions but not read them); at most three
#      attempts, and an attempt whose answer was lost is checked against the version metadata
#      before another is made;
#   4. only then enable the client, then revoke earlier PulseBridge clients;
#   5. remove every local copy of the private key.
#
# If step 3 fails, the new client is deleted while it is still disabled, so there is never an
# enabled client whose key is not stored; the next boot registers again from scratch.

OPENEMR_CLIENT_STATE=""
WORK_DIR=""
ACTION=""
OPENEMR_UPLOAD_ATTEMPTS=3

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
  STATE_status="" STATE_client_id="" STATE_secret_version="" STATE_certificate_sha256="" STATE_kid="" STATE_scopes=""
  [ -f "$OPENEMR_CLIENT_STATE" ] || return 0
  while IFS='=' read -r key value; do
    case "$key" in
      status) [[ "$value" =~ ^(registered|uploaded|enabled|done)$ ]] && STATE_status=$value ;;
      client_id) [[ "$value" =~ ^[A-Za-z0-9_-]{16,128}$ ]] && STATE_client_id=$value ;;
      secret_version) [[ "$value" =~ ^[0-9]{1,10}$ ]] && STATE_secret_version=$value ;;
      certificate_sha256) [[ "$value" =~ ^[0-9a-f]{64}$ ]] && STATE_certificate_sha256=$value ;;
      kid) [[ "$value" =~ ^[A-Za-z0-9_-]{43}$ ]] && STATE_kid=$value ;;
      scopes) [[ "$value" =~ ^[A-Za-z0-9/.\ ]{1,512}$ ]] && STATE_scopes=$value ;;
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
    printf 'scopes=%s\n' "$OPENEMR_CLIENT_SCOPES"
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
# the VM cannot read the value). Prints the version number ("none" when there is none). Returns 0
# when it is ENABLED, 2 when there is definitely no usable latest version (none, or
# disabled/destroyed), 1 when it cannot tell (no token, no permission, timeout).
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

# Decide what this boot does, into ACTION: nothing, enable, revoke or register. Logs why.
plan_action() {
  local enabled cert_sha latest rc=0
  ACTION=register
  load_state
  if [ -z "$STATE_status" ] || [ "$STATE_status" = "registered" ] || [ -z "$STATE_client_id" ] || [ -z "$STATE_secret_version" ]; then
    log "no stored client registration on the data disk: registering a client"
    return 0
  fi
  enabled=$(client_enabled "$STATE_client_id") || die "cannot query OpenEMR's database for client $STATE_client_id; not registering a new client on a guess"
  if [ -z "$enabled" ]; then
    log "client $STATE_client_id is not in OpenEMR's database (was the database recreated?): registering a new client"
    return 0
  fi
  cert_sha=$(sha256sum "$OPENEMR_TLS_CERT" | awk '{ print $1 }')
  if [ "$cert_sha" != "$STATE_certificate_sha256" ]; then
    log "OpenEMR's TLS certificate changed since client $STATE_client_id was stored: registering a new client so fhir-proxy gets the new certificate with it"
    return 0
  fi
  if [ "$STATE_scopes" != "$OPENEMR_CLIENT_SCOPES" ]; then
    log "client $STATE_client_id was registered with scopes '$STATE_scopes', not the configured '$OPENEMR_CLIENT_SCOPES': registering a new client"
    return 0
  fi
  latest=$(latest_secret_version) || rc=$?
  case "$rc" in
    0)
      if [ "$latest" != "$STATE_secret_version" ]; then
        log "the key secret's latest version is $latest, not $STATE_secret_version which holds client $STATE_client_id's key: registering a new client"
        return 0
      fi
      ;;
    2)
      log "the key secret has no usable latest version ($latest), and the key cannot be uploaded again (only its holder could, and it was removed): registering a new client"
      return 0
      ;;
    *)
      warn "cannot check the key secret's latest version (result $latest; the VM needs roles/secretmanager.viewer on it, identity edge_vm_writes_openemr_key): trusting the state file"
      ;;
  esac
  case "$STATE_status" in
    done)
      if [ "$enabled" = "1" ]; then
        ACTION=nothing
        log "client $STATE_client_id is registered, enabled, and its key is version $STATE_secret_version of the secret; nothing to do"
      else
        log "client $STATE_client_id is disabled in OpenEMR (revoked?): registering a new client"
      fi
      ;;
    uploaded)
      ACTION=enable
      log "resuming: client $STATE_client_id's key is stored as version $STATE_secret_version; enabling it (no new client, no new version)"
      ;;
    enabled)
      if [ "$enabled" = "1" ]; then
        ACTION=revoke
        log "resuming: client $STATE_client_id is enabled; revoking earlier clients"
      else
        log "client $STATE_client_id is disabled in OpenEMR (revoked?): registering a new client"
      fi
      ;;
  esac
  return 0
}

# POST the registration. Prints the new client_id. The response also holds a client_secret and a
# registration_access_token that this flow never uses (S1); it is read from a mode-600 file in the
# work directory and deleted, never logged. A client_id that is not in the expected form is
# refused before it reaches any SQL statement or file.
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
    error "the registration response has no client_id in the expected form (16-128 of A-Z a-z 0-9 _ -); refusing it. OpenEMR keeps that client disabled; it can be deleted by hand."
    return 1
  fi
  printf '%s' "$client_id"
}

# The latest version number for comparing before and after an upload (0 when there is none), or
# "unknown" when the metadata cannot be read.
version_marker() {
  local latest rc=0
  latest=$(latest_secret_version) || rc=$?
  if [ "$rc" -eq 1 ]; then
    echo "unknown"
  elif [[ "$latest" =~ ^[0-9]+$ ]]; then
    echo "$latest"
  else
    echo 0
  fi
}

# Add the Secret Manager version from add-version.json. Prints the version number.
#
# addVersion is not idempotent: every successful call adds a version. So at most
# OPENEMR_UPLOAD_ATTEMPTS calls are made, and after a call whose outcome is unknown (no answer, a
# timeout, a 5xx or 429), the version metadata decides whether it landed before another call is
# made. Secret Manager numbers versions 1, 2, 3, ... in the order they are added, and only this
# hook adds versions to this secret (edge-vm's lock keeps one run at a time; T27 never writes it),
# so "the latest version number is higher than before the first attempt" means the upload landed,
# and the latest version is ours. This needs metadata only (roles/secretmanager.viewer), unlike
# reading the payload back, and no clock: comparing createTime with the attempt's start would
# depend on the VM's clock agreeing with Google's. When the metadata cannot be read, nothing is
# retried, because a retry could add a second version.
upload_key() { # <version number before the first attempt, or "unknown">
  local before=$1 out="$WORK_DIR/add-version.response" code number attempt=0 after
  while [ "$attempt" -lt "$OPENEMR_UPLOAD_ATTEMPTS" ]; do
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
      400 | 401 | 403 | 404 | 409)
        error "Secret Manager refused the new key version (HTTP $code; 403 = the VM's account may not add versions: identity edge_vm_writes_openemr_key; 404 = no such secret)"
        return 1
        ;;
      no-token)
        # Nothing was sent: safe to try again.
        warn "no access token for Secret Manager (try $attempt of $OPENEMR_UPLOAD_ATTEMPTS)"
        ;;
      *)
        if [ "$before" = "unknown" ]; then
          error "storing the key gave no clear answer (result $code) and the version metadata cannot be read, so whether it landed is unknown; not retrying, because a retry could add a second version"
          return 1
        fi
        after=$(version_marker)
        if [ "$after" = "unknown" ]; then
          error "storing the key gave no clear answer (result $code) and the version metadata cannot be read now; not retrying"
          return 1
        fi
        if [ "$after" -gt "$before" ]; then
          log "storing the key gave no clear answer (result $code), but the latest version is now $after (it was $before): the upload landed"
          printf '%s' "$after"
          return 0
        fi
        warn "storing the key failed (result $code, try $attempt of $OPENEMR_UPLOAD_ATTEMPTS); the latest version is still $before, so it did not land"
        ;;
    esac
    if [ "$(remaining)" -le 0 ]; then
      break
    fi
    pause
  done
  error "could not store the key in Secret Manager ($attempt tries, last result $code)"
  return 1
}

# Enable the client (S1 recipe step 6), then revoke the others.
finish_enable() { # <client_id> <version> <certificate_sha256> <kid>
  printf 'UPDATE oauth_clients SET is_enabled = 1 WHERE client_id = %s;\n' "$(sql_literal "$1")" | openemr_sql > /dev/null ||
    die "could not enable client $1; its key is stored as version $2, so the next boot only enables it"
  [ "$(client_enabled "$1")" = "1" ] || die "client $1 is not enabled after the database update; the next boot tries again"
  save_state enabled "$1" "$2" "$3" "$4"
  log "client $1 enabled"
  finish_revoke "$@"
}

# Revoke (disable, not delete) every earlier client of the same name, so exactly one PulseBridge
# client can get tokens. Clients of other names are never touched.
finish_revoke() { # <client_id> <version> <certificate_sha256> <kid>
  printf 'UPDATE oauth_clients SET is_enabled = 0 WHERE client_name = %s AND client_id <> %s;\n' \
    "$(sql_literal "$OPENEMR_CLIENT_NAME")" "$(sql_literal "$1")" | openemr_sql > /dev/null ||
    die "could not revoke earlier $OPENEMR_CLIENT_NAME clients; the next boot retries only the revoke"
  save_state "done" "$1" "$2" "$3" "$4"
  log "earlier $OPENEMR_CLIENT_NAME clients revoked. fhir-proxy reads client $1 from version $2 of $OPENEMR_CLIENT_KEY_SECRET"
}

register_client() {
  local client_id kid version cert_sha before
  WORK_DIR=$(mktemp -d "$OPENEMR_RUN_DIR/pulsebridge-openemr-client.XXXXXX")
  chmod 700 "$WORK_DIR"

  # A registration that stopped before its client was enabled left a disabled client; remove it.
  if [ -n "$STATE_client_id" ] && [ "$STATE_status" != "done" ]; then
    printf 'DELETE FROM oauth_clients WHERE client_id = %s AND is_enabled = 0;\n' "$(sql_literal "$STATE_client_id")" | openemr_sql > /dev/null
    log "removed the unfinished client $STATE_client_id from an earlier boot (if it was still disabled)"
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

  before=$(version_marker)
  if ! version=$(upload_key "$before"); then
    printf 'DELETE FROM oauth_clients WHERE client_id = %s AND is_enabled = 0;\n' "$(sql_literal "$client_id")" | openemr_sql > /dev/null
    rm -f "$OPENEMR_CLIENT_STATE"
    die "the key was not stored, so client $client_id was deleted while still disabled; the next boot registers again"
  fi
  remove_sensitive "$WORK_DIR/add-version.json"
  save_state uploaded "$client_id" "$version" "$cert_sha" "$kid"
  log "stored the client's key, ID and OpenEMR's certificate as version $version of $OPENEMR_CLIENT_KEY_SECRET"
  finish_enable "$client_id" "$version" "$cert_sha" "$kid"
}

main() {
  init_hook
  check_prerequisites
  mkdir -p "$OPENEMR_CLIENT_DIR"
  chmod 700 "$OPENEMR_CLIENT_DIR"
  OPENEMR_CLIENT_STATE="$OPENEMR_CLIENT_DIR/state"
  wait_until_ready
  require_setup_complete
  plan_action
  case "$ACTION" in
    nothing) return 0 ;;
    enable) finish_enable "$STATE_client_id" "$STATE_secret_version" "$STATE_certificate_sha256" "$STATE_kid" ;;
    revoke) finish_revoke "$STATE_client_id" "$STATE_secret_version" "$STATE_certificate_sha256" "$STATE_kid" ;;
    *) register_client ;;
  esac
  log "done"
}

main "$@"
