# shellcheck shell=bash
# Shared code for set-secret.sh and set-reviewer-password.sh (task T27). Sourced, never run.
#
# What every function here protects: the secret value. It lives only in the shell variables
# sv_value and sv_confirm, which are never exported. It reaches gcloud on standard input through
# the shell builtin printf (a builtin runs inside bash, so the value is never a process argument
# that `ps` could show), and it is never written to a file: no here-document or here-string (bash
# backs those with temporary files), no `echo` into a file, no `set -x`.
#
# Written for bash 3.2 (macOS's /bin/bash) as well as current bash: no associative arrays, no
# `${var,,}`, no `[[ -v ]]`, no `mapfile`, no `local -n`, and no "${array[@]}" of a possibly empty
# array (an error under `set -u` before bash 4.4).

# Exit codes, the same in both scripts.
SV_EXIT_FAILED=1 # gcloud failed, or a precondition (gcloud missing, secret missing)
SV_EXIT_USAGE=2  # a usage error or a refused value; nothing was written

# Character rules for every value (see README, "Value rules"). Printable ASCII is letters, digits,
# punctuation and the space (0x20 to 0x7E): every program on the path (gcloud, the edge VM's shell
# scripts, OpenEMR's PHP setup, a browser form, fhir-proxy) handles it the same way, and a
# password can be typed on any keyboard.
# shellcheck disable=SC2034 # used by the scripts that source this file
SV_MIN_LENGTH=16
# shellcheck disable=SC2034 # used by the scripts that source this file
SV_MAX_LENGTH=128
# Generated values: letters and digits only (62 symbols, about 5.95 bits each), so they survive
# any quoting. 32 characters are about 190 bits; the reviewer password uses 24 (about 143 bits,
# docs/spikes/s6.md asks for 24 or more) because a person may have to type it.
SV_GENERATE_ALPHABET='A-Za-z0-9'

# allexport (`set -a`, or SHELLOPTS=allexport in the environment) would export every variable the
# script assigns, the value included, into gcloud's environment. Switch it off.
set +a
# A variable of the same name in the caller's environment arrives here exported, and assigning to
# it would keep that export attribute. unset drops the imported variable first.
unset sv_value sv_confirm
sv_value=''
sv_confirm=''

sv_err() {
  # Messages go to standard error; standard output is reserved for the generated reviewer
  # password. No message ever contains the value.
  builtin printf '%s: %s\n' "$SV_PROG" "$*" >&2
}

sv_die() { # sv_die <exit code> <message>
  local code="$1"
  shift
  sv_clear
  sv_err "$*"
  exit "$code"
}

sv_clear() {
  sv_value=''
  sv_confirm=''
  unset sv_value sv_confirm
}

# Run gcloud with HTTP logging off, no file log and a fixed verbosity, whatever the person's
# gcloud configuration or environment says. `--log-http` (or the property core/log_http) logs
# request bodies, and the body of `versions add` carries the value (base64-encoded); verbosity
# `debug` would print it. Properties set in the environment (CLOUDSDK_CORE_*) override gcloud's
# configuration files, and the --verbosity flag overrides both. Only these assignments reach
# gcloud's environment; sv_value is not exported.
sv_gcloud() {
  CLOUDSDK_CORE_LOG_HTTP=false \
    CLOUDSDK_CORE_DISABLE_FILE_LOGGING=true \
    CLOUDSDK_CORE_VERBOSITY=warning \
    gcloud "$@" --verbosity=warning
}

# --project wins over CLOUDSDK_CORE_PROJECT (the environment form of gcloud's core/project).
# --prefix wins over SECRET_NAME_PREFIX. The prefix is the env root's output secret_name_prefix
# (module identity's name_prefix), so the secret ID is "<prefix>-<short name>".
sv_project=''
sv_prefix=''

sv_resolve_target() {
  if [ -z "$sv_project" ]; then
    sv_project="${CLOUDSDK_CORE_PROJECT:-}"
  fi
  if [ -z "$sv_prefix" ]; then
    sv_prefix="${SECRET_NAME_PREFIX:-}"
  fi
  if [ -z "$sv_project" ]; then
    sv_die "$SV_EXIT_USAGE" "no project: pass --project <project-id> or set CLOUDSDK_CORE_PROJECT."
  fi
  if [ -z "$sv_prefix" ]; then
    sv_die "$SV_EXIT_USAGE" "no secret name prefix: pass --prefix <prefix> or set SECRET_NAME_PREFIX (the value of \`terraform output -raw secret_name_prefix\` in infra/envs/<env>)."
  fi
  # A project ID: 6 to 30 lowercase letters, digits and hyphens, starting with a letter
  # (optionally "domain:" in front, for the old domain-scoped projects).
  local project_re='^([a-z][a-z0-9.-]*:)?[a-z][a-z0-9-]{4,28}[a-z0-9]$'
  if ! [[ "$sv_project" =~ $project_re ]]; then
    sv_die "$SV_EXIT_USAGE" "'$sv_project' is not a valid project ID."
  fi
  # The same rule as identity_name_prefix in infra/envs/*/variables.tf.
  local prefix_re='^[a-z]([a-z0-9-]{0,10}[a-z0-9])?$'
  if ! [[ "$sv_prefix" =~ $prefix_re ]]; then
    sv_die "$SV_EXIT_USAGE" "'$sv_prefix' is not a valid secret name prefix (1-12 lowercase letters, digits and hyphens; see \`terraform output -raw secret_name_prefix\`)."
  fi
}

# Check the secret exists, by name only, before any value is read: the person is not asked for a
# password that cannot be stored, and gcloud's error output (shown as it is) cannot contain one.
sv_require_secret() { # sv_require_secret <secret id>
  local secret="$1" out rc=0
  if ! command -v gcloud >/dev/null 2>&1; then
    sv_die "$SV_EXIT_FAILED" "gcloud is not on PATH. Install the Google Cloud CLI and run \`gcloud auth login\`."
  fi
  out="$(sv_gcloud secrets describe "$secret" --project="$sv_project" --format='value(name)' 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    sv_err "gcloud could not find the secret '$secret' in project '$sv_project' (exit code $rc). gcloud said:"
    builtin printf '%s\n' "$out" >&2
    sv_die "$SV_EXIT_FAILED" "The secrets are created by Terraform, not by this script: run the identity apply first (infra/envs/README.md, step 4: \`terraform apply -target=module.stack.module.identity\`), and check that the prefix equals \`terraform output -raw secret_name_prefix\` and that you are signed in (\`gcloud auth list\`) with access to the project."
  fi
}

# Prints the name of the newest enabled version, or nothing when there is none. Exits on a gcloud
# failure: "could not tell" must not be read as "none".
sv_enabled_version() { # sv_enabled_version <secret id>
  local secret="$1" out rc=0
  out="$(sv_gcloud secrets versions list "$secret" --project="$sv_project" \
    --filter='state:ENABLED' --sort-by='~createTime' --limit=1 --format='value(name)' 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    sv_err "gcloud could not list the versions of '$secret' (exit code $rc). gcloud said:"
    builtin printf '%s\n' "$out" >&2
    sv_die "$SV_EXIT_FAILED" "Nothing was written."
  fi
  builtin printf '%s' "$out"
}

# Generate a random value into sv_value. The source is the kernel's random device, filtered to
# the alphabet: `tr -dc` drops every byte outside it, which is rejection sampling, so each of the
# 62 symbols is equally likely (taking the bytes modulo 62 would favour some). Not $RANDOM, which
# is a 15-bit pseudo-random generator seeded from the clock and process ID. The value travels
# through a pipe and a command substitution (also a pipe), never a file.
sv_generate() { # sv_generate <length>
  local length="$1"
  sv_value="$(LC_ALL=C tr -dc "$SV_GENERATE_ALPHABET" </dev/urandom 2>/dev/null | head -c "$length")" || true
  # `tr` ends with SIGPIPE when `head` has enough, so the pipeline's status says nothing; check
  # the result instead.
  if [ "${#sv_value}" -ne "$length" ]; then
    sv_die "$SV_EXIT_FAILED" "could not read random bytes from /dev/urandom."
  fi
  case "$sv_value" in
  *[!A-Za-z0-9]*) sv_die "$SV_EXIT_FAILED" "the generated value has an unexpected character; nothing was written." ;;
  esac
}

# Read the value into sv_value, from the terminal (typed twice, not echoed) or from standard input
# when it is a pipe or a file.
sv_read_value() { # sv_read_value <what, for the prompt>
  local what="$1" rc=0
  if [ -t 0 ]; then
    # read: -s no echo, -r keep backslashes; IFS= keeps leading and trailing spaces.
    builtin printf 'Enter %s (input hidden): ' "$what" >&2
    IFS= read -r -s sv_value || rc=$?
    builtin printf '\n' >&2
    if [ "$rc" -ne 0 ]; then
      sv_die "$SV_EXIT_USAGE" "no input (end of input before Enter); nothing was written."
    fi
    builtin printf 'Enter it again to confirm: ' >&2
    IFS= read -r -s sv_confirm || rc=$?
    builtin printf '\n' >&2
    if [ "$rc" -ne 0 ]; then
      sv_die "$SV_EXIT_USAGE" "no confirmation (end of input before Enter); nothing was written."
    fi
    if [ "$sv_value" != "$sv_confirm" ]; then
      sv_die "$SV_EXIT_USAGE" "the two entries differ; nothing was written. Run the command again."
    fi
    sv_confirm=''
  else
    # Read everything up to a NUL byte or the end of input. read returns 0 only when it stopped at
    # a NUL (the delimiter), so 0 means the input holds a NUL: refused, because a shell variable
    # cannot hold one and the value would be cut there.
    if IFS= read -r -d '' sv_value; then
      sv_die "$SV_EXIT_USAGE" "the input contains a NUL byte; nothing was written."
    fi
    # One newline at the very end is the end of the input line (`printf '%s\n'`, `echo`, a
    # password manager), not part of the value. Any other newline is refused below.
    sv_value="${sv_value%$'\n'}"
  fi
}

# Refuse a value that breaks the rules. Messages say which rule, never the value.
sv_check_value() { # sv_check_value <min length> <max length>
  local min="$1" max="$2"
  if [ -z "$sv_value" ]; then
    sv_die "$SV_EXIT_USAGE" "the value is empty; nothing was written."
  fi
  case "$sv_value" in
  *$'\n'*) sv_die "$SV_EXIT_USAGE" "the value contains a newline (only one newline at the very end of piped input is removed); nothing was written." ;;
  *$'\r'*) sv_die "$SV_EXIT_USAGE" "the value contains a carriage return (a file saved with Windows line endings?); nothing was written." ;;
  *[[:cntrl:]]*) sv_die "$SV_EXIT_USAGE" "the value contains a control character (for example a tab); nothing was written." ;;
  *[![:print:]]*) sv_die "$SV_EXIT_USAGE" "the value contains a character outside printable ASCII (letters, digits, punctuation, space); nothing was written." ;;
  esac
  # LC_ALL=C (set by the scripts) makes ${#sv_value} count bytes; with printable ASCII only,
  # bytes and characters are the same.
  if [ "${#sv_value}" -lt "$min" ]; then
    sv_die "$SV_EXIT_USAGE" "the value has ${#sv_value} characters; at least $min are needed. Nothing was written."
  fi
  if [ "${#sv_value}" -gt "$max" ]; then
    sv_die "$SV_EXIT_USAGE" "the value has ${#sv_value} characters; at most $max are allowed. Nothing was written."
  fi
}

# Add sv_value as a new version, then clear it whatever gcloud did.
sv_add_version() { # sv_add_version <secret id>
  local secret="$1" statuses
  # The builtin printf writes the exact bytes, with no newline added, to gcloud's standard input;
  # `--data-file=-` makes gcloud read the payload from there. gcloud's own output goes to
  # standard error, so standard output carries nothing but a generated password.
  set +e
  builtin printf '%s' "$sv_value" | sv_gcloud secrets versions add "$secret" \
    --project="$sv_project" --data-file=- --quiet >&2
  statuses="${PIPESTATUS[*]}"
  set -e
  # Keep the value only if it must still be shown (a generated reviewer password).
  if [ "${SV_KEEP_VALUE:-}" != "1" ]; then
    sv_clear
  fi
  case "$statuses" in
  *" 0") ;;
  *)
    sv_clear
    sv_die "$SV_EXIT_FAILED" "gcloud could not add the version (exit code ${statuses##* }); the value was not stored and has been cleared from memory."
    ;;
  esac
}
