#!/usr/bin/env bash
# Set or rotate the shared reviewer password that fhir-proxy checks at login (task T27, D9).
#
#   scripts/secrets/set-reviewer-password.sh [--project <id>] [--prefix <prefix>] [--generate [--show]]
#
# The password is read from a hidden prompt (typed twice) or from standard input, never from an
# argument, or generated. See scripts/secrets/README.md.

# Refuse to run under xtrace: it would print the password. Checked before anything else.
case "$-" in
*x*)
  set +x
  printf '%s\n' "set-reviewer-password.sh: refusing to run with xtrace (bash -x or SHELLOPTS=xtrace): it would print the password. Run it without -x." >&2
  exit 2
  ;;
esac

set -eu -o pipefail
umask 077
LC_ALL=C
export LC_ALL

SV_PROG="set-reviewer-password.sh"
# shellcheck source=lib.sh source-path=SCRIPTDIR
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

# 24 random letters and digits, about 143 bits (docs/spikes/s6.md: 24 or more).
GENERATED_LENGTH=24

usage() {
  cat <<'EOF'
Usage: scripts/secrets/set-reviewer-password.sh [options]

Adds a version to the secret <prefix>-reviewer-password, which fhir-proxy checks at login.
The password is read from a hidden prompt (typed twice) when standard input is a terminal,
else from standard input (one trailing newline is removed). It is never taken from an argument.

Rules: 16 to 128 characters, printable ASCII (letters, digits, punctuation, space), not only
spaces; leading and trailing spaces are kept as part of the password.

Options:
  --project <id>     project ID (default: $CLOUDSDK_CORE_PROJECT, then $GCP_PROJECT_ID)
  --prefix <prefix>  secret name prefix (default: $SECRET_NAME_PREFIX, then
                     $PULSEBRIDGE_NAME_PREFIX); from the repository root:
                     terraform -chdir=infra/envs/<env> output -raw secret_name_prefix
  --generate         store a random 24-character password (letters and digits) and print it
                     once on standard output, after it is stored. Refused when standard output
                     is not a terminal, unless --show is given too.
  --show             with --generate: print it even when standard output is not a terminal
                     (for example piped into a password manager). Never written anywhere else.
  -h, --help         this text

Exit codes: 0 stored; 1 gcloud or a precondition failed; 2 usage error or value refused.
EOF
}

generate=0
show=0
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
  --generate)
    generate=1
    shift
    ;;
  --show)
    show=1
    shift
    ;;
  --log-http | --log-http=* | --verbosity | --verbosity=*)
    sv_die "$SV_EXIT_USAGE" "'${1%%=*}' is refused: gcloud's HTTP log and debug output would contain the password. This script runs gcloud with HTTP logging off and verbosity 'warning'."
    ;;
  -*)
    sv_die "$SV_EXIT_USAGE" "unknown option $(sv_shown "${1%%=*}"). See --help."
    ;;
  *)
    # Never echo it: it may be the password, typed as an argument by mistake.
    sv_die "$SV_EXIT_USAGE" "this script takes no arguments; the argument was ignored and is not shown. The password is never taken from an argument, because arguments are visible in the process list and saved in shell history. If you typed the password there, remove it from your history and choose a new one."
    ;;
  esac
done

if [ "$show" -eq 1 ] && [ "$generate" -ne 1 ]; then
  sv_die "$SV_EXIT_USAGE" "--show only applies to --generate: a password you typed is never shown."
fi
# A generated password nobody saw is useless, and printing it into a pipe or file that was not
# asked for could put it on disk. So without a terminal on standard output, --show must say so.
# Checked before anything is written.
if [ "$generate" -eq 1 ] && [ "$show" -ne 1 ] && ! [ -t 1 ]; then
  sv_die "$SV_EXIT_USAGE" "--generate prints the password on standard output, which is not a terminal here. Add --show if you mean to send it there (for example into a password manager); nothing was written."
fi

sv_resolve_target
secret="$sv_prefix-reviewer-password"
sv_require_secret "$secret"

if [ "$generate" -eq 1 ]; then
  sv_generate "$GENERATED_LENGTH"
  SV_KEEP_VALUE=1
else
  sv_read_value "the reviewer password for $secret"
fi
# fhir-proxy itself accepts 1 to 1024 characters (services/fhir-proxy: password.py, app.py);
# the 16-character minimum is the project's rule (D9, s6), enforced here.
sv_check_value "$SV_MIN_LENGTH" "$SV_MAX_LENGTH" reviewer
sv_add_version "$secret"

if [ "$generate" -eq 1 ]; then
  sv_err "stored a new version of '$secret' in project '$sv_project'. The generated password follows, shown this once; it is not saved anywhere else:"
  builtin printf '%s\n' "$sv_value"
  sv_clear
else
  sv_err "stored a new version of '$secret' in project '$sv_project'."
fi
cat >&2 <<'EOF'
Who picks it up: fhir-proxy, which mounts this secret as a file at version "latest". A mounted
secret is fetched from Secret Manager when the file is read, and the proxy re-reads it every
30 seconds (PASSWORD_CACHE_SECONDS), so no restart and no apply are needed.
- Within about 30 seconds the new password works and the old one stops working.
- Every signed-in reviewer is signed out at once: the session-token key is derived from the
  password, so tokens issued under the old one no longer verify.
- The older versions stay enabled; disable them when you are sure (README, "Rotation").
- dev only: if this environment sets the password from tfvars (reviewer_password_wo_version > 0),
  do not use this script there: the two ways undo each other (infra/modules/identity/README.md,
  "Dev only"). Use one way per environment.
EOF
