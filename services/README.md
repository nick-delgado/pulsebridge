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
  declared by name only. `.dockerignore` is an allow-list: only `libs/` and `services/` are sent,
  and it leaves out tests, caches, `.venv`, `.env` files, key files, Terraform state and tfvars
  (`.git`, `.worktrees`, `web/` and `infra/` are never sent).
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
  ones in the service README's "Running" section: one worker, four threads, `--timeout 0`. There
  is one exception, `--bind ":${PORT:-8080}"`, which needs a shell to expand. Instead the image
  sets `PORT=8080`, and when `PORT` is set and no `--bind` is given, gunicorn binds
  `0.0.0.0:$PORT`. The result is the same: Cloud Run's `PORT` is honoured and 8080 is the default.
  The test compares the CMD with the README, so if either changes, both must change.
- `PYTHONUNBUFFERED=1`, so log lines reach Cloud Logging as they are written.
  `GUNICORN_CMD_ARGS=--no-control-socket` turns off gunicorn's local control socket (new in
  gunicorn 25.1). Nothing in a container uses it, and it would log an error on every start
  because the app user has no home directory.
- **No secrets.** The image holds no keys, tokens or `.env` files. On Cloud Run, credentials are
  the service account's own (Application Default Credentials), and secrets are mounted from
  Secret Manager at run time (`health-sync`'s Google Health files).

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
- it refuses to start with bad configuration and says why;
- it runs as UID 10001;
- it holds no stray files (`.git`, tests, key files, pip).

### Build in Cloud Build

`cloudbuild.yaml` has one step in the Docker builder image (pinned by digest). It runs these
steps for every `services/*/Dockerfile` it finds:

1. Build from the repository root for `linux/amd64`. The base image is pulled through the
   project's Docker Hub cache (below).
2. Tag the image twice: with the git tag and with the full commit SHA, as
   `<region>-docker.pkg.dev/<project>/<repository>/<service>:<tag>`. Then push both tags.
3. Read the digest the registry stored. When every service has been built, print the block to
   paste into `infra/envs/<env>/terraform.tfvars`:

```text
----- BEGIN tfvars (v0.1.0, 0123456789abcdef0123456789abcdef01234567) -----
cuff_sim_image = "us-central1-docker.pkg.dev/<project>/pulsebridge-services/cuff-sim@sha256:<64 hex>"
health_sync_image = "us-central1-docker.pkg.dev/<project>/pulsebridge-services/health-sync@sha256:<64 hex>"
v2_to_fhir_image = "us-central1-docker.pkg.dev/<project>/pulsebridge-services/v2-to-fhir@sha256:<64 hex>"
----- END tfvars -----
```

Each variable name is the service directory with `-` turned into `_`, plus `_image`. The value
is the image by digest, without a tag. T20 (`v2_to_fhir_image`, `health_sync_image`), T22
(`cuff_sim_image`, later `fhir_proxy_image`) and T25 should use these names, so the block can be
pasted unchanged. If any service fails, no block is printed: you never get a partial set of
digests. To get the block from a finished build:
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

**Tags are pushed once.** The services repository has immutable tags (`project-base`). A
second build of the same git tag, or of a commit that has already been released, is refused
when it pushes. Release a new tag from a new commit.

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
| `roles/artifactregistry.writer` | the services repository only | push the images |
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
is billed per build-minute. One build of the three images takes a few minutes on the default
`e2-standard-2` worker. That is inside Cloud Build's monthly free allowance (2,500 build-minutes
at the time of writing), and about $0.006 per minute beyond it. These are not confirmed
against the current pricing page.

Storage is Artifact Registry's: each image is about 55 to 58 MB compressed. The three share the
base layers, so a release adds about 100 MB, and the first 0.5 GB is free. Each release keeps
its images, because tags are immutable and nothing deletes them. A cleanup policy on the
repository (keep the last few releases) belongs in `project-base`. It is a suggested follow-up,
because without one, storage grows by about $0.01 a month per release. Pulls inside the region
are free. No private worker pool is used: private pools are billed per minute and are not needed.

### Bumping the base image

1. `docker buildx imagetools inspect python:3.13-slim` (or a newer `3.13.x-slim-<debian>` tag),
   and copy the `Digest:` line. It is the multi-platform index digest.
2. Replace the tag and digest in the two `FROM` lines of **every** `services/*/Dockerfile`, for
   example with `sed -i 's|python:3.13.18-slim-trixie@sha256:[0-9a-f]*|python:<new-tag>@sha256:<new>|' services/*/Dockerfile`.
   `test_every_dockerfile_uses_the_same_base_image` fails if one is missed.
3. Run `make images && make images-smoke`, then open a pull request.

Bump the builder image in `cloudbuild.yaml` the same way. Use
`docker buildx imagetools inspect gcr.io/cloud-builders/docker:<major>` and keep the major
version tag in front of the digest.

### Adding an image (T09 `seed`, T14 `fhir-proxy`)

1. Copy `services/v2-to-fhir/Dockerfile` to `services/<name>/Dockerfile`.
2. Change the `COPY libs/...` lines and the first `pip install` line to the local libraries your
   `pyproject.toml` declares, and `services/v2-to-fhir` to `services/<name>`.
3. Set `CMD` to your README's command in exec form. For a gunicorn service, use the README's
   arguments without `--bind ":${PORT:-8080}"`; the image's `PORT` covers that.
4. Delete the service from `NO_IMAGE_YET` in `tests/tooling/test_container_images.py`, or leave
   it (the Dockerfile is checked either way). Add a case to `tests/tooling/test_image_smoke.py`
   for its route and its bad-configuration error.
5. Do not edit `cloudbuild.yaml`: it builds every `services/*/Dockerfile` it finds, and the tfvars
   block gets a `<name>_image` line by itself.

The static tests fail if the COPY lines do not match the declared libraries, if a library is
installed after the service, or if the CMD differs from the README. Expected results:

- **`fhir-proxy` (T14)**: a Cloud Run service (T22). It most likely needs `libs/pulsebridge_fhir`
  and `libs/pulsebridge_gcp`, but its `pyproject.toml` decides. The CMD is its README's gunicorn
  command. If the login rate limit is kept per process, as `cuff-sim`'s is, that means one
  worker.
- **`seed` (T09)**: needs an image only if it runs on Cloud Run, as a job, for example with
  `use_synthetic_data`. If it does, give it the same Dockerfile, with
  `CMD ["<its console script>", ...]` instead of gunicorn, and no `PORT` (a job serves nothing).
  `test_starts_with_the_readme_gunicorn_command` assumes a web service, so T09 would extend that
  test for jobs. If `seed` is only run from a laptop, leave it on `NO_IMAGE_YET` with that reason.

### Not verified against Google Cloud

T24 made no GCP calls. The images were built and smoke-tested locally. The Cloud Build step's
script was run in the pinned builder image against a local registry, with the real `docker`.
To confirm on the first real build:

- `automapSubstitutions` exposes the built-in `PROJECT_ID`, `TAG_NAME` and `COMMIT_SHA` to the
  `script` as well as the `_` substitutions.
- The step's docker client is already authenticated to `<region>-docker.pkg.dev` for both the
  push and `docker buildx imagetools inspect`.
- The default `MUST_MATCH` substitution check accepts substitutions that only the script uses.
- `gcloud builds submit` accepts `TAG_NAME` and `COMMIT_SHA` as `--substitutions`.
- `roles/artifactregistry.reader` is enough to pull through the remote repository and cache new
  images.
