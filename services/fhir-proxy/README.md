# fhir-proxy

The dashboard's only backend (task T14, decision D10), and the only public service in
PulseBridge. It checks the shared reviewer password and issues a signed session token (D9),
serves a narrow read API over the two FHIR stores and the HL7v2 store, assembles the messages
log, and forwards cuff readings to the private cuff simulator with its own identity. The share
to OpenEMR (T15) plugs in as an extension (see "For T15").

```
browser ──HTTPS, Authorization: Bearer <session token>──> fhir-proxy
                                                           │ token check (HS256, key = scrypt(password))
             mounted secret file (reviewer-password) ──────┤
FHIR store (health data)   <── read/search, patient-pinned ┤
audit FHIR store (D15)     <── AuditEvent search/read ─────┤
HL7v2 store                <── messages.get / list ────────┤
cuff-sim  (private)        <── OIDC identity token ────────┤ POST /cuff/readings, GET /cuff/samples
health-sync (private)      <── OIDC identity token ────────┘ GET /status
```

It is **read-only** (D12): it writes nothing to any store. Every input is treated as hostile.

## Layout

```text
services/fhir-proxy/
  pyproject.toml        the three libs, Flask, gunicorn, PyJWT (exact pins)
  fhir_proxy/
    config.py           environment variables, checked at start-up (errors name variables only)
    password.py         the mounted reviewer-password file, re-read after a short cache
    session_token.py    the session token: scrypt key from the password, HS256 JWT, constant-time check
    client_address.py   which X-Forwarded-For entry is the client (TRUSTED_PROXY_HOPS)
    login_limit.py      failed-login sliding windows, per address and in total
    search.py           GET /fhir/Observation: strict validation, the R4 date rule, paging links
    store.py            reads from the three stores; the patient pin
    hl7.py              stored message -> control ID, and the Observation identifier v2-to-fhir writes
    messages.py         the messages log: AuditEvents + stored messages + transactions, newest first
    traces.py           heart-rate trace (reconstructed roll-up window) and cuff attempt trace
    status.py           GET /status: health-sync's state, counts, newest heart-rate write
    upstream.py         identity tokens and the calls to cuff-sim and health-sync
    capability.py       the CapabilityStatement, generated from the route table
    routes.py           the route table (the app and the CapabilityStatement both read it)
    extensions.py       where T15's share plugs in; the share-trace helper
    errors.py           OperationOutcome with fixed wording; upstream errors to bounded reasons
    logs.py             JSON log lines, values never logged
    app.py              Flask app: the gate, CORS, security headers, thin handlers
    testing.py          test fakes (synthetic data only), like pulsebridge_gcp.testing
  tests/                pytest; no network beyond 127.0.0.1, no credentials, no GCP
```

## Routes as built

Paths are relative to `PATH_PREFIX` (empty by default; `/api` behind the Firebase Hosting
rewrite, see "For T22"). Every route except `POST /auth/login` needs
`Authorization: Bearer <token>` and answers `401` with `WWW-Authenticate: Bearer` otherwise,
**including** `GET /fhir/metadata` and unknown paths. Every error body is a FHIR
`OperationOutcome` whose `issue[0].details.coding[0]` (system `urn:pulsebridge:fhir-proxy:reason`)
is a fixed reason code and whose `diagnostics` is a fixed sentence.

| Method and path | Answer |
| --- | --- |
| `POST /auth/login` | `{"password": "..."}` (exactly that, strict JSON, at most 1 KiB) → `200 {"token", "expires_at"}`; `401` wrong password; `429` + `Retry-After`; `503` secret missing or empty; `400` bad body; `413`; `415` not `application/json` |
| `GET /status` | always `200` once authenticated: `{"watchSync": {state, reason, notes, strandedPreliminary, checkedAt, lastSyncAt}, "observationCounts": {watch, cuff}, "fhirStore": {reachable, checkedAt, detail}, "openemr": {...}}` |
| `GET /fhir/metadata` | the CapabilityStatement (`application/fhir+json`) |
| `GET /fhir/Observation?code=&date=&_sort=&_count=` | `searchset` Bundle of the configured patient's Observations; `link` `self` and `next` point at the proxy; `400` OperationOutcome for anything unsupported |
| `GET /trace/heart-rate/{observationId}` | `{"resources": Bundle (collection: Observation, Provenance?, Device?), "source": {"kind": "reconstructed", "window"} \| null}`; `404` not found, another patient's, or not a heart rate |
| `GET /trace/cuff-attempts/{attemptId}` | `{"attempt": SendAttemptEntry, "messageSent": "<ER7>" \| null, "resources": Bundle (collection: AuditEvent as stored, Observation?, Provenance?)}`; `404`; `422` AuditEvent unreadable |
| `GET /messages?kind=&outcome=&count=&cursor=` | `{"entries": [...], "nextCursor": string \| null, "unreadable": number}`, newest first |
| `GET /cuff/samples` | cuff-sim's list, forwarded |
| `POST /cuff/readings` | forwarded to cuff-sim unchanged; its `200/400/413/422/429/500`, body and `Retry-After` unchanged; `502` cuff-sim unreachable or unusable; `504` timed out (may have been sent) |
| `OPTIONS <any route>` | CORS preflight, no token: `204` for an allowed origin, method and headers; `403` otherwise; `404` unknown path |

A token in the query string (`access_token`, `token`, `id_token`) is refused with `400` and
`WWW-Authenticate: Bearer error="invalid_request"`, even beside a valid header.

### Contract amendments for the dashboard (web/README.md, contract.ts, mock-client.ts)

Each is a deliberate change from T16/T34's proposal; the client and mock should follow.

| # | What | Before (proposal / mock) | After (built) | Who follows |
| --- | --- | --- | --- | --- |
| 1 | `SendAttemptEntry.messageLookup` | absent | **new**: `'confirmed' \| 'not-found' \| 'failed' \| null`. Whether the store confirmed the message name with a `get`: `confirmed` (labels read), `not-found` (the derived MLLP name was wrong, or the message was deleted), `failed` (the store could not be asked); `null` when there is no name (rejected, no acknowledgment). An accepted row whose lookup is not `confirmed` has `application: null` | T19, mock |
| 2 | `FhirTransactionEntry.writer` | `'health-sync' \| 'v2-to-fhir'` | `'health-sync' \| 'v2-to-fhir' \| 'seed' \| 'unknown'` (the seeder's assembler is `PulseBridge seed (synthetic data)`) | T19, contract |
| 3 | Messages `count` | default 50, cap 100 (mock) | default **25**, cap **50** (each accepted row costs one `get`) | T19, mock |
| 4 | Filtered log pages | mock filters a full list | with `outcome` or `kind`, a page may hold **fewer than `count` rows, even none, while `nextCursor` is not null**: the filter runs after the join. Follow `nextCursor` until null | T19, mock |
| 5 | `store-only` rows | not produced by the mock | produced. `id` = the stored message name, `transport` `'https'` when the message has cuff-sim's `transport` label, else `null`; `controlId` read from the bytes; `messageLookup: 'confirmed'`. **No trace route**: `getCuffAttemptTrace` takes AuditEvent ids, so do not put a store-only row in the selection | T19, mock |
| 6 | `unreadable-send-attempt.id` | string | string, `""` when the AuditEvent had no id | T19 |
| 7 | Search refusals | mock: `422` for an unsupported code | **`400`** with an OperationOutcome (R4: a refused search parameter is a 400), `issue.code` `not-supported` or `invalid` | mock |
| 8 | Search Bundle | `link[next]` under the base URL | `link` has `self` and `next` as **path-absolute** URLs (`/api/fhir/Observation?...`), never built from `Host`; entries have **no `fullUrl`** (the store's would name the project; the proxy has no read route); no `total` (entries are post-filtered) | none (the client already resolves relative links) |
| 9 | `watchSync.reason` when health-sync fails | `health-sync-unreachable` (example) | one of `health-sync-timeout`, `health-sync-unreachable`, `health-sync-error` (non-200), `health-sync-bad-answer`, `health-sync-identity-token-unavailable`, `health-sync-not-deployed`; `state` is `unknown` | T17 (shows it as detail text) |
| 10 | `openemr` in `/status` | mock: reachable | `{"reachable": false, "detail": "not-checked"}` until T15 installs a probe | T15 |
| 11 | `503` on protected routes | only login | when the secret is missing **every** route answers `503` (`login-not-configured`): no token can be checked | none (an `ApiError`) |
| 12 | Cuff trace of an unreadable AuditEvent | `404` (mock) | `422` (`unreadable-send-attempt`); the row is in the log | T18 |
| 13 | `lastSyncAt` | ISO | ISO in UTC with milliseconds (`2026-01-02T12:00:01.000Z`) | none |

Unchanged and as proposed: the login body and `expires_at`; `getHeartRateTrace`'s wire shape
with `kind: "reconstructed"`; `getCuffAttemptTrace`'s wire shape; `CommitAck` (`raw` decoded,
`controlId` null in a log row); `application` from the labels with `pending`; cuff refusals;
`502`/`504` for cuff-sim; `messageName` only ever store-returned and `derivedMessageName` only
derived.

### Answers to the dashboard's open questions

1. **Patient parameter.** Every search is **pinned to the configured patient** (`PATIENT_ID`).
   The proxy finds the Patient's server id by identifier
   (`Patient?identifier=urn:pulsebridge:patient|<id>`, the setup step's record) and adds
   `subject=Patient/<id>` itself; `patient` and `subject` from the caller are refused (`400`).
   Traces of another patient's resources answer `404`.
2. **Heart-rate source JSON.** Rebuilt from the stored Observation per health-sync's table and
   sent as `{"kind": "reconstructed", "window": {...}}` (`traces.reconstruct_window`; a test
   rebuilds T06's own mapping of a roll-up item exactly). Any missing part gives `source: null`,
   never a partial window.
3. **"HL7v2 as sent".** Accepted send: the stored message (`view=RAW_ONLY`), by the store-returned
   name (HTTPS) or by the derived name once a `get` confirmed it (MLLP). A malformed sample not
   stored: T07's bytes, with the PID rebuilt by T07's `with_patient` exactly as cuff-sim does
   when the proxy has the same `PATIENT_*` variables (otherwise `null` for the samples
   `with_patient` changes; the seven it leaves alone are always shown). A built reading that was
   rejected or unacknowledged: `null` (T31 keeps no message bytes, on purpose).
4. **FHIR transaction rows.** From the `Provenance` each ingest Bundle writes: Provenances with
   the same `recorded` and the same assembler agent were written by one `executeBundle`, so they
   form one row; `resources` are their targets plus the Provenances; `httpStatus` is `null`
   (nothing records it). A transaction is all or nothing, so a Provenance proves it committed.
5. **Date rule.** The proxy passes `gt`/`ge`/`lt`/`le` to the store **and** applies R4's rule to
   every result itself (`search.matches_dates`): on a Period, `gt v` matches a period that ends
   after `v`, `lt v` one that starts before `v`. So an aligned 24 hours returns exactly 288
   windows whatever the store does (tested with 290 stored windows). Whether the store's own
   matching agrees is unverified; if it is narrower, windows could be missing (see "Not verified").
6. **Share failures and the CapabilityStatement route.** The CapabilityStatement is at
   `GET /fhir/metadata` and needs a token, as proposed. Share failures are T15's (the helpers
   for them are in "For T15").
7. **Forwarding cuff-sim's refusals.** Status, body and `Retry-After` (delay-seconds only) passed
   through for 200, 400, 413, 422, 429, 500; unreachable or any other status `502`; timeout or
   Cloud Run's own 504 `504`. Forward timeout 95 s by default (at least 90, at least 10 s below
   `REQUEST_TIMEOUT_SECONDS`). **Firebase Hosting has a 60-second request timeout** for Cloud
   Run rewrites ("Firebase Hosting is subject to a 60-second request timeout ... you'll receive
   an HTTPS status code 504", firebase.google.com/docs/hosting/cloud-run), below cuff-sim's 80 s
   worst case: see "For T22".
8. **`/status` from health-sync.** `googleHealth` is sent as `state`; `reason`, `notes`,
   `strandedPreliminary`, `checkedAt` passed on after checking each; health-sync not answering,
   any non-200, or a body that is not its shape → `state: "unknown"` with a `reason` (amendment 9).
   Never a 5xx from the proxy.
9. **`lastSyncAt`.** `meta.lastUpdated` of the most recently updated heart-rate Observation of
   the configured patient (`_sort=-_lastUpdated&_count=1`, Cloud Healthcare's documented default
   order), cached with the counts for `STATUS_CACHE_SECONDS` (60 s). It moves only when
   health-sync wrote a changed window.
10. **Store-only rows.** Listed: stored messages created in the page's time window
    (`create_time` filter) that no attempt on the page names, and that no AuditEvent recorded
    around their creation time names (one extra audit search per candidate, at most 25 per
    page), and that are not about another patient (`patientIds`).

## Reviewer login (D9, docs/spikes/s6.md)

- **The password** is read from the mounted secret file (`REVIEWER_PASSWORD_FILE`, version
  `latest`) and reused for `PASSWORD_CACHE_SECONDS` (30 s), so a new version takes effect within
  that time with no redeploy. Missing, empty, unreadable, not UTF-8 or over 1 KiB → `503`
  "Reviewer login is not configured". A trailing newline is not part of the password.
- **Comparison** is constant-time: `hmac.compare_digest` over the SHA-256 digests of both sides,
  so it always compares 32 bytes and leaks neither content nor length.
- **The token** is an HS256 JWT (PyJWT), claims `iss`, `aud`, `iat`, `exp`, `jti`, no personal
  data. Its key is `scrypt(password, salt = "pulsebridge/fhir-proxy/session-token/v1|" + issuer,
  N=2^14, r=8, p=1)`, 32 bytes, derived once per password. Why scrypt and not a plain HMAC of
  the password (s6's sketch): a token is an HMAC under that key, so anyone holding a token can
  test password guesses offline; a password KDF makes each guess cost ~16 MiB and tens of
  milliseconds. The long random password (s6: 24+ characters, at least 16 enforced) is still the
  real defence. Because the key comes from the password, **a new password version ends every
  session at once**. Verification accepts `HS256` only (no `none`, no `HS512`/`RS256` confusion),
  requires all claims, checks `iss`/`aud`, refuses a lifetime longer than configured, and checks
  expiry against an injected clock: valid while `now < exp`. Lifetime `SESSION_TTL_SECONDS`
  (28800 = 8 hours).
- **No cookie** is ever set (`Set-Cookie` is stripped from every response); the token travels
  only in `Authorization: Bearer`, so there is nothing for cross-site request forgery to ride on.
- **Rate limit** (`login_limit.py`): failed logins in a sliding window (`LOGIN_WINDOW_SECONDS`,
  60), 5 per client address and 60 in total per instance. Checked **before** the password: once
  an address has 5 failures, its sixth attempt and every one after it in the window get `429`
  with `Retry-After` (whole seconds until the oldest failure ages out), right password or not.
  A success does not clear the count (a NAT-mate attacker could otherwise reset it on every
  reviewer login). IPv6 clients are counted per /64.
- **The client address** (`client_address.py`): `X-Forwarded-For` is appended to by each proxy,
  so only right-hand entries are trustworthy. Google documents the load balancer's behaviour
  ("appends two IP addresses ... `EXISTING_VALUE, CLIENT_IP, LOAD_BALANCER_IP`" and "does not
  verify any IP addresses that precede", docs.cloud.google.com/load-balancing/docs/https,
  "X-Forwarded-For header", read 2026-11-15). Cloud Run's own documentation (container contract,
  HTTPS invocation) says nothing about the header; a Google developer forum thread reports that
  `*.run.app` appends exactly one entry, the observed client address. So the default
  `TRUSTED_PROXY_HOPS=1` takes the **last** entry; `2` takes the second from the right (one
  more proxy in front); `0` ignores the header (no proxy, local use). A spoofed left-hand entry
  never moves the key (tested). Failed logins log the address and the number of XFF entries,
  never the values to their left, so T22 can see the real shape behind Firebase Hosting.
- **Logs** record `login-failed` (client address, XFF entry count), `login-rate-limited`,
  `login-succeeded`, `token-refused` (reason code); never a submitted password or a token.

What it is not: per-user sign-in. Everyone with the password is the same "reviewer"; nothing
records who viewed what. A real multi-patient deployment would use SMART on FHIR or an identity
provider and an audit log. The CapabilityStatement says so and claims no
`restful-security-service` code.

## CORS and response headers

- `Access-Control-Allow-Origin` is set only to an origin listed **exactly** in
  `ALLOWED_ORIGINS` (never `*`); no `Allow-Credentials` (a bearer token is attached by script,
  not by the browser); `Vary: Origin` on every response; `Retry-After` and `WWW-Authenticate`
  exposed. Preflight: the route's one method, headers `Authorization` and `Content-Type` only,
  `Max-Age` 600.
- On every response: `Cache-Control: no-store` (also keeps Firebase Hosting's CDN from caching
  health data), `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none';
  frame-ancestors 'none'`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, HSTS. No
  server version (gunicorn's `Server` header carries none; Cloud Run's front end adds its own).

## Robustness

- Request bodies are capped at 16 KiB (cuff-sim's own limit; `413` in cuff-sim's shape), login
  bodies at 1 KiB. Login JSON is strict: UTF-8, no `NaN`, no repeated keys, exactly one field.
- Ids from the path must be FHIR ids (1-64 of `[A-Za-z0-9.-]`) **with at least one letter or
  digit**: `..` is a valid R4 id pattern but would climb a path segment once in a URL.
  `pulsebridge_gcp`'s own check allows it (a follow-up for that library).
- No route lets a caller name a store, a resource type, a patient or a path: stores come from
  configuration, search parameters are an allow-list, message names are fetched only when they
  lie inside the configured HL7v2 store (whatever an AuditEvent says), and paging tokens are
  checked before they go back to the store.
- Upstream Cloud Healthcare failures become bounded reasons by error class:
  `upstream-permission-denied` (502), `not-found` (404), `upstream-unavailable` (503),
  `upstream-timeout` (504), `upstream-bad-answer` / `upstream-refused` (502). The store's text,
  the project, dataset and store names and upstream URLs are never in an answer.
- Every upstream call has a deadline: `FHIR_TIMEOUT_SECONDS` per Cloud Healthcare request (no
  retries), the cuff and health-sync timeouts, 10 s for minting an identity token. An answer over
  256 KiB from cuff-sim or health-sync is refused. Redirects are never followed.
- An unexpected exception is logged by class name only and answered `500`.

## How the joins work

- **Attempt → stored message**: HTTPS by T31's `message_name` (the store returned it); MLLP by
  `derived_message_name`, always confirmed with `messages.get` before its labels are used.
  A control ID is never a key (T07's samples share them).
- **Stored message → Observation**: v2-to-fhir keys the Observation on UUIDv5(patient key,
  `85354-9`, `[MSH-3.1, MSH-4.1, MSH-10]`) (T06/T12). The proxy computes the same identifier from
  the stored bytes (read with hl7apy) and searches `Observation?identifier=...`, which Cloud
  Healthcare answers from its synchronous identifier index. The Provenance is read by its
  deterministic id (UUIDv5 of the Observation's key) and must point back at the Observation. A
  test maps T07's samples with v2-to-fhir's real mapper and checks the keys agree.
- **Paging the log**: the cursor is an instant plus the ids already shown at that instant
  (base64url JSON, checked strictly). Each source is asked for its newest items at or before it;
  only items no source could still be missing are shown (a source that filled its page may have
  more just below its last item).

## For T15: how `share/` plugs in

- Create the package **`services/fhir-proxy/fhir_proxy/share/`** (a subpackage, so it is
  installed with the service). The work plan names `services/fhir-proxy/share/`; that path
  cannot be a subpackage of `fhir_proxy`, so **the orchestrator should amend T15's Owns** to
  `services/fhir-proxy/fhir_proxy/share/` (plus its tests, for example
  `services/fhir-proxy/tests/share/`).
- Its `__init__.py` defines `register(proxy: ProxyContext) -> None`. `create_app` imports it if
  present and calls it once at start-up (`extensions.load_extension`). In `register`:
  - `proxy.add_route(Route("POST", "/share", "share", "view", "<summary>"), handler)`: the route
    joins the same table, so it gets the token check (an extension cannot add a public route),
    CORS, the headers, error handling and a line in the CapabilityStatement.
  - `proxy.set_openemr_probe(fn)`: `fn()` returns `{"reachable", "checkedAt", "detail"}` for the
    status strip.
  - `proxy.stores`: `patient_references()`, `read(type, id)`, `is_ours(resource, refs)`: read
    the Observations to share with the patient pin.
  - `proxy.config`: the proxy's configuration; read T15's own variables (OpenEMR URL, audience,
    CA, key file) in its own config module and fail at `register` time, naming variables only.
- Refuse with `raise Problem(422, "required", "<reason>", "<fixed sentence>")` before calling
  OpenEMR, `Problem(502, ...)` when it is unreachable (`fhir_proxy.errors`).
- Build each trace step with `extensions.trace_step(...)`: the dashboard's `ShareStep` shape,
  with `client_assertion`, `access_token`, `refresh_token`, `id_token`, `assertion` and
  `private_key` replaced by `[not shown]` at any depth, and `url` required to be a path.
- Log with `fhir_proxy.logs.log(level, event, **codes)`; never a token, key, assertion or value.

## For T22: deploying it

**Environment variables** (all checked at start-up; errors name the variable, never the value):

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `GCP_PROJECT_ID` | yes | | project of the Healthcare dataset |
| `HEALTHCARE_LOCATION` | yes | | dataset region |
| `HEALTHCARE_DATASET_ID` | yes | | dataset id |
| `FHIR_STORE_ID` | yes | | the health-data FHIR store |
| `AUDIT_FHIR_STORE_ID` | yes | | the audit FHIR store (D15); must differ |
| `HL7V2_STORE_ID` | yes | | the HL7v2 store |
| `PATIENT_ID` | yes | | the configured patient (the same as cuff-sim's and v2-to-fhir's) |
| `REVIEWER_PASSWORD_FILE` | yes | | absolute path of the mounted `reviewer-password` secret file |
| `ALLOWED_ORIGINS` | yes | | comma-separated exact origins (`https://...`; `http://localhost:<port>` for development) |
| `CUFF_SIM_URL` | yes | | cuff-sim's Cloud Run URL (`https://...run.app`); also the identity token's audience |
| `HEALTH_SYNC_URL` | no | empty | health-sync's Cloud Run URL; empty: `state: unknown`, `health-sync-not-deployed` |
| `PATIENT_ASSIGNING_AUTHORITY`, `PATIENT_FAMILY_NAME`, `PATIENT_GIVEN_NAME` | no | empty | the same values as cuff-sim's; all or none. With them the trace shows a sent sample's exact bytes |
| `PATH_PREFIX` | no | empty | `/api` when the page reaches the proxy through the Firebase Hosting rewrite (Hosting forwards the full path) |
| `SESSION_TTL_SECONDS` | no | `28800` | token lifetime, 60-86400 |
| `TOKEN_ISSUER` | no | `pulsebridge-fhir-proxy` | JWT `iss`/`aud`; also salts the key derivation (set a deployment-specific value to defeat shared precomputation) |
| `PASSWORD_CACHE_SECONDS` | no | `30` | rotation delay, 0-300 |
| `TRUSTED_PROXY_HOPS` | no | `1` | see below |
| `LOGIN_FAILURES_PER_ADDRESS` / `LOGIN_FAILURES_TOTAL` / `LOGIN_WINDOW_SECONDS` | no | `5` / `60` / `60` | the login limit |
| `FHIR_TIMEOUT_SECONDS` | no | `10` | per Cloud Healthcare request, 1-30 |
| `CUFF_FORWARD_TIMEOUT_SECONDS` | no | `95` | 90-3000, at least 10 s below `REQUEST_TIMEOUT_SECONDS` |
| `SAMPLES_FORWARD_TIMEOUT_SECONDS` | no | `15` | |
| `HEALTH_SYNC_TIMEOUT_SECONDS` | no | `25` | health-sync budgets 20 s for `/status` |
| `REQUEST_TIMEOUT_SECONDS` | no | `120` | **set the Cloud Run request timeout to the same number** |
| `STATUS_CACHE_SECONDS` | no | `60` | reuse of the store-derived status |

**Secret mount.** Mount `reviewer-password` as a **file volume** with version **`latest`**
(not an environment variable, which is read once at start), and point
`REVIEWER_PASSWORD_FILE` at the file. Only the proxy's service account has
`secretAccessor` on it (`infra/modules/identity`).

**Invoker bindings the proxy needs**: `roles/run.invoker` for the `fhir-proxy` service account
on **cuff-sim** (D10, already in the identity README) **and on health-sync** (its README, "For
T20/T22"). The proxy itself is public (`allUsers` invoker): the session token is the gate.

**`TRUSTED_PROXY_HOPS`.** `1` when the browser calls `*.run.app` directly. Behind the Firebase
Hosting rewrite the right-most entry is probably Firebase's own egress address, which would put
every reviewer in one bucket: **T22 must check**. Deploy, make two failed logins (one with a
made-up `X-Forwarded-For: 192.0.2.1` header), and read the `login-failed` log lines: the
`forwarded_for_entries` count and which `client_address` came out tell you whether to set `2`.
While `*.run.app` stays reachable directly, a hop count of 2 lets a direct caller choose its own
key (the left of two entries); the total limit still bounds that.

**Request timeout and Firebase Hosting.** Set the proxy's Cloud Run request timeout to
`REQUEST_TIMEOUT_SECONDS` (120). But Firebase Hosting cuts a rewritten request at **60 s**
(documented). cuff-sim's worst case is 80 s (HTTPS with a token refresh and 401 retry on both
calls), so a slow cuff send through the rewrite can end in Firebase's 504 while the send may
still complete. Options for T22/the orchestrator: lower cuff-sim's `HTTPS_TIMEOUT_SECONDS`
(for example to 6: worst case 48 s) and keep the rewrite; or have the page call the proxy's
`*.run.app` URL for the cuff route (CORS is already in place). This is not decided here.

**Instances.** `max_instance_count = 2` (s6). The login limit is in memory per process and the
image runs one worker, so the real per-address limit is up to 5 × instances a minute and the
total up to 60 × instances. Do not raise `--workers`.

**Ingress.** The proxy needs direct VPC egress only if T15's OpenEMR calls need it; T14's calls
(Cloud Healthcare, the two Cloud Run services) go over Google's public endpoints.

## For T24/T35: the image

No Dockerfile is added by T14 (the `fhir-proxy` entry in `tests/tooling/test_container_images.py`
`NO_IMAGE_YET` is left as it is). Following services/README.md "Adding an image":

- `COPY` and install `libs/pulsebridge_fhir`, `libs/pulsebridge_hl7v2` (with its `samples/`
  package data: the cuff trace loads T07's samples) and `libs/pulsebridge_gcp`, then
  `services/fhir-proxy`.
- `CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "0", "fhir_proxy.app:create_app()"]`
  (the "Running" command without `--bind`). **One worker**: the login limit is per process.
- `.dockerignore` allow-list: `services/fhir-proxy/pyproject.toml`, `README.md`, `fhir_proxy/`.
- Smoke case: `GET /status` with no token answers `401` without calling Google; start-up with
  `CUFF_SIM_URL` unset fails naming `CUFF_SIM_URL`.
- `fhir_proxy/testing.py` is in the package (as in health-sync); it imports nothing beyond the
  service's own dependencies and does nothing unless called.

## For T18 and T19

- `derivedMessageName` is shown labelled as derived; `messageLookup` says whether the store
  confirmed it (amendment 1). An accepted MLLP row with `not-found` has no application outcome.
- Store-only rows have no trace route (amendment 5). Unreadable rows are error rows.
- Follow `nextCursor` until null; a filtered page can be short or empty (amendment 4).
- `pending` is a real state: no label yet (mapper not run, failed or dead-lettered).

## Running

```bash
gunicorn --bind ":${PORT:-8080}" --workers 1 --threads 8 --timeout 0 "fhir_proxy.app:create_app()"
```

One worker (the login limit is per process), eight threads (the messages log looks up stored
messages in parallel), `--timeout 0` (Cloud Run's request timeout is the limit). Credentials are
Application Default Credentials: on Cloud Run the `fhir-proxy` service account (FHIR reader on
both FHIR stores, HL7v2 consumer, secret accessor on `reviewer-password`); identity tokens for
cuff-sim and health-sync come from the metadata server. No key file.

## Logging

One JSON object per line on stdout. Events: `login-failed` (`client_address`,
`forwarded_for_entries`), `login-rate-limited` (`client_address`, `retry_after`),
`login-succeeded`, `token-refused` (`reason`), `reviewer-password-unavailable` (`problem`:
`missing`/`empty`/`unreadable`), `unreadable-send-attempt` (`attempt_id`), `attempts-skipped`
(`count`), `message-lookup-failed` (`error_type`), `store-only-scan-truncated`,
`store-only-checks-capped`, `paging-link-unrecognised`, `health-sync-status` (`outcome`,
`http_status`), `fhir-store-status`, `cuff-forwarded` / `cuff-forward-failed` (`route`,
`http_status`, `reason`, `duration_ms`), `openemr-probe-failed`, `bug` (`error_type`,
`endpoint`). Never logged: passwords, tokens, the patient identifier or name, observation
values, message or ACK text, request or response bodies, query strings, exception messages,
server error text. A test pushes distinctive values through every route, including a
deliberate exception that quotes them, and checks every record with and without its traceback.

## Tests

`make test` runs `tests/`. The real `HealthcareClient` runs over an in-memory fake of the three
stores (`fhir_proxy.testing.FakeHealthcare`), which applies **no** date filter so the proxy's own
R4 rule is what is tested. Heart-rate data is written through T06's real `heart_rate_transaction`;
blood-pressure data through v2-to-fhir's real `map_message` over T07's samples; the five
AuditEvent fixtures are T31's examples as committed. cuff-sim and health-sync are a fake sender;
the real `requests` sender is tested against an HTTP server on 127.0.0.1 (bytes, timeout, no
redirect, size cap). All data is synthetic; the test password is obviously fake.

## Not verified

T14 made no GCP call. Everything about Google's services is from documentation or other tasks'
notes, and must be confirmed on the first deployment (T25):

- **X-Forwarded-For on `*.run.app`** (one appended entry) and **behind Firebase Hosting**: from a
  forum report and reasoning, not Google's Cloud Run documentation. See "For T22".
- That Firebase Hosting's rewrite forwards the `Authorization` header and the full path, and its
  60 s timeout in practice.
- Cloud Healthcare FHIR search: `gt`/`lt` on `effectivePeriod` (the proxy re-applies R4's rule,
  but cannot add windows the store left out); `_sort=date` on Observation; reusing a
  `_page_token` from a `next` link with the proxy's own parameter order; `_summary=count` giving
  `total`; `_sort=-_lastUpdated` with `_elements=id,meta` returning `meta.lastUpdated`;
  identifier OR lists; AuditEvent `type`/`subtype`/`date`/`_sort=-date` (T31 flagged these);
  Provenance `recorded` search and sort. **Indexing delay**: Cloud Healthcare indexes
  non-identifier searches asynchronously (its search documentation), so a just-written reading
  may lag in the Observation search, the counts and `lastSyncAt`; identifier lookups do not lag.
- HL7v2 `messages.list` with a `create_time` filter (only accepted syntax was checked, spike S5)
  and with `view=RAW_ONLY`; `hl7V2Consumer` allowing list and get.
- The derived MLLP message name rule (spike S5, one message): the proxy confirms it with a
  `get` and reports `not-found` when wrong, so a wrong rule shows up rather than misleads.
- That Cloud Run accepts an identity token whose audience is the service URL, and that the
  metadata server mints one for the proxy's service account.
- That a mounted secret file reflects a new `latest` version on read without a restart (s6,
  from a search summary).
- Least-privilege roles: that `fhirResourceReader` allows every search above.
