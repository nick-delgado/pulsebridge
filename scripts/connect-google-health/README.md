# connect-google-health

Connects **your own** Google Health account to your PulseBridge deployment (task T11, decision
D2). You run it once on your own computer after `terraform apply`, and again whenever the
connection needs renewing (every 7 days while your OAuth app is in "Testing" status).

It does three things:

1. puts your OAuth client ID and client secret into Secret Manager (it asks for them, hidden, if
   they are not there yet);
2. sends you through Google's consent screen and stores the **refresh token** Google returns as a
   new Secret Manager version, which `health-sync` picks up on its next run;
3. writes your **Patient** record into the FHIR store, once and in full (decision D11).

`health-sync` needs exactly these three secrets from you and nothing else:
`<prefix>-google-health-client-id`, `<prefix>-google-health-client-secret`,
`<prefix>-google-health-refresh-token` (prefix `pb` unless you changed the identity module's
`name_prefix`). The script writes to no other secret (a test checks this).

## Before you run it

1. **Terraform applied.** The identity module creates the three secrets (empty) and the
   healthcare module the FHIR store. The script never creates either.
2. **A Google Health OAuth app** in your project (spike S3 has the console steps): enable the
   Google Health API; on the consent screen choose user type External, add yourself as a test
   user, and add the scope
   `https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly` on the
   Data Access page; then create an OAuth client of type **Desktop app**. A Desktop client accepts
   a loopback redirect on any port; a "Web application" client would need
   `http://127.0.0.1:<port>` registered and `--port <port>`.
3. **Your own Google Cloud credentials** for Secret Manager and the FHIR store:
   `gcloud auth application-default login` (the script uses Application Default Credentials; it
   never reads a key file). If Secret Manager complains about a quota project, run
   `gcloud auth application-default set-quota-project <your project>`.
4. **Python 3.13 or newer** on the machine.

## Run it

```bash
export GCP_PROJECT_ID=<your project id>
export HEALTHCARE_LOCATION=<dataset region> HEALTHCARE_DATASET_ID=<dataset id> FHIR_STORE_ID=<main FHIR store id>
export PATIENT_ID=<the same patient id health-sync is configured with>
export PATIENT_FAMILY_NAME=<family name> PATIENT_GIVEN_NAME=<given name>
export PATIENT_GENDER=<male|female|other|unknown> PATIENT_BIRTH_DATE=<YYYY-MM-DD>

scripts/connect-google-health/connect.sh
```

`connect.sh` is the one command: on first use (and after any change to this tool or the two
libraries) it creates a private virtual environment in `~/.cache/pulsebridge/connect-google-health`
(override with `CONNECT_VENV`; choose the Python with `PYTHON`), installs this tool together with
`libs/pulsebridge_fhir` and `libs/pulsebridge_gcp` into it, and runs `connect-google-health` with
your flags. Values can be given as flags instead of variables (`--help` lists them).

What you see (a first run; synthetic project name):

```text
Connecting Google Health for project demo-project-1.
[1/4] OAuth client
  OAuth client ID: no usable version stored. Copy it from Google Auth Platform > Clients.
OAuth client ID (input hidden):
  OAuth client ID: stored as version 1 of secret pb-google-health-client-id.
  OAuth client secret: no usable version stored. Copy it from Google Auth Platform > Clients.
OAuth client secret (input hidden):
  OAuth client secret: stored as version 1 of secret pb-google-health-client-secret.
[2/4] Consent
  A browser was opened at this local link (open it by hand if not):
    http://127.0.0.1:40977/start
  Waiting up to 300 s for Google's redirect...
[3/4] Refresh token
  Connected. Stored the refresh token as version 1 of secret pb-google-health-refresh-token.
  health-sync reads the latest version on its next run; no redeploy is needed.
  Google says this refresh token expires in 7.0 days, at 2026-01-22 11:59 UTC. Run this script again before then.
  A 7-day limit is what Google applies while the OAuth app's publishing status is "Testing".
  To stop the weekly reconnect, publish the OAuth app: Google Cloud console > Google Auth Platform > Audience > "Publish app" (publishing status "In production"). Whether Google then requires app verification for health scopes was not tested (spike S3).
[4/4] Patient record
  Patient record created (conditional update; safe to repeat).
Done.
```

On later runs the client is read from Secret Manager (no prompts), the refresh token becomes
version 2, 3, ..., and the Patient record is "updated" (the same record written again).

### When the browser is on another machine

On a remote machine (SSH, a cloud shell), nothing on your computer can reach the script's
listener. Use `--paste`: the script prints Google's address; open it in your browser, approve,
and the browser then fails to load `http://127.0.0.1:<port>/?...`. That is expected: copy the
whole address from the address bar and paste it at the hidden prompt. It holds the one-time code,
which is why the input is hidden. The script falls back to this mode by itself if it cannot
listen on 127.0.0.1.

In paste mode the printed address contains your OAuth **client ID**. A client ID is a public
identifier in OAuth (RFC 6749 section 2.2: "The client identifier is not a secret") and it is in
the browser's address bar anyway; still, it is the only case where the script shows it. In the
default mode the browser is sent to the local `/start` link, which redirects to Google, so the
client ID never appears in the terminal.

## Flags and environment variables

| Flag | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `--project` | `GCP_PROJECT_ID` | required | project of the secrets and the FHIR store |
| `--name-prefix` | `PULSEBRIDGE_NAME_PREFIX` | `pb` | the identity module's `name_prefix` (secret names) |
| `--scope` | `GOOGLE_HEALTH_SCOPE` | `https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly` | scope requested; same default as `health-sync` |
| `--location` | `HEALTHCARE_LOCATION` | required* | dataset region |
| `--dataset` | `HEALTHCARE_DATASET_ID` | required* | dataset id |
| `--fhir-store` | `FHIR_STORE_ID` | required* | the main FHIR store (not the audit store) |
| `--patient-id` | `PATIENT_ID` | required* | must equal `health-sync`'s and `v2-to-fhir`'s `PATIENT_ID` |
| `--family-name`, `--given-name` | `PATIENT_FAMILY_NAME`, `PATIENT_GIVEN_NAME` | required* | official name |
| `--gender` | `PATIENT_GENDER` | required* | `male`, `female`, `other`, `unknown` (FHIR R4 administrative gender) |
| `--birth-date` | `PATIENT_BIRTH_DATE` | required* | `YYYY-MM-DD` |
| `--port` | | `0` (random free port) | loopback port for the redirect |
| `--paste` | | off | paste the redirect address instead of listening |
| `--no-browser` | | off | print the local link instead of opening a browser |
| `--timeout` | | `300` | seconds to wait for the redirect (10 to 3600) |
| `--reset-client` | | off | ask for the client ID and secret again even if stored |
| `--disable-previous` | | off | disable (never destroy) older refresh-token versions |
| `--skip-patient` | | off | do not write the Patient; *not required then |

A flag wins over its variable. Everything is checked before anything is stored or any browser
opens; an error names the flag or variable, never the value.

Exit codes: `0` connected (and Patient written unless skipped); `1` not connected, no refresh
token stored; `2` bad arguments or configuration; `3` connected (refresh token stored) but the
Patient write failed; `130` interrupted.

## What happens, step by step

The code reads in the same order (`connect_google_health/cli.py`, function `run`).

| Step | What | Where |
| --- | --- | --- |
| 1 | Read the latest version of the client ID and client secret. A secret with no version, or whose latest version is disabled or destroyed, counts as absent: prompt with `getpass` (no echo), check it is one printable value with no spaces (the rule `health-sync` applies to its mounted files), store it as a new version. | `secret_store.py`, `cli.client_credentials` |
| 2 | PKCE: a 43-character random **code verifier** and its **S256 challenge**; a random `state`. | `pkce.py` |
| 3 | Google's authorization address: `client_id`, `redirect_uri=http://127.0.0.1:<port>`, `response_type=code`, `scope`, `code_challenge`, `code_challenge_method=S256`, `state`, `access_type=offline`, `prompt=consent`. | `oauth.authorization_url` |
| 4 | A listener on 127.0.0.1 only. `/start` redirects to Google; `/?state=...&code=...` is the redirect. `state` is checked first; `error=` (for example `access_denied`), no code, a repeated parameter, a wrong path or `Host`, a second redirect and a timeout are each handled. The page shown holds no code, token or state; http.server's request log (which would hold the code) is switched off. | `loopback.py` |
| 5 | `POST https://oauth2.googleapis.com/token` with `client_id`, `client_secret`, `code`, `code_verifier`, `grant_type=authorization_code`, `redirect_uri`. A response without `refresh_token`, or whose `scope` list lacks the requested scope (its box was unticked), stores nothing and says what to do. | `oauth.exchange_code` |
| 6 | The refresh token becomes a new version of `<prefix>-google-health-refresh-token`. Only the secret name and version number are printed. | `cli.run` |
| 7 | Expiry: Google's `refresh_token_expires_in` when given; otherwise an explicit "cannot know" (below). | `cli.describe_expiry` |
| 8 | `GET https://health.googleapis.com/v4/users/me/identity` with the new access token gives `healthUserId`; T06's `patient_setup_transaction` builds the setup Bundle; `pulsebridge_gcp.HealthcareClient.execute_bundle` writes it with your Application Default Credentials. | `patient.py` |

### FHIR: the Patient write

The Bundle has one entry, the full Patient, written as a **conditional update**:

```json
{"resourceType": "Bundle", "type": "transaction", "entry": [{
  "fullUrl": "urn:uuid:...",
  "resource": {"resourceType": "Patient",
    "identifier": [
      {"use": "official", "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "PT"}]},
       "system": "urn:pulsebridge:patient", "value": "<PATIENT_ID>"},
      {"use": "secondary", "system": "urn:pulsebridge:google-health-user-id", "value": "<healthUserId>"}],
    "name": [{"use": "official", "family": "<family>", "given": ["<given>"]}],
    "gender": "<gender>", "birthDate": "<YYYY-MM-DD>"},
  "request": {"method": "PUT", "url": "Patient?identifier=urn:pulsebridge:patient|<PATIENT_ID>"}}]}
```

`PUT Patient?identifier=...` asks the server to find the Patient with that identifier and replace
it, or create it if none exists. Running the script again therefore lands on the same Patient.
The ingest services write the Patient with a conditional *create* (`ifNoneExist`), which never
overwrites, so this full record is not undone by them (T06 README, "Who owns the Patient
record").

**No Device is written.** T06 keys the Device on the patient and the watch's display name, and
`health-sync` takes that name from `dataSource.device.displayName` of the newest raw heart-rate
point. Setup knows nothing more about the watch, and a name read here that differed in any way
from health-sync's would create a second watch Device. So setup writes none, and `health-sync`
creates the Device on its first sync, as the T06 README recommends ("If setup cannot read that
name, pass no devices").

## Expiry, honestly

Google's token response may carry `refresh_token_expires_in`. Spike S3 saw `604799` (7 days) with
an app in Testing status; Google's desktop-app page says the field "is only set when the user
grants time-based access". So:

- **When Google gives it**, the script prints the date and time, and, if it is about 7 days, says
  that this is the Testing-status limit and how to publish the app.
- **When Google does not**, the script says it cannot see your app's publishing status: if
  "Testing", the token expires 7 days from now (Google Identity, "Refresh token expiration": an
  External app in Testing "is issued a refresh token expiring in 7 days"); if "In production", it
  lasts until access is revoked or it is unused for six months.

To stop the weekly reconnect, publish the app (Google Auth Platform > Audience > "Publish app").
Whether Google then asks for verification for these health scopes was not tested.

## Re-running

Always safe:

- the client ID and secret are only written when absent (or with `--reset-client`);
- each run adds a **new** refresh-token version; `health-sync` mounts version `latest`, so the
  newest is used from its next run, with no redeploy;
- the Patient write is a conditional update: the same record, never a second Patient;
- if the Patient write fails after the token was stored (exit `3`), nothing is rolled back: the
  connection works; run the script again to write the Patient.

**Older refresh-token versions are left enabled by default.** They are not destroyed, ever.
`--disable-previous` disables those older than the new one (needs
`roles/secretmanager.secretVersionManager` on that secret). Why opt-in: disabling needs a
stronger role than the rest of the script, and in Testing status the older tokens expire within
7 days anyway; in production an old token stays valid at Google until revoked, so disabling
copies you no longer need is good hygiene there. Google also keeps at most 100 refresh tokens per
account per client and silently invalidates the oldest beyond that.

## IAM the deployer needs

On the three secrets (or a project role that includes these, such as Owner):

| Role | On | Why |
| --- | --- | --- |
| `roles/secretmanager.secretAccessor` | `<prefix>-google-health-client-id`, `<prefix>-google-health-client-secret` | read the stored client |
| `roles/secretmanager.secretVersionAdder` | all three secrets | store new versions |
| `roles/secretmanager.secretVersionManager` | `<prefix>-google-health-refresh-token` | only with `--disable-previous` |
| `roles/healthcare.fhirResourceEditor` | the main FHIR store | the Patient `executeBundle` (conditional update) |

Plus the Secret Manager and Cloud Healthcare APIs enabled (T03), and your own Google account as a
test user of the OAuth app. The identity module grants none of these to the deployer: they are
the deployer's own project roles.

## For other tasks

- **T25 (env roots):** the setup write must happen before the Scheduler job and the push
  subscription are enabled (T25 AC). This script is that setup write when the real watch is used
  (`use_synthetic_data = false`); the seeder (T09) is it otherwise. `PATIENT_ID` here must equal
  the `PATIENT_ID` given to `health-sync` and `v2-to-fhir`. Output the FHIR location, dataset
  and store id (and the secret prefix) so the deployer can set the variables above from
  `terraform output`.
- **T27 (secret scripts):** the three Google Health secrets are written here, not by
  `scripts/secrets`. The same rules apply (prompted without echo, never on disk).
- **T28 (setup guide):** the "Before you run it", "Run it", "When the browser is on another
  machine", "Expiry, honestly" and "IAM" sections are written for a new user and can be carried
  into `docs/setup.md` as they are. The guide must say: client type "Desktop app"; add yourself
  as a test user; the reconnect every 7 days in Testing; how to publish.
- **T29 (smoke test):** after the first real run, check `GET Patient?identifier=urn:pulsebridge:patient|<PATIENT_ID>`
  returns exactly one Patient with the `google-health-user-id` identifier.

## Security properties (and the tests that check them)

| Property | Test |
| --- | --- |
| PKCE S256 per RFC 7636 (Appendix B vector) | `tests/test_pkce.py` |
| Authorization address has exactly the documented parameters | `tests/test_oauth.py::test_authorization_url_has_exactly_the_documented_parameters` |
| `state` checked before an error or code is acted on | `tests/test_oauth.py::test_state_mismatch_is_refused`, `tests/test_cli.py::test_state_mismatch_stores_nothing` |
| Listener binds 127.0.0.1 only, answers one redirect, then stops; its page holds no code or state; no request log | `tests/test_loopback.py` |
| Token request body, literally; failures (`invalid_grant`, `invalid_client`, network, non-JSON, 5xx) reported without leaking | `tests/test_oauth.py`, `tests/test_local_token_endpoint.py` (real HTTP on 127.0.0.1) |
| No refresh token, or scope not granted: nothing stored | `tests/test_cli.py::test_token_failures_store_no_refresh_token` |
| Nothing secret or private in stdout, stderr, logs, pages or exception text, on every path | `tests/test_no_leaks.py` (19 scenarios, fd-level capture) |
| Only the three secrets are written; nothing is destroyed | `tests/test_cli.py::test_writes_to_exactly_the_three_secrets`, `tests/test_secret_store.py` |
| Identity call and the setup Bundle (method, URL, identifiers, no Device) | `tests/test_cli.py::test_connects_stores_the_refresh_token_and_writes_the_patient` |
| Partial failure after the token is stored; re-run | `tests/test_cli.py` |

## Tests

`make test` runs `tests/` (the repository's `pyproject.toml` lists this directory in pytest's
`testpaths`). No network except 127.0.0.1, no credentials, no GCP: Secret Manager is a fake
behind the `SecretStore` interface (and the real adapter is tested against a fake client and a
fake exceptions module, so `google-cloud-secret-manager` is not needed in the dev environment);
Google's token endpoint, the identity call and the FHIR store are `pulsebridge_gcp.testing.FakeTransport`;
a scripted browser (`connect_google_health.testing.FakeBrowser`) drives the real listener over
real HTTP. Every credential is assembled at run time from obviously synthetic words.

## Documentation relied on

- Google Identity, "OAuth 2.0 for Mobile & Desktop Apps" (developers.google.com/identity/protocols/oauth2/native-app), read 2026-11-15: loopback redirect `http://127.0.0.1:port` on a random port; PKCE verifier rules and S256; authorization endpoint and parameters; token endpoint fields (`client_secret` "Optional"); response fields including `refresh_token_expires_in`; `error=access_denied`; the HTML page to show; manual copy/paste (OOB) is no longer supported (our paste mode is not OOB: it is the normal loopback redirect, copied by hand).
- Google Identity, "Using OAuth 2.0 for Web Server Applications" (…/oauth2/web-server): `access_type=offline`, `prompt=consent`.
- Google Identity, "Using OAuth 2.0 to Access Google APIs" (…/oauth2): "Refresh token expiration" (7 days in Testing; six months unused; 100 tokens per account per client) and "Token size" (refresh tokens up to 512 bytes).
- RFC 7636 Appendix B (the S256 vector) and RFC 6749 sections 2.2, 4.1.2.1, 5.2, 10.12. The RFC text could not be fetched from the build environment; the vector's three values (octets, verifier, challenge) are consistent with each other, which the test checks.
- Spike S3 for the Google Health identity call and the observed token response.
- The `google-cloud-secret-manager` 2.31.0 client source (method names and the `latest` alias).

## Not verified against real Google services

Nothing in this task called Google OAuth, the Google Health API, Secret Manager or the FHIR
store. To confirm on the first real run:

1. The consent flow with a **Desktop app** client and a random loopback port returns to
   `http://127.0.0.1:<port>/?state=...&code=...` (spike S3 saw this shape, with a pasted address).
2. Google accepts `access_type=offline` and `prompt=consent` together with PKCE for a Desktop
   client, and the token response carries `refresh_token` (and, in Testing,
   `refresh_token_expires_in`).
3. The token response's `scope` is the requested scope string exactly; if Google ever lists it
   differently, the script refuses to store (it could be relaxed to a warning, as `health-sync`
   does).
4. `access_secret_version` on `versions/latest` answers `NOT_FOUND` for a secret with no versions
   and `FAILED_PRECONDITION` when the latest is disabled (both treated as "absent"), and the
   deployer's ADC works with Secret Manager without extra quota-project setup.
5. The identity call answers with `healthUserId` for the new access token (spike S3 saw it).
6. The FHIR store accepts the setup Bundle with the deployer's credentials, and its
   transaction-response entry status starts with `201` (created) or `200` (updated).
7. `connect.sh` on a deployer's macOS or Linux machine. In the build container (Linux, Python
   3.13) it created its environment, installed the pinned packages from PyPI and ran `--help`;
   no connect was attempted.
8. Whether publishing the app requires verification for the health scope.
