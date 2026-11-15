# Container image for seed, a Cloud Run JOB (not a service). Build from the REPOSITORY ROOT, so
# libs/ is in the build context:
#
#   docker build -f services/seed/Dockerfile -t pulsebridge/seed:local .
#   docker run --rm -e PATIENT_ID=SYN-DEMO-0001 -e TIME_ZONE=America/New_York \
#       pulsebridge/seed:local --days 1 --dry-run
#
# services/README.md ("Container images", "Job images") explains every choice below. It is the
# service pattern (v2-to-fhir's), with a job's command instead of gunicorn;
# tests/tooling/test_container_images.py checks it.

# Base image: Python 3.13 on Debian slim, pinned by digest (the digest wins; the tag is for
# readers). PYTHON_REGISTRY is the only part a build may change: Cloud Build passes the project's
# Docker Hub pull-through cache (an Artifact Registry remote repository), which serves the same
# digest. To bump, see services/README.md ("Bumping the base image").
ARG PYTHON_REGISTRY=docker.io

# ---- Stage 1: build. Installs the libraries and the service into a virtual environment. ------
FROM ${PYTHON_REGISTRY}/library/python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

# The venv has no pip of its own: the base image's pip installs into it (`pip --python`), so the
# runtime image carries no installer.
RUN ["python", "-m", "venv", "--without-pip", "/opt/venv"]

WORKDIR /src
# Exactly the local libraries this service declares (pyproject.toml), then the service. They are
# declared by name only and are not on PyPI, so they must be installed from these paths first.
COPY libs/pulsebridge_fhir libs/pulsebridge_fhir
COPY libs/pulsebridge_gcp libs/pulsebridge_gcp
COPY services/seed services/seed

# Non-editable installs (wheels built from these paths). The libraries go first, so the service's
# install finds them already present instead of looking them up on PyPI. `pip freeze` puts the
# resolved versions in the build log (transitive dependencies are not pinned; see the README).
# The optional `pip_cert` build secret is a CA bundle for builds behind a TLS-inspecting proxy; a
# secret mount is never stored in a layer.
RUN --mount=type=secret,id=pip_cert \
    if [ -s /run/secrets/pip_cert ]; then export PIP_CERT=/run/secrets/pip_cert; fi; \
    python -m pip --python /opt/venv/bin/python install \
        ./libs/pulsebridge_fhir ./libs/pulsebridge_gcp \
    && python -m pip --python /opt/venv/bin/python install ./services/seed \
    && python -m pip --python /opt/venv/bin/python freeze

# ---- Stage 2: runtime. The venv only: no source tree, no tests, no build tools, no pip. -------
FROM ${PYTHON_REGISTRY}/library/python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS runtime

# A system user with a fixed numeric UID/GID. Cloud Run (and Kubernetes' runAsNonRoot) can only
# verify a numeric USER, and a name would mean nothing outside this image's /etc/passwd.
RUN ["groupadd", "--system", "--gid", "10001", "app"]
RUN ["useradd", "--system", "--uid", "10001", "--gid", "10001", "--no-create-home", "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin", "app"]
# The installer is not needed at run time.
RUN ["python", "-m", "pip", "uninstall", "--yes", "--quiet", "--root-user-action=ignore", "pip"]

# Owned by root and not writable by the app user: the running job cannot change its own code.
COPY --from=build /opt/venv /opt/venv

# PYTHONUNBUFFERED: log lines (JSON, on stderr) and the report (JSON, on stdout) reach Cloud
# Logging as they are written. A job serves nothing, so there is no PORT, no EXPOSE and no
# gunicorn setting.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 10001:10001

# The command is the pulsebridge-seed console script, in exec form (PID 1, no shell). It is the
# ENTRYPOINT, and the arguments come from the job: in Cloud Run a
# container's `args` replace the image's CMD and its `command` replaces the ENTRYPOINT
# (google_cloud_run_v2_job containers.args: "Arguments to the entrypoint. The docker image's
# CMD is used if this is not provided."). So Terraform sets only `args`, for example
# ["--hours", "4"], and an execution override can change them.
# There is deliberately no CMD: a job created without args runs `pulsebridge-seed` alone,
# which exits 2 (one of --days or --hours is required), so a mis-wired job fails visibly
# instead of succeeding without doing anything. Setting ENTRYPOINT also clears the base image's
# CMD ("python3"). `docker run <image> --help` shows the flags.
# Signals: PID 1 gets no default action for SIGTERM, so the script installs its own handler
# (cli.py). On SIGTERM (a task timeout, a cancelled execution) it stops at once, logs its
# seed-run line with outcome "terminated" and the counts so far, and exits 143, well inside
# Cloud Run's 10 s before SIGKILL. Each transaction Bundle is all-or-nothing and every write is
# keyed, so a stopped run leaves whole Bundles. A SIGKILL alone (no SIGTERM first, or a run
# that ignores it) still leaves no seed-run line, and nothing alerts on it.
ENTRYPOINT ["pulsebridge-seed"]
