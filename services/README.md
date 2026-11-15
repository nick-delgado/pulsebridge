# services

Python services deployed to Cloud Run. Each one scales to zero when idle and runs as its own
least-privilege service account (see the `identity` Terraform module).

Planned services (from the project brief; each task that builds one creates its directory):

| Service | What it does |
| --- | --- |
| `health-sync` | Pulls heart-rate data from the Google Health API, maps it to FHIR, writes a transaction Bundle |
| `v2-to-fhir` | Receives HL7v2 store notifications from Pub/Sub, maps ORU^R01 OBX segments to FHIR Observations |
| FHIR proxy | Narrow read API for the dashboard; runs the share action against OpenEMR |
| Cuff simulator | Builds an ORU^R01 from entered blood-pressure values, sends it over MLLP, returns the ACK |

`v2-to-fhir` (task T12) is the first service built and the pattern for the others: a mapping
module with no cloud calls, a thin handler, configuration validated at start-up, and tests that
use the shared client's fake transport. Read its README before starting a new service.

## Layout

```text
services/<name>/
  pyproject.toml      # dependencies, including the shared libs under libs/
  <package>/          # application code
  tests/              # pytest tests for this service only
  Dockerfile          # container image, built from the repository root (see "Container images")
```

## Working with a service

- `make install` installs every `services/*/pyproject.toml` into the shared `.venv` in editable
  mode, alongside the libraries in `libs/`.
- `make test` runs the tests under `services/*/tests` together with the rest of the repository.
- Configuration (project, dataset, store names) comes from environment variables set by Terraform.
  Secrets come from Secret Manager. Nothing specific to one deployment is hardcoded.

## Container images

Each service with a `Dockerfile` becomes one image. Cloud Build (`cloudbuild.yaml`) builds and
pushes them all on a git tag and prints their digests. Terraform deploys each one by digest, so
an apply always runs exactly the image that was built and tested.

### What an image is

Every `services/<name>/Dockerfile` follows the same pattern (`v2-to-fhir`'s is the reference).
`tests/tooling/test_container_images.py` checks the pattern:

- **Built from the repository root.** The build context is `.`, not the service directory,
  because the image installs the shared libraries from `libs/`. They are not on PyPI and are
  declared by name only. `.dockerignore` is an allow-list. It sends exactly what `pip install`
  reads for each library and service: its `pyproject.toml`, its `README.md` (the package's
  `readme`) and the package directories its `pyproject.toml` names. Nothing else is sent:
  `.git`, `.worktrees`, `web/`, `infra/`, tests and examples stay out. A deny-list then keeps
  secret-looking files out even inside those directories: `.env` files, `.envrc`, `.netrc`,
  `.pypirc`, SSH keys, `*.pem`, `*.key`, `*.crt`, `secrets/`, `token*.json`, credential JSON
  files (including a service account key under its console name, `<project>-<12 hex>.json`),
  Terraform state and tfvars, and caches. A new library or service adds its allow-list lines;
  `test_dockerignore_allows_exactly_what_the_images_install` names any that are missing.
- **Two stages.** The `build` stage creates a venv in `/opt/venv` and installs into it:
  first exactly the local libraries the service's `pyproject.toml` declares (and what those
  declare), then the service. Both are non-editable installs: real wheels, not links to the
  source. The `runtime` stage copies only `/opt/venv` from the build stage, so it carries no
  source tree, no tests, no compilers and no pip.
- **Base image pinned by digest**: `python:3.13.18-slim-trixie@sha256:...`, the same digest in
  both stages and in every Dockerfile. A digest names the image's exact bytes. A tag such as
  `3.13-slim` can be moved to a different image, a digest cannot.
- **Non-root.** `USER 10001:10001`, a system user with no shell and no home. Cloud Run and
  Kubernetes' `runAsNonRoot` can only check that a numeric user is non-root. The code is owned by
  root and read-only to that user, so the service cannot rewrite itself.
- **Starts with the README's gunicorn command**, in exec form (`CMD ["gunicorn", ...]`): gunicorn
  is PID 1 and gets Cloud Run's SIGTERM directly, with no shell in between. The arguments are the
  ones in the service README's "Running" section: one worker, four threads, `--timeout 0`,
  `--preload` (see "Start-up failures" below). There
  is one exception, `--bind ":${PORT:-8080}"`, which needs a shell to expand. Instead the image
  sets `PORT=8080`, and when `PORT` is set and no `--bind` is given, gunicorn binds
  `0.0.0.0:$PORT`. The result is the same: Cloud Run's `PORT` is honoured and 8080 is the default.
  The test compares the CMD with the README, so if either changes, both must change.
- `PYTHONUNBUFFERED=1`, so log lines reach Cloud Logging as they are written.
- `GUNICORN_CMD_ARGS="--no-control-socket --graceful-timeout 8"` holds two settings the README
  commands leave at gunicorn's defaults, so the CMD can stay equal to the READMEs:
  - `--no-control-socket` turns off gunicorn's local control socket (new in gunicorn 25.1).
    Nothing in a container uses it, and it would log an error on every start because the app
    user has no home directory.
  - `--graceful-timeout 8`: after SIGTERM, gunicorn lets running requests finish for this long
    before stopping its workers. The default is 30 s, but Cloud Run sends SIGKILL 10 s after
    SIGTERM, so 8 s lets gunicorn exit cleanly first.
- **Limits of the image.** `PORT` must be a port number: an empty `PORT` is not supported
  (gunicorn stops with `'' is not a valid port number`). The image expects a writable `/tmp`,
  where gunicorn's workers keep a heartbeat file. Cloud Run provides one. On a platform with a
  read-only root filesystem and no writable `/tmp`, add `--worker-tmp-dir /dev/shm`.
- **No secrets.** The image holds no keys, tokens or `.env` files. On Cloud Run, credentials are
  the service account's own (Application Default Credentials), and secrets are mounted from
  Secret Manager at run time (`health-sync`'s Google Health files).

### Start-up failures: `--preload`

Each service checks its configuration in its app factory (`create_app()`) and refuses to start
with a bad one (a malformed store id, a fixed-offset time zone, an audit store equal to the
HL7v2 store). Without `--preload`, gunicorn's master opens the listening socket first and only
then forks the worker that calls the factory (gunicorn 26.2.0 `arbiter.py`: `Arbiter.start()`
calls `sock.create_sockets()` before `manage_workers()`). The port is then open for a moment
while the worker crashes, and Cloud Run's default startup probe, a TCP connect to `PORT`, can
pass.

With `--preload` (gunicorn setting `preload_app`, "Load application code before the worker
processes are forked"), `Arbiter.setup()` calls the factory in the master, before `start()`
creates the socket. An exception there is not caught by gunicorn: the master exits with status
1 and the port never opens.

What this guarantees: a start-up configuration error means the container exits without ever
accepting a connection on `PORT`. The revision fails its startup probe and the deploy fails,
instead of a broken revision taking traffic. `make images-smoke` checks this inside each
container for each service's documented bad configuration
(`test_bad_configuration_exits_without_ever_listening`), and checks that good configuration
still opens the port.

What it does not cover: errors that appear only on the first request (a missing IAM role, a
store that does not exist), because the factory makes no request. Not verified until a real
deploy: that Cloud Run marks the revision failed and keeps the previous one serving, and how
long it waits before it does.

**Fork safety.** With one worker, the objects `create_app()` builds in the master are copied
into the worker by the fork. They are: the logging handler, the configuration, the
`HealthcareClient` and its `AuthorizedSession` (no connection is open and no token is fetched
until the first request), health-sync's plain `requests.Session`, its status cache and two
`threading.Lock`s, and cuff-sim's token bucket and its lock. The locks are unlocked, because no
thread runs in the master. No app starts a thread or opens a socket at import or in its
factory. One exception, on Cloud Run only: finding Application Default Credentials asks the
metadata server (`google.auth.default()`) through a session that google-auth creates and drops.
At worst the worker inherits that idle connection, which neither process uses again. This is
not verified on Cloud Run. The master never serves requests, so nothing is used by two
processes.

### Job images (`seed`)

A **Cloud Run job** runs a command to completion and serves nothing; a **Cloud Run service**
is an HTTP server. The seeder (`seed`) is a job: one execution per first seeding or hourly
top-up (its README, "For T20, T22, T24 and T25"). Its image is the same pattern with three
differences, and `tests/tooling/test_container_images.py` tells the two kinds apart by
`JOB_IMAGES` and the README, never by the Dockerfile it is checking:

- **`ENTRYPOINT ["pulsebridge-seed"]`, and no `CMD`.** The program is the console script its
  README's "Running it" commands run (the test reads it from there, as it reads the gunicorn
  command for a service). In Cloud Run a container's `args` replace the image's `CMD` and its
  `command` replaces the `ENTRYPOINT` (Google provider 8.11.0, `google_cloud_run_v2_job`
  `containers.args`: "Arguments to the entrypoint. The docker image's CMD is used if this is
  not provided."; `command`: "The docker image's ENTRYPOINT is used if this is not
  provided."). So **T20 sets only `args`**, for example `["--hours", "4"]`, never `command`,
  and an execution override (Cloud Scheduler's `jobs.run` call) can replace them for one run.
  With no `CMD`, a job created without `args` runs `pulsebridge-seed` alone and exits 2 ("one
  of --days or --hours is required"): a mis-wired job fails visibly instead of succeeding
  without doing anything. `ENTRYPOINT` also clears the base image's `CMD` (`python3`).
- **No `PORT`, no `EXPOSE`, no `GUNICORN_CMD_ARGS`**, and no gunicorn in the image.
- **Signals.** The script installs no SIGTERM handler and Linux applies no default signal
  action to PID 1, so on SIGTERM (task timeout, cancelled execution) the run goes on until the
  platform's SIGKILL. Every write is keyed and each transaction Bundle is all-or-nothing, so
  that is safe; the next run carries on.

The smoke test for a job (`JOB_CASES`): `--help` exits 0; `--dry-run --days 1` with
`PATIENT_ID=SYN-SMOKE-0001` and a time zone prints the counts report and exits 0, makes no
network call (run with `--network none`, and again under a CPython audit hook that records
every `socket.*` operation, even a swallowed one), and leaves no file in the container
(`docker diff` is empty); without arguments it exits 2; a non-`SYN-` `PATIENT_ID` exits 2 and
names the variable. It also gets the checks every image gets: UID 10001, no stray files, code
owned by root, no copy of the pip CA bundle.

The image is about 219 MB uncompressed, about 53 MB gzip-compressed (`docker save | gzip`), a
little smaller than the service images because it has no Flask or gunicorn.

`cuff-sim`'s image also checks at build time that T07's HL7v2 samples (package data of
`pulsebridge_hl7v2`) were installed. `health-sync`'s image also contains the
`health-sync-backfill` command.

### What is pinned and what is not

| Pinned | Not pinned |
| --- | --- |
| Base image (digest) and the Cloud Build builder image (digest) | Transitive Python dependencies (for example Werkzeug, Jinja2, urllib3, cryptography) are resolved from PyPI at build time |
| Direct third-party dependencies (exact `==` pins in each `pyproject.toml`) | The build backends' own dependencies |
| Build backends (`hatchling==1.32.4`, `setuptools==80.9.0`) | No hashes: the repository has no lock file with hashes (`--require-hashes` would need one per service) |
| The local libraries: built from the same commit | |

Each build prints `pip freeze`, so the build log records the exact versions in every image.
After that, the image digest pins everything: Terraform deploys a digest, never a rebuild. A
lock file with hashes per service would close the gap at build time. It is a possible follow-up,
not part of T24.

### Build locally (Docker, no Cloud Build)

```bash
make images         # builds pulsebridge/<service>:local for every services/*/Dockerfile
make images-smoke   # starts each image with fake configuration and probes it (tests/tooling/test_image_smoke.py)
```

Neither target is part of `make check`. They need a Docker daemon and network access to Docker Hub
and PyPI, and they push nothing. Behind a TLS-inspecting proxy, give pip the proxy's CA bundle
as a build secret: `make images IMAGE_PIP_CERT=/path/to/ca-bundle.crt`. A secret mount exists
only for that one `RUN` and is never stored in a layer.

The smoke test starts every image with synthetic configuration and a placeholder credentials file
that no request uses, with `PORT=9123`. For each image it checks four things:

- it listens on that port and answers its own route without calling Google:
  `POST /pubsub/hl7v2` with an empty envelope gets 400, `GET /status` reports `not-configured`,
  and `GET /cuff/samples` lists the samples;
- it refuses to start with bad configuration and says why, and the port never opens: a probe
  inside the container connects to `PORT` every 10 ms from start to exit (a port published to
  the host would be accepted by Docker's proxy either way);
- it runs as UID 10001, and nothing under `/opt/venv` is owned by anyone but root or writable by
  that user;
- gunicorn's effective settings (`gunicorn --print-config` with the image's CMD) are the
  README's, with `bind = 0.0.0.0:8080`, `graceful_timeout = 8`, the control socket off and
  `preload_app = True`;
- it holds no stray files (`.git`, tests, key and certificate files, pip);
- with `make images-smoke IMAGE_PIP_CERT=<the same file>`, no file in the image has that CA
  bundle's SHA-256, which proves the build secret was not copied into a layer.

### Build in Cloud Build

`cloudbuild.yaml` has one step in the Docker builder image (pinned by digest). It works on
every `services/*/Dockerfile` it finds, in phases:

1. **Check.** The tag, the commit SHA, the substitutions and every service directory name: lower
   case words joined by `-` (`^[a-z0-9]+(-[a-z0-9]+)*$`), because the name becomes the image
   name and the tfvars variable name. Nothing is built if any check fails.
2. **Plan.** For each service, ask the registry whether `<image>:<commit SHA>` already exists,
   for example from an earlier run that failed part-way (below). An existing image is reused,
   not rebuilt.
3. **Build every image that does not exist yet**, from the repository root for `linux/amd64`,
   tagged with the full commit SHA and the git tag. The base image is pulled through the
   project's Docker Hub cache (below). Nothing is pushed in this phase.
4. **Push**: the commit tag first, then the git tag. A reused image that lacks the git tag gets
   it in the registry, as a copy of the same manifest, so its digest does not change.
5. **Report.** Read each digest the registry stored, check that both tags name the same image,
   and print the block to paste into `infra/envs/<env>/terraform.tfvars`:

```text
----- BEGIN tfvars (v0.1.0, 0123456789abcdef0123456789abcdef01234567) -----
cuff_sim_image = "us-central1-docker.pkg.dev/<project>/pulsebridge-services/cuff-sim@sha256:<64 hex>"
health_sync_image = "us-central1-docker.pkg.dev/<project>/pulsebridge-services/health-sync@sha256:<64 hex>"
seed_image = "us-central1-docker.pkg.dev/<project>/pulsebridge-services/seed@sha256:<64 hex>"
v2_to_fhir_image = "us-central1-docker.pkg.dev/<project>/pulsebridge-services/v2-to-fhir@sha256:<64 hex>"
----- END tfvars -----
```

Each variable name is the service directory with `-` turned into `_`, plus `_image`. The value
is the image by digest, without a tag. T20 (`v2_to_fhir_image`, `health_sync_image`, and
`seed_image` for the seeder's Cloud Run job, created only with `use_synthetic_data`), T22
(`cuff_sim_image`, later `fhir_proxy_image`) and T25 should use these names, so the block can be
pasted unchanged. The block is printed only by a build that succeeded, and then it lists
every service, each digest read back from the registry. A failed build prints no block. It may
still have pushed some images (see "A failed release"), so do not take digests from the
`=== <service>` lines of a failed build. To get the block from a finished build:
`gcloud builds log <BUILD_ID> | sed -n '/BEGIN tfvars/,/END tfvars/p'`.

| Substitution | Default | Meaning |
| --- | --- | --- |
| `PROJECT_ID` | built in | the build's own project |
| `TAG_NAME`, `COMMIT_SHA` | set by a tag trigger | the tag names the release; the build refuses to run without both |
| `_REGION` | `us-central1` | region of the Artifact Registry repositories (`project-base` `region`) |
| `_REPOSITORY` | `pulsebridge-services` | the services repository (`project-base` `docker_repository_id`) |
| `_DOCKERHUB_MIRROR` | `pulsebridge-dockerhub` | the Docker Hub remote repository (`project-base` `dockerhub_mirror_repository_id`); empty means pull from Docker Hub directly (`create_dockerhub_mirror = false`) |

The defaults are the `project-base` module's own defaults, not values from any deployment.
Override them on the trigger or with `--substitutions`. A manual build from a clean checkout:

```bash
gcloud builds submit --config cloudbuild.yaml \
  --substitutions TAG_NAME=v0.1.0,COMMIT_SHA="$(git rev-parse HEAD)" .
```

A manual `gcloud builds submit` uploads the source to a Cloud Storage bucket
(`gs://<project>_cloudbuild` by default) and the build reads it from there. With a
user-specified build service account, that account probably also needs read access to that
bucket (for example `roles/storage.objectViewer` on it). This is not verified; it is a note for
T26. A trigger reads the source from GitHub and does not need it.

**Tags are pushed once.** The services repository has immutable tags (`project-base`): a
pushed tag can never be moved to another image. The phases above are arranged around that:

- **A build failure** (a typo, a PyPI outage) happens before any push, so nothing is pushed. Fix
  the cause and run the same tag again.
- **A failed release** is a build that stops during the pushes, for example on a registry or
  network error. Some services may then have both tags, one may have only its commit tag, and
  the rest have nothing. Recover by running the same build again (Cloud Build's "Retry", or
  the same tag and commit). It finds each image that already has its commit tag and reuses it.
  Immutable tags guarantee that this image is the one an earlier run built from that commit.
  The re-run adds any missing git tag without a rebuild, builds and pushes the rest, and prints
  the full block. The log says `already exists ... reusing it` for each reused image.
- **A new git tag on a commit that was already released** reuses that commit's images and only
  adds the new tag; nothing is rebuilt and the digests are the same. The images'
  `org.opencontainers.image.version` label keeps the first tag they were built for.
- The build **refuses** two situations it cannot have made itself: a git tag with no commit
  tag for this commit, and a git tag and commit tag that name different images.
- A registry lookup that fails for a reason other than "no such tag" (a permission or network
  error) reads as "not there". The build then rebuilds and pushes, and the immutable tag refuses
  the push if the tag did exist, so nothing is replaced; the build fails instead.

**Where the base image comes from.** Cloud Build pulls it through the project's Docker Hub
remote repository (an Artifact Registry pull-through cache), not straight from Docker Hub:

- Docker Hub rate-limits anonymous pulls, and Cloud Build workers share their IP addresses.
- The cache keeps a copy if Docker Hub is slow, down, or the tag is deleted.
- The cached image has the same digest as the original, so the Dockerfiles' pin still holds.
- Only the registry part changes (`PYTHON_REGISTRY`). The tag and digest stay in the Dockerfile.

The builder image `gcr.io/cloud-builders/docker` comes from Google's own registry, which Cloud
Build always reaches.

### Who runs builds: the build service account

Builds run as a **dedicated build service account**, not the Compute Engine default account and
not the `terraform-deployer` (which has far more power than a build needs). It needs:

| Role | On | Why |
| --- | --- | --- |
| `roles/artifactregistry.writer` | the services repository only | push the images, read existing tags (plan phase) and add a tag to an existing image |
| `roles/artifactregistry.reader` | the Docker Hub remote repository only | pull (and cache) the base image |
| `roles/logging.logWriter` | the project | write the build log (`options.logging: CLOUD_LOGGING_ONLY`) |

It needs no Secret Manager, Healthcare, Cloud Run or IAM roles. It deploys nothing: the deploy is
a Terraform apply, as the deployer, with the digests from the log.

**The trigger is not part of T24.** It would be a `google_cloudbuild_trigger` on tags matching
`^v[0-9]+\.[0-9]+\.[0-9]+$`, with `filename = "cloudbuild.yaml"` and `service_account` set to
the build account. It needs a one-time manual step: connecting the GitHub repository to Cloud
Build through the Cloud Build GitHub App. The natural owner is T26 (Terraform CI/CD), with the
build service account added to the `identity` module. An alternative that skips the Cloud Build
GitHub connection is a GitHub Actions workflow on tag pushes. It would authenticate with the
existing Workload Identity Federation and run `gcloud builds submit`. The deployer would then
also need `roles/cloudbuild.builds.editor` and `roles/iam.serviceAccountUser` on the build
account.

### Cost

Nothing here is always on. A build runs only when a tag is pushed (or you start one by hand) and
is billed per build-minute. One build of the four images takes a few minutes on the default
`e2-standard-2` worker. That is inside Cloud Build's monthly free allowance (2,500 build-minutes
at the time of writing), and about $0.006 per minute beyond it. These are not confirmed
against the current pricing page.

Storage is Artifact Registry's: each image is about 53 to 58 MB compressed. The four share the
base layers, so a release adds about 100 MB, and the first 0.5 GB is free. Each release keeps
its images, because tags are immutable and nothing deletes them. A cleanup policy on the
repository (keep the last few releases) belongs in `project-base`. It is a suggested follow-up,
because without one, storage grows by about $0.01 a month per release. Pulls inside the region
are free. No private worker pool is used: private pools are billed per minute and are not needed.

### Bumping the base image

1. Pick the exact tag you will write, for example `3.13.19-slim-trixie`, and inspect that
   tag: `docker buildx imagetools inspect python:3.13.19-slim-trixie`. Copy the `Digest:` line
   (the multi-platform index digest). Inspect the same tag you write, so the tag shown in the
   Dockerfile is the image the digest names.
2. Replace the tag and digest in the two `FROM` lines of **every** `services/*/Dockerfile`, for
   example with `sed -i 's|python:3.13.18-slim-trixie@sha256:[0-9a-f]*|python:3.13.19-slim-trixie@sha256:<digest>|' services/*/Dockerfile`.
   `test_every_dockerfile_uses_the_same_base_image` fails if one is missed.
3. Run `make images && make images-smoke`, then open a pull request.

Bump the builder image in `cloudbuild.yaml` the same way. Use
`docker buildx imagetools inspect gcr.io/cloud-builders/docker:<major>` and keep the major
version tag in front of the digest.

### Adding an image (T14 `fhir-proxy`; `seed` was added by T35)

1. Copy `services/v2-to-fhir/Dockerfile` to `services/<name>/Dockerfile`. The directory name
   must be lower-case words joined by `-` (the build checks it).
2. Change the `COPY libs/...` lines and the first `pip install` line to the local libraries your
   `pyproject.toml` declares, and `services/v2-to-fhir` to `services/<name>`.
3. Set `CMD` to your README's command in exec form. For a gunicorn service, use the README's
   arguments without `--bind ":${PORT:-8080}"`; the image's `PORT` covers that. For a Cloud
   Run job, follow "Job images" instead (`seed`'s Dockerfile) and add it to `JOB_IMAGES`.
4. Delete the service from `NO_IMAGE_YET` in `tests/tooling/test_container_images.py`, or leave
   it (the Dockerfile is checked either way). Add a case to `tests/tooling/test_image_smoke.py`
   for its route and its bad-configuration error.
5. Add the service's (and any new library's) `pyproject.toml`, `README.md` and package
   directories to the allow-list in `.dockerignore`.
6. Do not edit `cloudbuild.yaml`: it builds every `services/*/Dockerfile` it finds, and the tfvars
   block gets a `<name>_image` line by itself.

The static tests fail if the COPY lines do not match the declared libraries, if a library is
installed after the service, or if the CMD differs from the README. Expected results:

- **`fhir-proxy` (T14)**: a Cloud Run service (T22). It most likely needs `libs/pulsebridge_fhir`
  and `libs/pulsebridge_gcp`, but its `pyproject.toml` decides. The CMD is its README's gunicorn
  command. If the login rate limit is kept per process, as `cuff-sim`'s is, that means one
  worker.
- **`seed`**: built by T35 as a job image; see "Job images".

### Not verified against Google Cloud

T24 made no GCP calls. The images were built and smoke-tested locally. The Cloud Build step's
script was run in the pinned builder image against a local registry, with the real `docker`:
a first run, a re-run of the same tag (every image reused, nothing rebuilt) and a new tag on
the same commit (tag added, same digests). The local registry does not enforce immutable tags,
so refused pushes and recovery from a failed push were checked with the fake docker in
`tests/tooling/test_cloudbuild.py`, not against Artifact Registry. To confirm on the first real
build:

- `automapSubstitutions` exposes the built-in `PROJECT_ID`, `TAG_NAME` and `COMMIT_SHA` to the
  `script` as well as the `_` substitutions.
- The step's docker client is already authenticated to `<region>-docker.pkg.dev` for both the
  push and `docker buildx imagetools inspect`.
- The default `MUST_MATCH` substitution check accepts substitutions that only the script uses.
- `gcloud builds submit` accepts `TAG_NAME` and `COMMIT_SHA` as `--substitutions`.
- `roles/artifactregistry.reader` is enough to pull through the remote repository and cache new
  images.
- Artifact Registry accepts `docker buildx imagetools create --prefer-index=false` (adding a tag
  to an existing manifest) on a repository with immutable tags, and keeps the digest.
- A manual `gcloud builds submit` with a user-specified build account needs read access to the
  source bucket.
