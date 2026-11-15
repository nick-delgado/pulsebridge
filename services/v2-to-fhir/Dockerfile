# Container image for v2-to-fhir (Cloud Run). Build from the REPOSITORY ROOT, so libs/ is in the
# build context:
#
#   docker build -f services/v2-to-fhir/Dockerfile -t pulsebridge/v2-to-fhir:local .
#
# services/README.md ("Container images") explains every choice below; this file is the pattern
# every service image follows, and tests/tooling/test_container_images.py checks it.

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
COPY libs/pulsebridge_hl7v2 libs/pulsebridge_hl7v2
COPY libs/pulsebridge_gcp libs/pulsebridge_gcp
COPY services/v2-to-fhir services/v2-to-fhir

# Non-editable installs (wheels built from these paths). The libraries go first, so the service's
# install finds them already present instead of looking them up on PyPI. `pip freeze` puts the
# resolved versions in the build log (transitive dependencies are not pinned; see the README).
# The optional `pip_cert` build secret is a CA bundle for builds behind a TLS-inspecting proxy; a
# secret mount is never stored in a layer.
RUN --mount=type=secret,id=pip_cert \
    if [ -s /run/secrets/pip_cert ]; then export PIP_CERT=/run/secrets/pip_cert; fi; \
    python -m pip --python /opt/venv/bin/python install \
        ./libs/pulsebridge_fhir ./libs/pulsebridge_hl7v2 ./libs/pulsebridge_gcp \
    && python -m pip --python /opt/venv/bin/python install ./services/v2-to-fhir \
    && python -m pip --python /opt/venv/bin/python freeze

# ---- Stage 2: runtime. The venv only: no source tree, no tests, no build tools, no pip. -------
FROM ${PYTHON_REGISTRY}/library/python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS runtime

# A system user with a fixed numeric UID/GID. Cloud Run (and Kubernetes' runAsNonRoot) can only
# verify a numeric USER, and a name would mean nothing outside this image's /etc/passwd.
RUN ["groupadd", "--system", "--gid", "10001", "app"]
RUN ["useradd", "--system", "--uid", "10001", "--gid", "10001", "--no-create-home", "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin", "app"]
# The installer is not needed at run time.
RUN ["python", "-m", "pip", "uninstall", "--yes", "--quiet", "--root-user-action=ignore", "pip"]

# Owned by root and not writable by the app user: the running service cannot change its own code.
COPY --from=build /opt/venv /opt/venv

# PYTHONUNBUFFERED: log lines reach Cloud Logging as they are written. PORT: Cloud Run sets it;
# 8080 is the README's default. gunicorn binds 0.0.0.0:$PORT when PORT is set and --bind is not
# given, which is the README's `--bind ":${PORT:-8080}"` without needing a shell.
# GUNICORN_CMD_ARGS holds two settings the README's command leaves at gunicorn's defaults:
# - --no-control-socket: gunicorn 25.1+ opens a local management socket nothing in a container
#   uses; it would try to create ~/.gunicorn and log an error on every start.
# - --graceful-timeout 8: on SIGTERM gunicorn lets requests finish for this long (default 30 s).
#   Cloud Run sends SIGKILL 10 s after SIGTERM, so 8 s lets gunicorn exit cleanly first.
# PORT must be a port number: an empty PORT is not supported (gunicorn stops with "'' is not a
# valid port number").
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    GUNICORN_CMD_ARGS="--no-control-socket --graceful-timeout 8"

USER 10001:10001
EXPOSE 8080

# Exec form: gunicorn is PID 1 and receives Cloud Run's SIGTERM directly. The arguments are the
# README's ("Running"): one worker, four threads, request deadlines left to Cloud Run, and
# --preload: the app (and its configuration check) loads in the master before the port opens,
# so a bad configuration exits before anything listens and Cloud Run's startup probe fails.
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--timeout", "0", "--preload", "v2_to_fhir.app:create_app()"]
