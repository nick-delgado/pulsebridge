# shellcheck shell=bash
# shellcheck disable=SC2154 # the OPENEMR_* configuration is assigned in part 1 (the rendered template)
#
# Part 4 of hook "openemr": start the OpenEMR container, on every boot, safely repeated.
#
#   1. refuse to go on unless the data disk is mounted, MariaDB is healthy and the three password
#      files exist (OpenEMR exits if its database is not ready, spike S1);
#   2. pull the image by digest through the Docker Hub mirror;
#   3. seed the sites directory on the data disk from the image, once (a bind mount, unlike a
#      Docker volume, starts empty);
#   4. make sure OpenEMR's TLS certificate exists (generated once on the VM, for the VM's address);
#   5. write the Apache low-memory settings (spike S2: four workers, required);
#   6. block the metadata server for the edge Docker network (before OpenEMR can start);
#   7. create, replace or start the container (label-hash pattern, graceful stop);
#   8. wait for /meta/health/readyz;
#   9. check the OpenEMR settings in the database and write them there if the OPENEMR_SETTING_*
#      variables did not take effect; then check the FHIR API answers.

# Spike S2's apache-lowmem.conf, loaded last because of its zz- name, so it overrides the image's
# mpm.conf. MaxRequestWorkers is the setting that mattered: stock Apache grew to 38 PHP workers and
# 223 MB of swap under a burst; capped, 5 processes. Required on the e2-micro.
apache_lowmem_conf() {
  cat << 'EOF'
# PulseBridge (spike S2): cap Apache prefork at four workers on the e2-micro.
StartServers            2
MinSpareServers         1
MaxSpareServers         2
MaxRequestWorkers       4
MaxConnectionsPerChild  500
EOF
}

check_prerequisites() {
  require_data_disk
  require_mariadb_healthy
  require_secret_files "$MARIADB_ROOT_SECRET" "$OPENEMR_DB_SECRET" "$OPENEMR_ADMIN_SECRET"
  [ -d "$EDGE_OPENEMR_DIR" ] || die "$EDGE_OPENEMR_DIR does not exist (edge-vm creates it after mounting the data disk)"
  log "data disk mounted, MariaDB healthy, password files present"
}

# A bind mount does not copy the image's files the way an empty Docker volume does, so the first
# boot copies the image's sites directory onto the data disk. The directory itself is copied
# (cp -a sites /seed/sites), so it keeps the image's owner and mode: the hook's own umask is 077,
# and a sites directory made by the hook would be unreadable for Apache's unprivileged user. It is
# copied into a temporary directory and moved into place, so an interrupted copy is never mistaken
# for a seeded directory.
seed_sites_dir() {
  local tmp="$EDGE_DATA_DIR/.openemr-sites-seed"
  if [ -n "$(ls -A "$EDGE_OPENEMR_DIR")" ]; then
    log "sites directory $EDGE_OPENEMR_DIR has files; not seeding"
    return 0
  fi
  log "sites directory is empty: copying the image's sites directory onto the data disk (first boot)"
  rm -rf "$tmp"
  mkdir -p "$tmp"
  timeout 300 docker run --rm --network none --user 0:0 --entrypoint sh \
    --mount "type=bind,source=$tmp,target=/seed" \
    "$OPENEMR_IMAGE" -c "cp -a $OPENEMR_SITES_TARGET /seed/sites"
  if [ ! -d "$tmp/sites" ] || [ -z "$(ls -A "$tmp/sites")" ]; then
    die "copying the image's sites directory produced nothing"
  fi
  rmdir "$EDGE_OPENEMR_DIR"
  mv "$tmp/sites" "$EDGE_OPENEMR_DIR"
  rm -rf "$tmp"
  log "sites directory seeded"
}

# OpenEMR's image makes its own self-signed certificate for "localhost" when the container is
# created, so it changes whenever the container is replaced and does not name the VM's address.
# fhir-proxy verifies the certificate and the address, so the hook makes one, once, for the
# address the proxy calls, keeps it on the data disk, and bind-mounts it where the image looks.
ensure_tls_certificate() {
  local tmp="$OPENEMR_TLS_DIR.new"
  mkdir -p "$OPENEMR_TLS_DIR"
  chmod 700 "$OPENEMR_TLS_DIR"
  if [ -s "$OPENEMR_TLS_CERT" ] && [ -s "$OPENEMR_TLS_DIR/server.key" ] &&
    [ "$(cat "$OPENEMR_TLS_DIR/address" 2> /dev/null || true)" = "$OPENEMR_INTERNAL_IP" ]; then
    log "TLS certificate for $OPENEMR_INTERNAL_IP present; keeping it"
    return 0
  fi
  log "generating OpenEMR's TLS certificate for $OPENEMR_INTERNAL_IP (valid ${OPENEMR_TLS_DAYS} days); the OAuth2 client will be registered again so fhir-proxy gets it"
  rm -rf "$tmp"
  mkdir -p "$tmp"
  chmod 700 "$tmp"
  run_client_tools "$tmp" -- tls-cert /work "$OPENEMR_INTERNAL_IP" "$OPENEMR_TLS_DAYS"
  if [ ! -s "$tmp/server.crt" ] || [ ! -s "$tmp/server.key" ]; then
    die "the certificate tool produced no certificate"
  fi
  chmod 600 "$tmp/server.key"
  chmod 644 "$tmp/server.crt"
  mv -f "$tmp/server.key" "$OPENEMR_TLS_DIR/server.key"
  mv -f "$tmp/server.crt" "$OPENEMR_TLS_CERT"
  printf '%s\n' "$OPENEMR_INTERNAL_IP" > "$OPENEMR_TLS_DIR/address"
  rm -rf "$tmp"
}

write_apache_config() {
  local dir="$EDGE_STATE_DIR/openemr" file
  file="$dir/apache-lowmem.conf"
  mkdir -p "$dir"
  if [ "$(cat "$file" 2> /dev/null || true)" != "$(apache_lowmem_conf)" ]; then
    apache_lowmem_conf > "$file.tmp"
    chmod 644 "$file.tmp"
    mv "$file.tmp" "$file"
    log "wrote $file (Apache capped at four workers)"
  fi
}

# The bridge interface of the edge Docker network: br-<first 12 characters of the network ID>,
# unless the network was created with its own interface name.
bridge_interface() {
  local name id
  name=$(timeout 30 docker network inspect --format '{{index .Options "com.docker.network.bridge.name"}}' "$EDGE_DOCKER_NETWORK")
  if [ -z "$name" ] || [ "$name" = "<no value>" ]; then
    id=$(timeout 30 docker network inspect --format '{{.Id}}' "$EDGE_DOCKER_NETWORK")
    name="br-${id:0:12}"
  fi
  [[ "$name" =~ ^[A-Za-z0-9_.-]{1,15}$ ]] || die "unexpected bridge interface name for $EDGE_DOCKER_NETWORK"
  printf '%s' "$name"
}

# Every container can reach the metadata server and get a token for the VM's account (edge-vm
# README, "Trust boundary"). OpenEMR is a large PHP application reachable from fhir-proxy, so its
# network may not: a DROP rule in DOCKER-USER, the chain Docker reserves for the administrator's
# rules and evaluates before its own (Docker docs, "Packet filtering and firewalls"). It matches
# packets arriving from the edge network's bridge, so it holds whatever address Docker gives the
# container; the MLLP adapter (default bridge) and the VM itself are not affected. Docker leaves
# DOCKER-USER alone when its daemon restarts; a reboot empties iptables, and this hook adds the
# rule again before it starts OpenEMR (Docker cannot start OpenEMR earlier: its bind mounts are on
# the data disk, which is not mounted until the startup script runs).
block_metadata_server() {
  local bridge rule
  if [ "$OPENEMR_BLOCK_METADATA" != "true" ]; then
    warn "block_metadata_server is off: OpenEMR can reach the metadata server and get the VM account's token"
    return 0
  fi
  bridge=$(bridge_interface)
  rule=(DOCKER-USER -i "$bridge" -d "$METADATA_SERVER_IP/32" -j DROP)
  if timeout 30 iptables -w 10 -C "${rule[@]}" 2> /dev/null; then
    log "metadata server already blocked for $EDGE_DOCKER_NETWORK ($bridge)"
    return 0
  fi
  if ! timeout 30 iptables -w 10 -I "${rule[@]}"; then
    die "could not add the iptables rule that blocks the metadata server for $EDGE_DOCKER_NETWORK: not starting OpenEMR"
  fi
  timeout 30 iptables -w 10 -C "${rule[@]}" || die "the metadata-server rule is not in DOCKER-USER after adding it: not starting OpenEMR"
  log "blocked the metadata server ($METADATA_SERVER_IP) for containers on $EDGE_DOCKER_NETWORK ($bridge)"
}

start_openemr() {
  local cert_sha apache_sha setting env_args=()
  cert_sha=$(sha256sum "$OPENEMR_TLS_CERT" | awk '{ print $1 }')
  apache_sha=$(sha256sum "$EDGE_STATE_DIR/openemr/apache-lowmem.conf" | awk '{ print $1 }')
  for setting in "${OPENEMR_SETTINGS[@]}"; do
    env_args+=(--env "OPENEMR_SETTING_$setting")
  done

  # The three passwords are given to the image the only way it reads them: environment variables
  # (it has no *_FILE variants). `--env NAME` without a value copies NAME from this process's
  # environment, so the values are not arguments of `docker run`. They are, however, part of the
  # container's configuration: root on the VM can see them with `docker inspect openemr`, which is
  # no more than root can already read in the secret files (README, "Secrets").
  (
    MYSQL_ROOT_PASS=$(secret_value "$MARIADB_ROOT_SECRET")
    MYSQL_PASS=$(secret_value "$OPENEMR_DB_SECRET")
    OE_PASS=$(secret_value "$OPENEMR_ADMIN_SECRET")
    export MYSQL_ROOT_PASS MYSQL_PASS OE_PASS

    # --publish: HTTPS only, on the VM's address; port 80 is not published. The project-base
    #   firewall admits only the serverless subnet (fhir-proxy) to it (D13, D14).
    # --mount type=bind, never -v: -v would create a missing source directory, and OpenEMR would
    #   then run its first-time setup on an empty directory (edge-vm README).
    # --restart unless-stopped: back after a crash and after the daemon restarts; on a reboot the
    #   bind mounts are missing until the data disk is mounted, so this hook starts it.
    # The labels carry the certificate's and the Apache file's hashes, so a new certificate or new
    #   settings change the configuration hash and the container is replaced (Apache reads both
    #   only at start).
    ensure_container "$OPENEMR_CONTAINER" \
      --restart unless-stopped \
      --stop-timeout "$OPENEMR_STOP_TIMEOUT_SECONDS" \
      --network "$EDGE_DOCKER_NETWORK" \
      --publish "$OPENEMR_HTTPS_PORT:443" \
      --mount "type=bind,source=$EDGE_OPENEMR_DIR,target=$OPENEMR_SITES_TARGET" \
      --mount "type=bind,source=$EDGE_STATE_DIR/openemr/apache-lowmem.conf,target=/etc/apache2/conf.d/zz-lowmem.conf,readonly" \
      --mount "type=bind,source=$OPENEMR_TLS_CERT,target=$OPENEMR_TLS_CERT_TARGET,readonly" \
      --mount "type=bind,source=$OPENEMR_TLS_DIR/server.key,target=$OPENEMR_TLS_KEY_TARGET,readonly" \
      --label "pulsebridge.tls-sha256=$cert_sha" \
      --label "pulsebridge.apache-sha256=$apache_sha" \
      --env "MYSQL_HOST=$EDGE_MARIADB_CONTAINER" \
      --env MYSQL_ROOT_PASS \
      --env "MYSQL_USER=$OPENEMR_DB_USER" \
      --env MYSQL_PASS \
      --env "OE_USER=$OPENEMR_ADMIN_USER" \
      --env OE_PASS \
      "${env_args[@]}" \
      --log-opt max-size=10m --log-opt max-file=3 \
      "$OPENEMR_IMAGE"
  )
}

# Spike S1 saw the image apply OPENEMR_SETTING_* on every start; spike S2 saw no effect and wrote
# the globals table instead. So read the values back from the database and, for any that are wrong,
# write them there (S2's REPLACE INTO, keyed on gl_name and gl_index), then read them again.
verify_settings() {
  local setting name value i names=() want=() current wrong=() sql="" row code
  for setting in "${OPENEMR_SETTINGS[@]}"; do
    names+=("${setting%%=*}")
    want+=("${setting#*=}")
  done
  for name in "${OPENEMR_SETTINGS_KEPT_OFF[@]}"; do
    names+=("$name")
    want+=("0")
  done

  current=$(settings_from_db "${names[@]}")
  for i in "${!names[@]}"; do
    value=$(printf '%s\n' "$current" | awk -F '\t' -v n="${names[$i]}" '$1 == n { print $2; exit }')
    if [ "$value" != "${want[$i]}" ]; then
      wrong+=("${names[$i]}")
      sql+="REPLACE INTO globals (gl_name, gl_index, gl_value) VALUES ($(sql_literal "${names[$i]}"), 0, $(sql_literal "${want[$i]}"));"$'\n'
    fi
  done

  if [ "${#wrong[@]}" -eq 0 ]; then
    log "OpenEMR settings are as configured (OPENEMR_SETTING_* took effect; rest_api is off)"
  else
    warn "OpenEMR settings not as configured after start: ${wrong[*]}. The OPENEMR_SETTING_* variables did not take effect (or rest_api was on); writing them to the globals table (spike S2's fallback)"
    printf '%s' "$sql" | openemr_sql > /dev/null
    current=$(settings_from_db "${names[@]}")
    for i in "${!names[@]}"; do
      row=$(printf '%s\n' "$current" | awk -F '\t' -v n="${names[$i]}" '$1 == n { print $2; exit }')
      [ "$row" = "${want[$i]}" ] || die "setting ${names[$i]} is still not ${want[$i]} after the database write"
    done
    log "applied through the database: ${wrong[*]}"
  fi

  # The FHIR API answers its CapabilityStatement only when rest_fhir_api is on (S1: otherwise
  # "API is disabled"). The new value takes effect on the next request (S1).
  while :; do
    code=$(openemr_request /apis/default/fhir/metadata /dev/null) || true
    if [ "$code" = "200" ]; then
      log "FHIR API answers at $OPENEMR_SITE_ADDRESS/apis/default/fhir (metadata 200)"
      return 0
    fi
    if [ "$(remaining)" -le 0 ]; then
      die "the FHIR API does not answer (metadata $code) although rest_fhir_api is 1"
    fi
    pause
  done
}

# Tab-separated name and value of the named globals (index 0).
settings_from_db() {
  local list="" name
  for name in "$@"; do
    list+="${list:+, }$(sql_literal "$name")"
  done
  printf 'SELECT gl_name, gl_value FROM globals WHERE gl_index = 0 AND gl_name IN (%s);\n' "$list" | openemr_sql
}

main() {
  init_hook
  check_prerequisites
  pull_image "$OPENEMR_IMAGE"
  seed_sites_dir
  ensure_tls_certificate
  write_apache_config
  block_metadata_server
  start_openemr
  wait_until_ready
  verify_settings
  log "done: OpenEMR is up at $OPENEMR_SITE_ADDRESS (port $OPENEMR_HTTPS_PORT on the VM, HTTPS only)"
}

main "$@"
