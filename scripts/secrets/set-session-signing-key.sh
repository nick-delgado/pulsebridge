#!/usr/bin/env bash
# Create or rotate the key fhir-proxy signs demo sessions with (task T44, decision D16).
#
#   scripts/secrets/set-session-signing-key.sh [--project <id>] [--prefix <prefix>] [--rotate]
#
# The key is 32 random bytes (an HS256 key: RFC 7518, section 3.2, asks for a key at least as long
# as the hash output, 256 bits). They go from /dev/urandom straight into gcloud's standard input
# through a pipe: they are never in a shell variable, a file, an argument or the terminal, and
# nobody ever needs to see them. See scripts/secrets/README.md.

# Refuse to run under xtrace, as the other scripts do: nothing here expands the key, but xtrace
# would still print every gcloud command and is never needed. Checked before anything else.
case "$-" in
*x*)
  set +x
  printf '%s\n' "set-session-signing-key.sh: refusing to run with xtrace (bash -x or SHELLOPTS=xtrace). Run it without -x." >&2
  exit 2
  ;;
esac

set -eu -o pipefail
umask 077
LC_ALL=C
export LC_ALL

SV_PROG="set-session-signing-key.sh"
# shellcheck source=lib.sh source-path=SCRIPTDIR
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

# 32 bytes = 256 bits, the T43 to T45 contract's key ("a 32-byte random key").
KEY_BYTES=32
KEY_SOURCE=/dev/urandom

usage() {
  cat <<'EOF'
Usage: scripts/secrets/set-session-signing-key.sh [options]

Adds a version to the secret <prefix>-session-signing-key: 32 random bytes from /dev/urandom,
the key fhir-proxy signs and checks the demo's __session cookie with (D16). The key is
generated here and sent straight to Secret Manager; it is never shown, typed or written to disk.

If the secret already has an enabled version, nothing is written (so running it twice ends no
session) unless --rotate is given.

Options:
  --project <id>     project ID (default: $CLOUDSDK_CORE_PROJECT, then $GCP_PROJECT_ID)
  --prefix <prefix>  secret name prefix (default: $SECRET_NAME_PREFIX, then
                     $PULSEBRIDGE_NAME_PREFIX); from the repository root:
                     terraform -chdir=infra/envs/<env> output -raw secret_name_prefix
  --rotate           add a new key even though one exists. Every open demo session ends: tokens
                     signed with the old key no longer verify.
  -h, --help         this text

Exit codes: 0 stored, or a key already exists (without --rotate); 1 gcloud or a precondition
failed; 2 usage error.
EOF
}

rotate=0
while [ "$#" -gt 0 ]; do
  case "$1" in
  -h | --help)
    usage
    exit 0
    ;;
  --project)
    [ "$#" -ge 2 ] || sv_die "$SV_EXIT_USAGE" "--project needs a value."
    sv_set_project "$2"
    shift 2
    ;;
  --project=*)
    sv_set_project "${1#--project=}"
    shift
    ;;
  --prefix)
    [ "$#" -ge 2 ] || sv_die "$SV_EXIT_USAGE" "--prefix needs a value."
    sv_set_prefix "$2"
    shift 2
    ;;
  --prefix=*)
    sv_set_prefix "${1#--prefix=}"
    shift
    ;;
  --rotate)
    rotate=1
    shift
    ;;
  --log-http | --log-http=* | --verbosity | --verbosity=*)
    sv_die "$SV_EXIT_USAGE" "'${1%%=*}' is refused: gcloud's HTTP log and debug output would contain the key (the request body carries it). This script runs gcloud with HTTP logging off and verbosity 'warning'."
    ;;
  -*)
    sv_die "$SV_EXIT_USAGE" "unknown option $(sv_shown "${1%%=*}"). See --help."
    ;;
  *)
    sv_die "$SV_EXIT_USAGE" "this script takes no arguments; the argument was ignored and is not shown. The key is always generated, never given."
    ;;
  esac
done

sv_resolve_target
secret="$sv_prefix-session-signing-key"
sv_require_secret "$secret"

# A second key would end every session for nothing, so an existing one is kept unless --rotate.
existing="$(sv_enabled_version "$secret")" || exit "$SV_EXIT_FAILED"
if [ -n "$existing" ] && [ "$rotate" -ne 1 ]; then
  sv_err "'$secret' in project '$sv_project' already has an enabled key (${existing##*/}); nothing was written. To replace it (this ends every demo session), run again with --rotate."
  exit 0
fi

# The random source must give the full 32 bytes. Checked on a separate read, before anything is
# sent: the bytes counted here are thrown away (wc keeps only the count).
if ! [ -r "$KEY_SOURCE" ]; then
  sv_die "$SV_EXIT_FAILED" "cannot read $KEY_SOURCE; nothing was written."
fi
count="$(head -c "$KEY_BYTES" "$KEY_SOURCE" | wc -c)" || count=0
count="${count//[[:space:]]/}" # BSD wc pads the number with spaces
if [ "$count" != "$KEY_BYTES" ]; then
  sv_die "$SV_EXIT_FAILED" "$KEY_SOURCE gave $count bytes, not $KEY_BYTES; nothing was written."
fi

# The key itself: head reads exactly 32 bytes from the kernel's random device and writes them
# into the pipe; `--data-file=-` makes gcloud read the payload from its standard input. The bytes
# are binary (any value 0 to 255) and stored as they are, with no encoding and no newline: the
# secret's payload IS the key. gcloud's own output goes to standard error.
set +e
head -c "$KEY_BYTES" "$KEY_SOURCE" | sv_gcloud secrets versions add "$secret" \
  --project="$sv_project" --data-file=- --quiet >&2
statuses="${PIPESTATUS[*]}"
set -e
if [ "$statuses" != "0 0" ]; then
  sv_die "$SV_EXIT_FAILED" "could not store the key (exit codes: $statuses, random source then gcloud). If gcloud reported a new version anyway, run again with --rotate."
fi

sv_err "stored a new $KEY_BYTES-byte key as a version of '$secret' in project '$sv_project'. It is not shown and was not saved anywhere else."
if [ -n "$existing" ]; then
  cat >&2 <<'EOF'
Rotated. fhir-proxy mounts this secret as a file at version "latest", so it uses the new key
once it reads the file again, with no restart and no apply. From then on every session signed
with the old key is refused (401); visitors click "View the live demo" again.
The older version stays enabled but unused; disable it when you like (README, "Rotation").
EOF
else
  cat >&2 <<'EOF'
First key. fhir-proxy can now be deployed: Cloud Run refuses to deploy a service whose mounted
secret has no version (infra/envs/README.md, step 5).
EOF
fi
