# scripts/secrets

Two scripts that put secret values into Google Secret Manager after `terraform apply` (tasks T27
and T44):

| Script | Writes | Read by |
| --- | --- | --- |
| `set-secret.sh <short-name>` | `<prefix>-db-root-password`, `<prefix>-openemr-db-password`, `<prefix>-openemr-admin-password` | the edge VM, into password files for MariaDB and OpenEMR |
| `set-session-signing-key.sh` | `<prefix>-session-signing-key` | fhir-proxy, which signs and checks the public demo's `__session` cookie with it (D16) |

`set-session-signing-key.sh` replaced `set-reviewer-password.sh` (T44): D16 removed the shared
reviewer password, and the signing key is random bytes nobody types or sees.

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
| `session-signing-key` | **`set-session-signing-key.sh`** (you); generated, never shown |
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
  `secret_name_prefix` (module identity's `name_prefix`, the env variable `identity_name_prefix`,
  `pb` by default). From the repository root:

```bash
export CLOUDSDK_CORE_PROJECT=<project-id>
export SECRET_NAME_PREFIX="$(terraform -chdir=infra/envs/<dev|prod> output -raw secret_name_prefix)"
```

Each comes from the first of these that is set; an option given an empty value (`--project=`)
is refused rather than replaced by the environment:

| Setting | 1st | 2nd | 3rd |
| --- | --- | --- | --- |
| project | `--project <project-id>` | `CLOUDSDK_CORE_PROJECT` | `GCP_PROJECT_ID` |
| prefix | `--prefix <prefix>` | `SECRET_NAME_PREFIX` | `PULSEBRIDGE_NAME_PREFIX` |

The third column is what the env root's `setup_env` output exports for the connect script, so a
shell that sourced it needs nothing more. Without Terraform outputs at hand, `--prefix` takes the
`identity_name_prefix` from your tfvars (`pb` if you did not set it).

## Commands

```bash
# Database passwords nobody needs to type: generate them (32 random letters and digits, not shown).
scripts/secrets/set-secret.sh --generate db-root-password
scripts/secrets/set-secret.sh --generate openemr-db-password

# OpenEMR's "admin" login: type your own (hidden prompt, typed twice) if you will log in to
# OpenEMR, or generate it (then see "Reading a generated admin password back").
scripts/secrets/set-secret.sh openemr-admin-password

# The demo's session signing key: 32 random bytes, generated and stored, never shown.
# Run it once before the first full apply (Cloud Run will not deploy fhir-proxy without it).
scripts/secrets/set-session-signing-key.sh
# Later, to replace it (ends every open demo session):
scripts/secrets/set-session-signing-key.sh --rotate

# From a password manager or another program: standard input is used when it is not a terminal.
<command that prints the value> | scripts/secrets/set-secret.sh openemr-admin-password
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
  OpenEMR's setup. 32 characters (about 190 bits).
- `set-secret.sh --generate` never shows the value: nobody needs to know a database password.

### The session signing key

- **What it is.** fhir-proxy signs each demo session token (an HS256 JWT, the `__session`
  cookie) with this key and checks the signature on every request. Whoever holds the key can mint
  sessions, so it is a secret; nobody needs to know it.
- **How it is made.** `head -c 32 /dev/urandom` piped straight into
  `gcloud secrets versions add --data-file=-`: the 32 bytes (256 bits, the length RFC 7518
  section 3.2 asks for an HS256 key) are never in a shell variable, a file, an argument or the
  terminal. They are **raw bytes**, stored as they are: no hex or base64, no newline. A separate
  read first checks that the random source gives 32 bytes (only the count is kept).
- **Run twice by mistake?** If the secret already has an enabled version the script stops with
  exit code 0 and writes nothing; `--rotate` is the only way to replace a key, because a new key
  ends every open session.
- **Not here:** `--generate`, `--show` and typed values. There is nothing to choose and nothing
  to show.

## Value rules

**Use `--generate` for the three edge-VM passwords** unless you need to type one: generated
values are letters and digits, which every reader keeps as they are.

Rules for every value:

| Rule | Why |
| --- | --- |
| Not empty | An empty value is never what was meant (an empty pipe, Enter pressed twice). |
| No newline (only one newline at the very end of piped input is removed, as `echo` and most programs add one) | The edge VM writes the value into a password file; a newline inside would be read differently by each reader. |
| No NUL, carriage return or other control character; printable ASCII only | Nothing invisible reaches a password file, and the password can be typed on any keyboard. A carriage return usually means a file with Windows line endings. |
| 16 to 128 characters | The project's minimum for passwords (set with D9; kept for the edge VM's three). |

The readers do **not** all treat punctuation the same way, so each script adds its own rule:

| Script, secrets | Allowed | Refused, and why |
| --- | --- | --- |
| `set-secret.sh`: `db-root-password`, `openemr-db-password`, `openemr-admin-password` | letters, digits and `!#$%&()*+,-./:;<>?@[]^_{|}~`, not starting with `-` | **space** and **`=`**: OpenEMR's image passes all three passwords to its first-time setup in one string, `server=... rootpass=<root> ... pass=<db> ... iuserpass=<admin>`, splits it on whitespace (`read -r -a`), and `auto_configure.php` keeps only the part after the first `=` of each piece. A space splits the password; an `=` cuts it short, so the database would get a different password from the file the hooks log in with. **`'`** and **`\`**: the installer writes `$pass = '<db password>';` into `sites/default/sqlconf.php` without escaping, and in PHP a single quote ends that string and a backslash can escape the closing quote. **`"`** and **`` ` ``**: nothing found breaks on them, but one more script that puts the value inside double quotes or a command substitution would. A **leading `-`**: MariaDB's entrypoint passes the root password through `echo`, which takes a value made only of `-` and the letters `n`, `e`, `E` as an option. |

How the allowed punctuation was checked (by reading the code, not by running OpenEMR): the edge
VM's startup script and the openemr hooks only ever expand the values inside double quotes or pass
them by environment name (`--env NAME`, `MYSQL_PWD`); the image's scripts never `eval` them and use
them only as quoted arguments (`--password="${...}"`), and `read -a` does not expand globs, so `*`,
`?` and `[` stay literal; MariaDB's entrypoint escapes the root password for its SQL; OpenEMR's
installer escapes the passwords for SQL with `mysqli_real_escape_string`, and inside the
single-quoted `sqlconf.php` string `$`, `#`, `&`, `;`, `|`, `<`, `>`, `*`, `?` and `!` mean
nothing. The sources read were the image's `openemr.sh` and `devtoolsLibrary.source` of the 8.1.1
image, OpenEMR's `Installer.class.php` from its main branch, and MariaDB's 11.8 entrypoint; the
pinned OpenEMR image is 8.5.0, so this is **not verified** for that exact image (see the list at
the end).

The value is stored **exactly**, with no trailing newline added: `printf '%s'` writes it to
gcloud's standard input (`--data-file=-`). `echo` would add a newline.

## What the scripts guarantee, and how

| Guarantee | How |
| --- | --- |
| Never in a process argument (`ps`, `/proc/<pid>/cmdline`) | The value goes to gcloud through a pipe from bash's **builtin** `printf`, which runs inside bash and starts no program. |
| Never in a child's environment | The value variables are never exported; an inherited variable of the same name is `unset` first, and `allexport` is switched off. |
| Never read by the wrong command | In pipe mode standard input carries the value, so the existence and version checks (`gcloud secrets describe`, `versions list`) run with `--quiet` and standard input from `/dev/null`; only `versions add` reads it. |
| Never repeated in a message | An argument is shown in an error message only if it looks like a name (lowercase letters, digits and hyphens, at most 40); anything else, which may be a value typed in the wrong place, is reported as "not shown". |
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
| `session-signing-key` | fhir-proxy (Cloud Run), the secret mounted as a file at version `latest` | when the proxy reads the file (how often is fhir-proxy's, T43) | Nothing: no restart and no apply. Once the proxy has read the new key, **every open demo session ends** (its token was signed with the old key). The first version must exist **before** fhir-proxy is deployed: Cloud Run refuses to deploy a service whose mounted secret has no version (infra/envs/README.md, checklist A8). |

"Mounted at `latest`" means Cloud Run fetches the newest enabled version when the file is read,
not once at deploy time, which is why a new signing key needs no redeploy. The edge VM is
different: its startup script copies each secret into a file once and keeps it, because MariaDB
and OpenEMR take these passwords only when they first set themselves up.

## Rotation

**Session signing key.** `set-session-signing-key.sh --rotate`. The older versions stay enabled
but unused (fhir-proxy reads `latest`); disable them when you like:

```bash
gcloud secrets versions list <prefix>-session-signing-key --project <project-id>
gcloud secrets versions disable <old version number> --secret <prefix>-session-signing-key --project <project-id>
```

**Database and OpenEMR admin passwords.** dev is destroyed nightly, secrets included, so each new
dev starts empty and nothing needs rotating. In prod a new version alone changes nothing: the VM
keeps its copy of each file, MariaDB and OpenEMR keep the password they were set up with, and the
OpenEMR container keeps the passwords it was started with in its environment (the container is
replaced only when its configuration hash changes, and the passwords are not part of that hash).
That is why `set-secret.sh` stops when an enabled version exists. None of the steps below is
automated or **verified**; take a snapshot of the data disk first (prod keeps a daily one).

`openemr-db-password` (OpenEMR's own MariaDB user, the openemr module's `db_user`, `openemr` by
default):

1. in MariaDB, as root: `ALTER USER '<db_user>'@'%' IDENTIFIED BY '<new password>';` (for example
   `sudo docker exec -it -e MYSQL_HISTFILE=/dev/null mariadb mariadb -uroot -p`, typing the root
   password at its prompt; `MYSQL_HISTFILE=/dev/null` keeps the statement, and with it the new
   password, out of the client's history file);
2. in OpenEMR's site configuration on the data disk,
   `/mnt/disks/edge/openemr/default/sqlconf.php` (mounted as `sites/` in the container), set
   `$pass = '<new password>';`: OpenEMR reads its database password from that file, not from the
   secret;
3. `scripts/secrets/set-secret.sh --replace openemr-db-password` with the same new value;
4. on the VM: `sudo rm /mnt/disks/edge/secrets/openemr-db-password`, then stop and remove the
   container (`sudo docker stop openemr && sudo docker rm -v openemr`) and run
   `sudo google_metadata_script_runner startup`, which copies the new version and creates the
   container again with it (the site is already set up, so the first-time setup does not run);
5. disable the old version as above.

`db-root-password` (MariaDB root): the same, with `ALTER USER 'root'@'%' IDENTIFIED BY ...` and
`ALTER USER 'root'@'localhost' IDENTIFIED BY ...` in step 1, no `sqlconf.php` change (OpenEMR does
not store the root password), and the file `mariadb-root-password` in step 4. Remove and recreate
the `openemr` container too: it holds the root password in its environment and uses it on every
start (its wait for the database runs `mysqladmin ping` as root).

`openemr-admin-password`: change it in OpenEMR itself (the admin user's password screen, or
OpenEMR's own password-reset tooling), then `--replace` with the same value so the secret stays
the record of it. OpenEMR uses the secret's value only in its first-time setup.

## Reading a generated admin password back

`set-secret.sh --generate openemr-admin-password` never shows the password, and it is the only
login to OpenEMR's own screens (user `admin`). OpenEMR is not reachable from the internet (D13),
so you need it only for work on the VM. Two ways to read it when you do:

- on the edge VM, as root: the file `/mnt/disks/edge/secrets/openemr-admin-password` (the edge-vm
  module's `secrets_dir`, mode 600), for example `sudo cat` it into a password manager;
- or, as a deployer allowed to read secret payloads (`roles/secretmanager.secretAccessor` on that
  secret, which no role in Terraform grants a person):
  `gcloud secrets versions access latest --secret <prefix>-openemr-admin-password --project <project-id>`.

Both print the password on your terminal. If you expect to log in to OpenEMR, type the password
yourself instead of generating it.

## What these scripts trust

They run in your shell, with your account, so they trust what you give them:

- **`PATH`**: `gcloud`, `tr`, `head`, `cat` and `dirname` are whichever ones `PATH` finds first. A
  fake `gcloud` earlier on `PATH` would receive the value.
- **Exported shell functions** (`BASH_FUNC_*` in the environment): bash imports them before the
  script starts, and a function named `gcloud` would run instead of the real one. The scripts call
  `printf` and `read` as builtins (`builtin printf`), but they do not clear inherited functions.
- **`BASH_ENV`**: a non-interactive bash runs the file it names before the script, with full
  access to everything that follows.
- **Your gcloud account and configuration**: the account that writes the version, and the
  credentials gcloud finds. The scripts override only the logging and verbosity settings.

Run them from your own terminal, not from a shell or CI job whose environment someone else
controls.

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
PATH contains only the stub and links to `bash`, `env`, `dirname`, `tr`, `head`, `cat` and `wc`, so the
real gcloud cannot be reached; values are fake, generated per test. No credentials are needed.

- `tests/test_set_session_signing_key.py`: exactly 32 bytes stored in
  `<prefix>-session-signing-key` through `--data-file=-`, different on each run and not a
  printable-only or constant value; the key (raw, hex or base64) in no output, gcloud argument,
  gcloud environment or file but the stub's capture; describe, then the versions check, then add,
  with nothing on the checks' standard input; an existing key kept without `--rotate` (exit 0,
  nothing written) and replaced with it; a missing secret, an unreadable version list and a failed
  add write nothing or report it; every refused argument and option; xtrace; the setup_env names;
  gcloud's HTTP log off.
- `tests/test_set_secret.py`: the exact bytes stored (no newline); each character
  `set-secret.sh` refuses, for each of its three secrets; every allowed punctuation character stored; arguments repeated only when they look like
  names; the existence and version checks reading nothing from standard input; the project and
  prefix fallbacks and an empty `--project=` or `--prefix=`; Ctrl-D at the prompt; the value in no gcloud argument or environment, not in output, not in
  any file but the stub's capture, and (with `strace`, Linux only) in no program's argv or
  environment at all; every refused name, option and value; project and prefix; missing secret;
  existing version and `--replace`; gcloud failures; `--generate` (length, alphabet, different each
  time, reads `/dev/urandom`); xtrace, allexport and an inherited variable; the hidden
  prompt on a pseudo-terminal (not echoed, typed twice, mismatch refused).
- `tests/test_static.py`: shellcheck clean (also a pre-commit hook); none of the usual bash 4
  constructs; no `$RANDOM`, here-string, `set -x` or export of the value; every place the value is
  expanded is on a reviewed list; the generator is exactly `tr -dc` over `/dev/urandom`; the
  signing key's only stored read of `/dev/urandom` is piped straight into `secrets versions add`
  and nothing captures, encodes or copies it.

## Not verified (check on the first real run)

Settled on the first dev deploy (16 November 2026, synthetic data):

- For a secret with **no** versions, `gcloud secrets versions list ... --filter=state:ENABLED`
  prints nothing on standard output but a warning about the filter ("filter keys were not present
  in any resource") on standard error. The first run read that warning as an enabled version and
  refused to write; `lib.sh` now takes only standard output, and only lines that look like a
  version (two tests). The three edge-VM passwords were then stored with `--generate` as version 1.
- The edge VM picked up the three passwords at its first boot (master checklist E10), and
  OpenEMR's first-time setup accepted the generated admin password (O19).

Still open:

1. gcloud stores the bytes from `--data-file=-` unchanged, with no trailing newline: the `wc -c`
   check above was **not run** on the dev deploy. The VM and OpenEMR accepting the generated
   values shows they worked, not that no byte was added.
2. `--filter=state:ENABLED --sort-by=~createTime --limit=1` returns the **newest** enabled version
   when there are several (the dev run only ever had one), so the "already has an enabled
   version" stop works.
3. No file under `~/.config/gcloud/logs` is written during a run (file logging is switched off),
   and gcloud's error messages never contain the payload.
4. The roles above are enough for a deployer who is not project Owner.
5. macOS: bash 3.2, BSD `tr` and `head -c`. Only bash 5.2 on Linux ran the tests; bash 3.2 is
   covered by a static check of the usual bash 4 constructs, not by a run.
6. fhir-proxy picks up a new signing key without a redeploy (P6 confirmed this for the reviewer
   password's file; the key's re-read interval is fhir-proxy's, T43). gcloud stores a binary
   payload from `--data-file=-` byte for byte (`... access latest ... | wc -c` prints 32).
7. The pinned OpenEMR image (8.5.0) passes the allowed punctuation through its first-time setup
   unchanged, as the 8.1.1 scripts and the installer's main branch do (read, not run); for example
   log in with a typed admin password containing `!#$%&;|<>*?` once. `--generate` avoids the
   question (the dev run generated it). The rotation steps above are untested.
8. Pressing Ctrl-C at the hidden prompt leaves the terminal echoing (bash restores it; if not,
   type `stty echo` and Enter).
