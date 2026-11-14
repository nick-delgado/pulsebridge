# libs

Shared Python libraries used by more than one service. They are pure Python with no GCP calls, so
they can be tested on any machine without cloud credentials.

| Library | What it does | Task |
| --- | --- | --- |
| `pulsebridge_fhir` | Builds FHIR R4 resources (Patient, Device, Observation, Provenance, transaction Bundle) | T06 |
| `pulsebridge_hl7v2` | Builds, validates and parses HL7v2 ORU^R01 and ACK messages with `hl7apy` | T07 |

## Layout

Each library is its own installable package:

```text
libs/<name>/
  pyproject.toml      # name, version, dependencies, build backend
  <name>/             # the import package (`import <name>`)
    __init__.py
  tests/              # pytest tests for this library only
```

## Working with a library

- `make install` installs every `libs/*/pyproject.toml` into the shared `.venv` in editable mode,
  so a change to the code is picked up without reinstalling.
- `make test` runs the tests under `libs/*/tests` together with the rest of the repository.
- `make lint` checks the code with the ruff rules in the root `pyproject.toml`.
- Tests use synthetic data only; never real readings from the watch.
