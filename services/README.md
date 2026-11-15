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

## Layout

```text
services/<name>/
  pyproject.toml      # dependencies, including the shared libs under libs/
  <package>/          # application code
  tests/              # pytest tests for this service only
  Dockerfile          # container image (added by the build pipeline task)
```

## Working with a service

- `make install` installs every `services/*/pyproject.toml` into the shared `.venv` in editable
  mode, alongside the libraries in `libs/`.
- `make test` runs the tests under `services/*/tests` together with the rest of the repository.
- Configuration (project, dataset, store names) comes from environment variables set by Terraform.
  Secrets come from Secret Manager. Nothing specific to one deployment is hardcoded.
