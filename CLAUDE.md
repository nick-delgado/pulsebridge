# PulseBridge

Personal health record (PHR) on GCP: ingests heart-rate data from a Pixel Watch (Google Health API)
and blood-pressure readings from a simulated cuff (HL7v2 ORU^R01 over MLLP), stores everything as
FHIR R4 in a Cloud Healthcare API FHIR store, charts it in a single-page dashboard, and shares
selected readings with a clinic by writing to OpenEMR's FHIR API. All infrastructure is Terraform.

The full design is in `pulsebridge-projectbrief.md`. Read it before making design decisions; treat
its "Decisions log" as settled and its "Open questions and spikes" as unresolved.

This is a portfolio project. It is meant to showcase FHIR R4, HL7v2 and Terraform, so favour code
that is readable and standards-correct over clever shortcuts. The v2-to-FHIR mapping and the
Google-Health-to-FHIR mapping are our own code on purpose, not a managed mapping service; they are
what a reviewer should read. HL7v2 messages are parsed, built and validated with `hl7apy`; do not
hand-write an ER7 parser.

## Credentials and secrets (hard rules)

- Never print credential material: no `cat`, `echo`, `env` dumps or log lines that reveal service
  account keys, OAuth client secrets, access or refresh tokens, passwords, or `GCP_SA_KEY_B64`.
  When checking that a variable or file exists, show its name or path only, never its value.
- Never commit credential files or secret values: service account key JSON, `.pem` files, OAuth
  client secret files, tokens, `.env` files, Terraform state, or plan files.
- Before every commit, review `git status` and the staged diff for secrets.
- Secret values live in Secret Manager. Terraform creates the secret; a script adds the version
  after apply, so the value stays out of code and, where avoidable, out of state.
- If a secret is exposed by accident, say so immediately so it can be rotated.

## No hardcoded identity

The repo must work for a stranger who clones it and connects their own Google Health account.

- Nothing specific to the author is hardcoded: no project IDs, project numbers, account emails,
  OAuth client IDs, patient identifiers, domains or tokens in code or committed config.
- All such values come from Terraform variables, environment variables or Secret Manager.
- Committed examples use placeholders (`terraform.tfvars.example`), never real values.
- Health data from the watch is real PHI (the author's own). Never copy it into the repo, test
  fixtures, commit messages or logs. Tests and fixtures use synthetic data only.

## GCP environment

- Cloud sessions authenticate through `scripts/gcp-auth.sh`, run by the SessionStart hook in
  `.claude/settings.json`. It decodes the service account key from `GCP_SA_KEY_B64` into the path
  in `GOOGLE_APPLICATION_CREDENTIALS` and activates it for `gcloud`.
- The project comes from `CLOUDSDK_CORE_PROJECT`. Read it from there; do not write it into files.
- Check access with `gcloud auth list` and `gcloud projects describe "$CLOUDSDK_CORE_PROJECT"`.
- Enabling an API, creating billable resources and changing IAM are changes to a real project:
  confirm with the user before doing them outside an agreed Terraform apply.
- Outside an agreed apply, run `terraform` (validate, test, plan against mocks) with Google
  credentials removed from the environment. A mocked provider alone is not a guarantee.
- Cost matters: the stack should cost near zero when idle. Flag anything always-on or billed hourly.

## Conventions

- Infrastructure: Terraform under `infra/` (`modules/`, `envs/dev`, `envs/prod`, `bootstrap/`).
  Pin provider and module versions, commit `.terraform.lock.hcl` in root modules only
  (`envs/*`, `bootstrap/`; lock files under `modules/` are git-ignored), give every variable a
  description, and run `terraform fmt` and `terraform validate` before committing.
- Services: Python on Cloud Run, one least-privilege service account per service.
- FHIR: R4. Observations carry LOINC codes, UCUM units, the vital-signs category, a `device`
  reference and a `Provenance`. Writes are idempotent (conditional create or update).
- HL7v2: version and segment usage follow the brief (ORU^R01, PID/OBR/OBX, ACK AA/AE).

## How work is organised

- `docs/workplan.md` is the task list. Each task has an ID, dependencies, acceptance criteria and
  the paths it owns. Only the orchestrating session edits it.
- Each task is built in its own git worktree: branch `task/<id>-<slug>`, worktree
  `.worktrees/<id>-<slug>` (git-ignored). Never work on a task in the main checkout. Task branches
  are pushed to GitHub (`task/*` only); merging into the integration branch is the orchestrator's job.
- Build with the `feature-builder` agent, which follows the `feature-dev` skill: build, test with
  break-checks, self-review, then an explained summary published as an artifact (not committed).
- Review with the `review-work` skill (`/review-work <task-id> <base-branch>`), which runs the
  `work-reviewer` agent. A task is `done` only after it passes review and is merged.
- Tests must be able to fail. Every test is break-checked: break the implementation, see the test
  fail, restore.
- Run independent tasks in parallel only when their owned paths do not overlap.
- The user is new to FHIR, HL7v2 and Terraform. When explaining work in these areas, define terms
  and say why, not only what.
