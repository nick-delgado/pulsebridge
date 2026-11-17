# End-to-end smoke test (T29)

One command that checks a **deployed** PulseBridge stack the way a reviewer would use it:
through its public front door only (the `fhir-proxy` behind the Firebase Hosting rewrite,
`<dashboard_url>/api`), asserting on what the APIs answer.

```bash
make smoke          # runs: PYTHONPATH=tests/e2e .venv/bin/python -m pulsebridge_smoke
```

It is not part of `make check`: it needs a deployment, the network and the reviewer password,
and it writes a synthetic reading into the stack. Its own tests run in `make test`, against an
in-process stack, with no network and no credentials ("Tests", below).

## Terms

- **FHIR R4**: the standard for health data as JSON *resources*. A measurement is an
  `Observation`; where it came from is a `Provenance`; a visit is an `Encounter`.
- **LOINC** and **UCUM**: the standard codes for *what* was measured (blood-pressure panel
  `85354-9`, systolic `8480-6`, diastolic `8462-4`) and the standard unit syntax (`mm[Hg]`).
- **HL7v2**: the older pipe-delimited message format devices use. The cuff sends an
  **ORU^R01** ("observation result") message: segments `MSH` (header, with MSH-10 the *control
  ID*), `PID` (patient), `OBR` (the panel) and one `OBX` per value (OBX-3 code, OBX-5 value,
  OBX-6 unit, OBX-14 time).
- **MLLP**: how HL7v2 travels over TCP: the message wrapped between a start byte and two end
  bytes. The cuff simulator sends to the edge VM's MLLP adapter, which stores the message in the
  Cloud Healthcare HL7v2 store.
- **ACK**: the HL7v2 reply. Its MSA-1 is `AA` (application accept: stored) or `AE`
  (application error: refused, with the reason in MSA-3).
- **Front door**: the one public entrance. The smoke test never calls Cloud Healthcare, cuff-sim,
  health-sync or OpenEMR directly, so a pass means the system works for a user of the API, not
  only that its parts exist.

## Run it against dev

You need: the dashboard URL, the reviewer password (from Secret Manager, read with **your own**
`gcloud` login), and Python from the repository's venv (`make smoke` creates it).

```bash
# From the repository root. CLOUDSDK_CORE_PROJECT names the dev project (already set in a cloud
# session); your gcloud account needs secretAccessor on <prefix>-reviewer-password.
PULSEBRIDGE_BASE_URL="$(terraform -chdir=infra/envs/dev output -raw dashboard_url)/api" \
SECRET_NAME_PREFIX="$(terraform -chdir=infra/envs/dev output -raw secret_name_prefix)" \
make smoke
```

`terraform output` reads the remote state with your Google credentials. Where that is not wanted
(CLAUDE.md asks that `terraform` run without credentials outside an agreed apply), set the two
variables yourself: the dashboard URL is the Hosting address reviewers open (for a default
Hosting site `https://<hosting_site_id>.web.app`), and the prefix is module identity's
`name_prefix` from your `terraform.tfvars`.

| Variable | Needed for | Value |
| --- | --- | --- |
| `PULSEBRIDGE_BASE_URL` | always | `<dashboard_url>/api` (`https://` only; `http://` only for `localhost`) |
| `CLOUDSDK_CORE_PROJECT` | the password from Secret Manager; Part B | the project id |
| `SECRET_NAME_PREFIX` | the password from Secret Manager | `terraform output -raw secret_name_prefix` |
| `PULSEBRIDGE_REVIEWER_PASSWORD` | instead of Secret Manager | the password (see below for why this is second best) |
| `PULSEBRIDGE_HEALTH_SYNC_SERVICE` | Part B on a real-data stack | `terraform output -raw health_sync_service_name` |

Flags (pass them through `SMOKE_ARGS="..."`): `--verbose` (one line per request: method, path,
status, milliseconds), `--report-json <path>` (the results as JSON), `--fail-on-skip`,
`--allow-real-data-writes`, `--sync-runs-from gcloud|<file>`, `--log-freshness 12h`,
`--status-max-seconds 5`, `--poll-timeout 180`, `--transport mllp|https`. `--help` lists them.

**The password.** By default it is read at run time with
`gcloud secrets versions access latest --secret=<prefix>-reviewer-password`: it then exists only
in the smoke test's memory, never on a command line, in a file, in shell history or in the
environment of other processes. `PULSEBRIDGE_REVIEWER_PASSWORD` works too (for a runner without
`gcloud`), but an environment variable is visible to every child process of that shell; never
type the value on the command line. Either way the password and the session token are
registered with a redactor before first use, and every printed line, failure message and the
JSON report are redacted (`Bearer ...` and anything shaped like a JWT too). Neither is ever
written to disk.

## What it checks

Each line of output is `PASS`, `FAIL`, `SKIP` (deliberately not run, with the reason) or `WARN`
(not as hoped, but not an acceptance criterion). The summary lists every non-pass and ends with
`SMOKE RESULT: PASSED`, `PASSED WITH n SKIPPED` or `FAILED`.

| Id | Check |
| --- | --- |
| S1 | reviewer login (`POST /auth/login`) gives a session token |
| S2 to S6 | `GET /status` answers 200, within `--status-max-seconds` (5 s), FHIR store reachable, observation counts known, OpenEMR reachable |
| S7 | the stack's data mode (below), cross-checked with the patient of the newest earlier reading |
| B1 to B4 | Part B, health-sync's first-sync checks (real data only, below) |
| A1 | a cuff reading with random synthetic values (systolic 101-139, diastolic 61-89) is sent over MLLP (`POST /cuff/readings`) |
| A2, A3 | the store's ACK is `AA`; the attempt is recorded as an AuditEvent |
| A4, A5 | the message sent (read with hl7apy) is the ORU^R01 asked for (MSH-9, MSH-10, OBX-5 values, OBX-6 `mm[Hg]`); on a synthetic stack its PID-3.1 starts with `SYN-` (else everything stops: no share, no negatives) |
| A6 | v2-to-fhir mapped it: the cuff-attempt trace (`GET /trace/cuff-attempts/<id>`) shows application outcome `accepted` and an Observation, polled up to `--poll-timeout` |
| A7a to A7h | the Observation: LOINC `85354-9` panel, `vital-signs` category, status `final`, components `8480-6`/`8462-4` with exactly the values sent, UCUM `mm[Hg]`, `effectiveDateTime` equal to OBX-14, a `Device/...` reference, a `Patient/...` subject |
| A8 | a Provenance targets the Observation and names the stored HL7v2 message as its source |
| A9 | `GET /fhir/Observation/<id>` (what the search's `fullUrl` names) returns the same reading |
| A10, A11a to A11c | the share (`POST /share`) answers `shared`; the read-back "as stored in OpenEMR" has the panel code, the values and the time |
| A12 | sharing again answers `already-shared` and names the same OpenEMR Observation (idempotent) |
| N1 | an invalid reading (80/120: systolic must be above diastolic) is refused with `422` and an OperationOutcome; nothing is sent |
| N2, N3 | a message the store cannot parse (T07 sample `unparseable_no_msh`) gets `AE`, and its trace has no Observation |
| N4, N5 | a message that is stored but cannot be mapped (`unmappable_non_numeric_obx5`: OBX-5 `HIGH`) gets `AA`, then the mapper labels it `error` and creates no Observation |

The Observation is found through the cuff-attempt trace, which looks it up **by identifier**
(Cloud Healthcare indexes identifiers synchronously), not with a date search, which Cloud
Healthcare indexes with a delay (fhir-proxy README, "Not verified").

## Real data: the guard, and Part B

**How the data mode is detected.** With `use_synthetic_data = true` the stack deploys no
health-sync (the seeder job replaces it), so fhir-proxy has no `HEALTH_SYNC_URL` and `/status`
reports `watchSync.reason = "health-sync-not-deployed"`. Any other answer, including one that
cannot be read, counts as **real data** (the safe direction). Before writing, S7 also reads the
newest earlier built reading in the messages log and checks that its PID-3.1 starts with `SYN-`
(module phr refuses any other patient id with synthetic data); after sending, A5 checks the new
message the same way.

**Why Part A's writes are refused on real data.** Part A adds a blood-pressure reading to the
configured patient's record and shares it to OpenEMR. On a real-data stack that patient is a
real person: the values are synthetic, but they would sit in that person's record, on their
chart in OpenEMR and in their dashboard, indistinguishable from a real reading. So on a
real-data stack A1, A10 and N1 are reported `SKIP` ("refused: the stack reports real data")
and nothing is written, unless `--allow-real-data-writes` is given. Use that flag only on a
stack whose owner agrees to a test reading in their record, and note the trail it prints.

**Part B: health-sync's first-sync checks** (services/health-sync/README.md, "For T25"):

| Id | Check |
| --- | --- |
| B1 | `/status` says health-sync is `connected` to Google Health |
| B2 | in every synced run that wrote a Bundle, the Patient entry answered `200`, never `201`. health-sync's Bundle creates the Patient only if none has its identifier (a conditional create); the setup step already wrote it, so the store must *match* (200). A `201` means it created a second Patient: switch to the fixed-ID fallback in the T06 README and record it |
| B3 | the newest synced run with windows (at least the second one) has `stored_read: ok` and `windows_unchanged > 0`: the store answers the searches health-sync relies on. `ok` with 0 unchanged in every run means the read silently matches nothing |
| B4 | `WARN` only: `stranded_preliminary` is a number |

On a synthetic stack B1 to B4 are `SKIP` ("skipped: synthetic data"), never a silent pass. Use
`--fail-on-skip` to make any skip exit with status 3.

**What Part B needs, and why.** The evidence (`entry_statuses`, `stored_read`,
`windows_unchanged`) is in health-sync's run report, which only Cloud Scheduler receives and
which health-sync logs as a `sync-run` line. health-sync is internal-only and the proxy's
`/status` passes on only the connection state, so from outside the reports can be read **only
from Cloud Logging**. The runner supplies that access:

- `--sync-runs-from gcloud` (the default when `PULSEBRIDGE_HEALTH_SYNC_SERVICE` is set) runs
  `gcloud logging read 'resource.type="cloud_run_revision" AND
  resource.labels.service_name="<service>" AND jsonPayload.event="sync-run"'
  --project=<CLOUDSDK_CORE_PROJECT> --freshness=12h --limit=50 --order=desc --format=json` with
  **your** credentials: `roles/logging.viewer` on the project is enough (the deployer role does
  not include it; the project owner has it);
- `--sync-runs-from <file>` reads the JSON that command printed, saved by someone who has the
  role.

The lines hold counts and codes only (health-sync never logs heart-rate values, tokens or the
patient id). On a real-data stack without a log source, B2 and B3 `FAIL` with what is needed.

## Exit status

| Status | Meaning |
| --- | --- |
| 0 | no check failed (skips are listed in the summary) |
| 1 | a check failed, or a part stopped after a failure |
| 2 | configuration: no base URL, no password, an unreadable secret, a bad setting |
| 3 | with `--fail-on-skip`: nothing failed, but something was skipped |

## What each run adds, and cleanup

All values are synthetic. A run on a synthetic stack adds:

- **one blood-pressure reading**: an HL7v2 message (new control ID), its AuditEvent, one
  `Observation` and one `Provenance` in the FHIR store, labelled `accepted`;
- **in OpenEMR**: the reading, in that day's Encounter for the synthetic patient (the Patient
  and the Encounter are created only if absent). The second share adds nothing;
- **one rejected attempt**: an AuditEvent only (the store did not keep the message);
- **one unmappable attempt**: an AuditEvent, and the sample's message labelled `error` (the
  store keeps one copy of identical bytes, so later runs add no second message, only another
  AuditEvent);
- in the messages log and the dashboard's chart, the reading shows like any other cuff reading.

The output ends with the trail: the values and send time, the attempt ids, the control ID, the
FHIR Observation id and the OpenEMR Observation id, so these records can be found later.

**Cleanup through the API is not possible, by design.** The proxy is read-only (D12) except
the share, which only creates in OpenEMR and never updates or deletes. Removing a test reading
needs the owner's own access: the Cloud Healthcare API (`DELETE` on the Observation and
Provenance, the HL7v2 message, the AuditEvent) and OpenEMR's own interface. On a synthetic dev
stack this is not needed; the readings are synthetic and plausible.

**Reruns are safe.** Every reading gets a new control ID, so it never overwrites an earlier
Observation (v2-to-fhir keys the Observation on MSH-3, MSH-4 and MSH-10). Two caveats:

- OpenEMR keeps one blood pressure per Encounter and `effectiveDateTime`, and cuff-sim stamps
  whole seconds. Two readings sent within the same second (two runs at once) collide, and the
  second run's A10 fails with "OpenEMR already holds a blood pressure at this exact second".
- cuff-sim allows 3 sends back to back and 6 a minute per instance. A run makes 3 sends; a
  `429` is waited out (`Retry-After`) and retried, which is safe because nothing was sent.

## Code

```text
tests/e2e/
  pulsebridge_smoke/     the command (stdlib urllib, plus hl7apy through T07's parser)
    cli.py               flags, environment, the password and log sources, exit status
    http.py              transport (no redirects), the API client, OperationOutcome reasons
    redact.py            the password and token out of every line
    report.py            PASS/FAIL/SKIP/WARN, the trail, the summary
    cuff_path.py         status, the real-data guard, Part A
    first_sync.py        Part B and its Cloud Logging source
    hl7.py, fhir.py      reading the sent ORU^R01; the R4 rules for the Observation
  e2e_fake_stack.py      the in-process stack for the tests (below)
  e2e_runs.py            runs the command against it
  test_*.py              the tests
```

## Tests

`make test` runs `tests/e2e/test_*.py` against **an in-process stack**: the real fhir-proxy
(with the share extension), the real cuff-sim and v2-to-fhir's real mapping, over fakes of the
Cloud Healthcare stores, the edge VM's MLLP adapter (which imitates what spikes S4/S5 recorded:
`AE` for a message without MSH, `AA` and storage otherwise) and OpenEMR (from
`fhir_proxy.share.testing`). The smoke command runs unchanged against it, through a transport
that calls the proxy's Flask test client.

Every assertion is break-checked in `test_smoke_breaks.py`: the stack is made to return the
wrong thing (the store answers `AE`, the mapper writes `mmHg` instead of `mm[Hg]` or drops the
device, the mapper never runs, OpenEMR keeps other values, a second share creates a copy, the
unparseable message is accepted, ...) and the test asserts that exactly that check fails, with
a message saying what was wrong. Part B is tested against synthetic `sync-run` log entries in
`test_first_sync.py`, including a whole real-data run that reads them from a file.

## Not verified

- **No live run yet.** T29 was built without network access to the deployment; the first live
  run against dev is the orchestrator's. Part B has never run live: no real-data stack exists.
- The `gcloud logging read` filter and the shape of its JSON (`jsonPayload` per entry) are from
  Cloud Logging's documented output and health-sync's logging code, not from a real entry.
- The 5 s `/status` limit: a cold Cloud Run start, or a cold edge VM behind the OpenEMR probe,
  can exceed it. Raise `--status-max-seconds` for a first call after a long idle period.
- health-sync's stranded-preliminary count uses `_summary=count`, which Cloud Healthcare refuses
  (found by the first dev deploy for the proxy's counts). Expect B4 to `WARN` until health-sync
  changes that search.
