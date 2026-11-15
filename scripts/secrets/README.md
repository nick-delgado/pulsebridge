# scripts/secrets

Two scripts that put secret values into Google Secret Manager after `terraform apply` (task T27):

| Script | Writes | Read by |
| --- | --- | --- |
| `set-secret.sh <short-name>` | `<prefix>-db-root-password`, `<prefix>-openemr-db-password`, `<prefix>-openemr-admin-password` | the edge VM, into password files for MariaDB and OpenEMR |
| `set-reviewer-password.sh` | `<prefix>-reviewer-password` | fhir-proxy, which checks it at the dashboard login (D9) |

Plain bash and `gcloud`; no Python, no extra install. They run on Linux and on macOS's
`/bin/bash` 3.2.

## Secret Manager in two minutes

- A **secret** is a named, access-controlled container, for example `pb-db-root-password`. It
  has an IAM policy (who may read it) but no value of its own.
- A **version** is one value stored in a secret. Versions are numbered 1, 2, 3, ... in the order
  they are added, and are never changed: a new value is a new version. Each version is `ENABLED`,
  `DISABLED` (kept, but cannot be read) or `DESTROYED`.
- **`latest`** is an alias for the newest version. Every PulseBridge reader asks for `latest`, so
  adding a version is how a value changes. *When* each reader asks again differs, see
  "Who picks up a new version".

**Why Terraform creates the secret and a script adds the version.** Terraform records everything
it manages in its state file, and a value written through Terraform would be stored there in
clear. So module `infra/modules/identity` creates the eight secrets **empty**, with their access
policies, and the value is added afterwards, outside Terraform, by whoever knows it:

| Secret (`<prefix>-...`) | Version written by |
| --- | --- |
| `db-root-password`, `openemr-db-password`, `openemr-admin-password` | **`set-secret.sh`** (you) |
| `reviewer-password` | **`set-reviewer-password.sh`** (you), or in dev optionally Terraform's write-only argument (identity README, "Dev only"), never both |
| `google-health-client-id`, `-client-secret`, `-refresh-token` | `scripts/connect-google-health/connect.sh` (after Google's consent screen) |
| `openemr-client-private-key` | the edge VM itself, when it registers fhir-proxy with OpenEMR at first boot |

`set-secret.sh` refuses every name it does not write, and says which tool does.

## Before you run them

- `gcloud auth login` as a person who may add versions to these secrets and list them
  (`roles/secretmanager.secretVersionAdder` and `roles/secretmanager.viewer` on the secrets, or
  project Owner / Secret Manager Admin; the deployer normally has these).
- The secrets must exist: run the identity apply first (infra/envs/README.md, step 4,
  `terraform apply -target=module.stack.module.identity`). The scripts check this, by name, before
  asking for a value.
- Tell them the project and the secret name prefix. The prefix is the env root's output
  `secret_name_prefix` (module identity's `name_prefix`, `pb` by default):

```bash
export CLOUDSDK_CORE_PROJECT=<project-id>
export SECRET_NAME_PREFIX="$(terraform -chdir=infra/envs/<dev|prod> output -raw secret_name_prefix)"
```

or pass `--project <project-id>` and `--prefix <prefix>` to each command (the flags win).

## Commands

```bash
# Database passwords nobody needs to type: generate them (32 random letters and digits, not shown).
scripts/secrets/set-secret.sh --generate db-root-password
scripts/secrets/set-secret.sh --generate openemr-db-password

# OpenEMR's "admin" login: generate it too, or type your own (hidden prompt, typed twice).
scripts/secrets/set-secret.sh openemr-admin-password

# The reviewer password: generate one and see it once, to give to reviewers...
scripts/secrets/set-reviewer-password.sh --generate
# ...or type your own (16 to 128 characters, hidden prompt, typed twice).
scripts/secrets/set-reviewer-password.sh

# From a password manager or another program: standard input is used when it is not a terminal.
<command that prints the value> | scripts/secrets/set-secret.sh openemr-admin-password
scripts/secrets/set-reviewer-password.sh --generate --show | <command that saves it>
```

`--help` on either script lists the options. Exit codes: `0` stored, `1` gcloud or a
precondition failed (nothing stored), `2` usage error or value refused (nothing stored).

**Keep the value out of your shell history.** Never type the value in a command, not even as
`echo '<value>' | ...`: the whole line goes into `~/.bash_history`. Use the prompt, `--generate`, or
a pipe from a program that prints it. The scripts take no value as an argument; an extra argument
is refused without being repeated, and the message tells you to remove it from your history.

### `--generate`

- Source: `/dev/urandom`, filtered with `tr -dc 'A-Za-z0-9'`. Dropping every byte outside the
  alphabet is rejection sampling, so each of the 62 symbols is equally likely. Not bash's
  `$RANDOM` (15 bits from a clock-seeded generator).
- Letters and digits only, so the value survives any quoting in the edge VM's scripts and in
  OpenEMR's setup. 32 characters (about 190 bits) for `set-secret.sh`; 24 (about 143 bits) for
  the reviewer password, which a person may have to type (spike S6 asks for 24 or more).
- `set-secret.sh --generate` never shows the value: nobody needs to know a database password.
- `set-reviewer-password.sh --generate` prints the password on standard output **once, after it
  is stored** (if storing fails, nothing is printed). It refuses to run when standard output is not
  a terminal, because a password printed into a file or pipe you did not mean to use could end up
  on disk; add `--show` when you do mean it (for example to pipe it into a password manager).
  `--show` without `--generate` is refused: a password you typed is never shown.

## Value rules

| Rule | Why |
| --- | --- |
| Not empty | An empty value is never what was meant (an empty pipe, Enter pressed twice); fhir-proxy treats an empty password file as "not configured" and answers 503. |
| No newline (only one newline at the very end of piped input is removed, as `echo` and most programs add one) | The edge VM writes the value into a password file and fhir-proxy strips a trailing newline; a newline inside would be read differently by each. |
| No NUL, no carriage return, no other control character, printable ASCII only (letters, digits, punctuation, space) | Every reader on the path (gcloud, the VM's shell scripts, OpenEMR's PHP setup, a browser form) treats these characters the same way. A carriage return usually means a file with Windows line endings. |
| 16 to 128 characters | The project's minimum for passwords (D9 for the reviewer password; the same for the others). fhir-proxy itself accepts 1 to 1024 characters; 128 keeps the login request well inside its 1 KiB limit even with JSON escaping. |
| Leading and trailing spaces are kept | They are part of the value: `IFS=` stops `read` from trimming them. |

The value is stored **exactly**, with no trailing newline added: `printf '%s'` writes it to
gcloud's standard input (`--data-file=-`). `echo` would add a newline.

## What the scripts guarantee, and how

| Guarantee | How |
| --- | --- |
| Never in a process argument (`ps`, `/proc/<pid>/cmdline`) | The value goes to gcloud through a pipe from bash's **builtin** `printf`, which runs inside bash and starts no program. |
| Never in a child's environment | The value variables are never exported; an inherited variable of the same name is `unset` first, and `allexport` is switched off. |
| Never echoed, never in output | `read -s` turns echo off; messages name the secret and the rule, never the value. gcloud's output goes to standard error. |
| Never on disk | No temporary files, no here-documents or here-strings with the value (bash backs those with temporary files), `umask 077`. |
| Never in `set -x` output | The scripts refuse to run under xtrace (`bash -x`, `SHELLOPTS=xtrace`), before doing anything. |
| No gcloud HTTP or debug log | gcloud runs with `CLOUDSDK_CORE_LOG_HTTP=false`, `CLOUDSDK_CORE_DISABLE_FILE_LOGGING=true` and `--verbosity=warning`, whatever your gcloud configuration says; `--log-http` and `--verbosity` are refused as options. `--log-http` logs request bodies, and the body of `versions add` carries the value (base64-encoded). |
| Cleared on failure | The variables are emptied right after gcloud returns, success or not; any gcloud failure exits non-zero with gcloud's message (which never sees the value). |
| The right secret, or none | Allowed names are fixed; the secret must exist (checked first, by name). `set-secret.sh` stops if the secret already has an enabled version, unless `--replace` (see "Rotation"). |

## Who picks up a new version

| Secret | Reader | When it reads `latest` | What you do after adding a version |
| --- | --- | --- | --- |
| `db-root-password` | edge VM, file `/mnt/disks/edge/secrets/mariadb-root-password` | **once, at boot**, then keeps the file | Before the VM's first boot (runbook step 6): nothing. If the VM already booted without it (its log says `ERROR: secret mariadb-root-password ... 404`): on the VM `sudo google_metadata_script_runner startup`, or reset the VM. |
| `openemr-db-password`, `openemr-admin-password` | edge VM, files of the same name, used by OpenEMR's first-time setup | **once, at boot**, then keeps the files | The same. |
| `reviewer-password` | fhir-proxy (Cloud Run), the secret mounted as a file at version `latest` | when the file is read; the proxy re-reads it every 30 s (`PASSWORD_CACHE_SECONDS`) | Nothing: no restart and no apply. Within about 30 s the new password works, the old one stops, and **every signed-in reviewer is signed out** (the session-token key is derived from the password). |

"Mounted at `latest`" means Cloud Run fetches the newest enabled version when the file is read,
not once at deploy time, which is why the reviewer password needs no redeploy. The edge VM is
different: its startup script copies each secret into a file once and keeps it, because MariaDB
and OpenEMR take these passwords only when they first set themselves up.

## Rotation

**Reviewer password.** Run `set-reviewer-password.sh` again (with or without `--generate`). The
older versions stay enabled; once the new one works, disable them so they cannot be read:

```bash
gcloud secrets versions list <prefix>-reviewer-password --project <project-id>
gcloud secrets versions disable <old version number> --secret <prefix>-reviewer-password --project <project-id>
```

In dev, if the password comes from tfvars (`reviewer_password_wo_version > 0`), rotate it the
Terraform way instead (infra/envs/README.md, "Rotating the reviewer password"); never mix the two.

**Database and OpenEMR admin passwords.** dev is destroyed nightly, secrets included, so each new
dev starts empty and nothing needs rotating. In prod a new version alone changes nothing: the VM
keeps its copy and the database keeps its password. That is why `set-secret.sh` stops when an
enabled version exists. To rotate (not verified, and not automated by these scripts):

1. change the password inside MariaDB (root, or OpenEMR's user) or in OpenEMR (admin);
2. `scripts/secrets/set-secret.sh --replace <short-name>` with the same new value;
3. on the VM, remove the old file in `/mnt/disks/edge/secrets/` and run
   `sudo google_metadata_script_runner startup`;
4. disable the old version as above.

## Checking a stored value without showing it

```bash
gcloud secrets versions access latest --secret <prefix>-<short-name> --project <project-id> | wc -c
```

prints only the number of bytes: it should equal the length of the value (no `+1` for a newline).

## Tests

```bash
make test                        # with the rest of the repository
.venv/bin/pytest scripts/secrets  # only these
```

pytest runs the scripts against a stub `gcloud` written into each test's temporary directory,
which records every call's arguments and environment and saves what it read on standard input.
PATH contains only the stub and links to `bash`, `env`, `dirname`, `tr`, `head` and `cat`, so the
real gcloud cannot be reached; values are fake, generated per test. No credentials are needed.

- `tests/test_set_secret.py`, `tests/test_set_reviewer_password.py`: the exact bytes stored
  (spaces kept, no newline); the value in no gcloud argument or environment, not in output, not in
  any file but the stub's capture, and (with `strace`, Linux only) in no program's argv or
  environment at all; every refused name, option and value; project and prefix; missing secret;
  existing version and `--replace`; gcloud failures; `--generate` (length, alphabet, different each
  time, reads `/dev/urandom`); `--show`; xtrace, allexport and an inherited variable; the hidden
  prompt on a pseudo-terminal (not echoed, typed twice, mismatch refused).
- `tests/test_static.py`: shellcheck clean (also a pre-commit hook); none of the usual bash 4
  constructs; no `$RANDOM`, here-string, `set -x` or export of the value; every place the value is
  expanded is on a reviewed list.

## Not verified (check on the first real run)

1. gcloud stores the bytes from `--data-file=-` unchanged: after the first version, the `wc -c`
   check above shows the value's length.
2. `gcloud secrets versions list ... --filter=state:ENABLED --sort-by=~createTime --limit=1`
   returns the newest enabled version (and nothing for a secret with none), so the "already has an
   enabled version" stop works.
3. No file under `~/.config/gcloud/logs` is written during a run (file logging is switched off),
   and gcloud's error messages never contain the payload.
4. The roles above are enough for a deployer who is not project Owner.
5. macOS: bash 3.2, BSD `tr` and `head -c`. Only bash 5.2 on Linux ran the tests; bash 3.2 is
   covered by a static check of the usual bash 4 constructs, not by a run.
6. The edge VM picks up the database passwords at boot (infra/envs master checklist E10) and
   fhir-proxy the reviewer password within about 30 s (P6).
7. OpenEMR's first-time setup accepts a typed admin or database password with punctuation
   (quotes, `$`, backslash). `--generate` avoids the question.
8. Pressing Ctrl-C at the hidden prompt leaves the terminal echoing (bash restores it; if not,
   type `stty echo` and Enter).
