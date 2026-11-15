#!/usr/bin/env bash
# Add a version to one of the PulseBridge secrets a person sets by hand (task T27).
#
#   scripts/secrets/set-secret.sh [--project <id>] [--prefix <prefix>] [--generate] [--replace] <short-name>
#
# The value is read from a hidden prompt (typed twice) or from standard input, never from an
# argument. See scripts/secrets/README.md.

# Refuse to run under xtrace (`bash -x`, `set -x` via SHELLOPTS): it would print every command
# with its expanded arguments to standard error, the value included. Checked before anything else.
case "$-" in
*x*)
  set +x
  printf '%s\n' "set-secret.sh: refusing to run with xtrace (bash -x or SHELLOPTS=xtrace): it would print the secret value. Run it without -x." >&2
  exit 2
  ;;
esac

set -eu -o pipefail
umask 077
# Bytes, not locale characters: ${#value} counts bytes and [[:print:]] means printable ASCII.
LC_ALL=C
export LC_ALL

SV_PROG="set-secret.sh"
# shellcheck source=lib.sh source-path=SCRIPTDIR
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() {
  cat <<'EOF'
Usage: scripts/secrets/set-secret.sh [options] <short-name>

Adds a version to the secret <prefix>-<short-name>. The value is read from a hidden prompt
(typed twice) when standard input is a terminal, else from standard input (one trailing
newline is removed). It is never taken from an argument.

Short names this script writes (read by the edge VM into password files):
  db-root-password        MariaDB root password
  openemr-db-password     OpenEMR's own database user
  openemr-admin-password  OpenEMR's "admin" login

Options:
  --project <id>     project ID (default: $CLOUDSDK_CORE_PROJECT)
  --prefix <prefix>  secret name prefix (default: $SECRET_NAME_PREFIX);
                     `terraform output -raw secret_name_prefix` in infra/envs/<env>
  --generate         store a random 32-character value (letters and digits); it is not shown
  --replace          add a version even though an enabled one exists (see README: the edge VM
                     does not pick it up by itself)
  -h, --help         this text

Exit codes: 0 stored; 1 gcloud or a precondition failed; 2 usage error or value refused.
EOF
}

# The secrets a person sets with this script, and who reads each (module identity's table). The
# others are written by something else and refused below with a pointer to it.
allowed_names="db-root-password openemr-db-password openemr-admin-password"

short_name=''
generate=0
replace=0
while [ "$#" -gt 0 ]; do
  case "$1" in
  -h | --help)
    usage
    exit 0
    ;;
  --project)
    [ "$#" -ge 2 ] || sv_die "$SV_EXIT_USAGE" "--project needs a value."
    sv_project="$2"
    shift 2
    ;;
  --project=*)
    sv_project="${1#--project=}"
    shift
    ;;
  --prefix)
    [ "$#" -ge 2 ] || sv_die "$SV_EXIT_USAGE" "--prefix needs a value."
    sv_prefix="$2"
    shift 2
    ;;
  --prefix=*)
    sv_prefix="${1#--prefix=}"
    shift
    ;;
  --generate)
    generate=1
    shift
    ;;
  --replace)
    replace=1
    shift
    ;;
  --log-http | --log-http=* | --verbosity | --verbosity=*)
    sv_die "$SV_EXIT_USAGE" "'${1%%=*}' is refused: gcloud's HTTP log and debug output would contain the value. This script runs gcloud with HTTP logging off and verbosity 'warning'."
    ;;
  -*)
    sv_die "$SV_EXIT_USAGE" "unknown option '${1%%=*}'. See --help."
    ;;
  *)
    if [ -n "$short_name" ]; then
      # Never echo it: it may be the value, typed as an argument by mistake.
      sv_die "$SV_EXIT_USAGE" "only one argument (the short name) is accepted; the extra argument was ignored and is not shown. The value is never taken from an argument, because arguments are visible in the process list and saved in shell history. If you typed the value there, remove it from your history and choose a new value."
    fi
    short_name="$1"
    shift
    ;;
  esac
done

if [ -z "$short_name" ]; then
  usage >&2
  sv_die "$SV_EXIT_USAGE" "missing <short-name>."
fi

sv_resolve_target

case "$short_name" in
google-health-client-id | google-health-client-secret | google-health-refresh-token)
  sv_die "$SV_EXIT_USAGE" "'$short_name' is written by the connect script, not here: scripts/connect-google-health/connect.sh (infra/envs/README.md, step 9) stores the OAuth client ID, client secret and refresh token together after Google's consent screen."
  ;;
openemr-client-private-key)
  sv_die "$SV_EXIT_USAGE" "'$short_name' is written by the edge VM itself: its openemr-oauth-client hook generates the key pair, registers the client with OpenEMR and stores the key as a version at first boot (infra/modules/openemr/README.md). A version written by hand would not match the client OpenEMR registered."
  ;;
reviewer-password)
  sv_die "$SV_EXIT_USAGE" "use scripts/secrets/set-reviewer-password.sh for '$short_name': it applies fhir-proxy's password rules and can generate and show one."
  ;;
"$sv_prefix"-*)
  sv_die "$SV_EXIT_USAGE" "pass the short name without the prefix, for example '${short_name#"$sv_prefix"-}' (the script adds '$sv_prefix-')."
  ;;
esac
case " $allowed_names " in
*" $short_name "*) ;;
*) sv_die "$SV_EXIT_USAGE" "unknown short name '$short_name'. This script writes: $allowed_names." ;;
esac

secret="$sv_prefix-$short_name"
sv_require_secret "$secret"

# The edge VM copies these secrets once, at first boot, and keeps its copy; MariaDB and OpenEMR
# take the passwords only when they set themselves up. A second version therefore changes
# nothing on a running VM, and it leaves `latest` out of step with the database. So an existing
# enabled version is a stop unless --replace says the person knows.
existing="$(sv_enabled_version "$secret")" || exit "$SV_EXIT_FAILED"
if [ -n "$existing" ] && [ "$replace" -ne 1 ]; then
  sv_die "$SV_EXIT_USAGE" "'$secret' already has an enabled version (${existing##*/}). A new one would not reach the database: the edge VM keeps the copy it made at first boot, and MariaDB and OpenEMR keep the password they were set up with. To rotate, change the password inside MariaDB or OpenEMR first, then run this again with --replace (scripts/secrets/README.md, \"Rotation\"). Nothing was written."
fi

if [ "$generate" -eq 1 ]; then
  sv_generate 32
else
  sv_read_value "the value for $secret"
fi
sv_check_value "$SV_MIN_LENGTH" "$SV_MAX_LENGTH"
sv_add_version "$secret"

sv_err "stored a new version of '$secret' in project '$sv_project'."
case "$short_name" in
db-root-password) file="mariadb-root-password" ;;
*) file="$short_name" ;;
esac
cat >&2 <<EOF
Who picks it up: the edge VM, which reads version "latest" of this secret once, at boot, into
/mnt/disks/edge/secrets/$file and then keeps that file.
- VM not created or not booted yet (infra/envs/README.md, step 6): nothing more to do.
- VM already booted without it (its log says "ERROR: secret $file ... 404"): on the VM run
  \`sudo google_metadata_script_runner startup\`, or reset the VM.
- VM already running with an older value: the new version is NOT used until the file is replaced
  on the VM, and the database must be changed first (README, "Rotation").
EOF
