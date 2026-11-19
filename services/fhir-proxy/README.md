# fhir-proxy

The dashboard's only backend (task T14, decision D10), and the only public service in
PulseBridge. It runs an invisible bot check and gives a visitor an anonymous 24-hour demo
session in a cookie (D16, task T43; this replaced D9's shared reviewer password), serves a narrow read API over the two FHIR stores and the HL7v2 store, assembles the messages
log, and forwards cuff readings to the private cuff simulator with its own identity. The share
to OpenEMR (T15) plugs in as an extension (see "Share to OpenEMR (T15)").

```
browser ──HTTPS, same origin, Cookie: __session=<token>──> fhir-proxy (behind Hosting's /api)
                                                           │ token check (HS256, key = the mounted secret)
         mounted secret file (session-signing-key) ────────┤
Fraud Defense (reCAPTCHA)  <── assessments.create ─────────┤ POST /auth/session only
Cloud Storage (demo-counters) <── one counter object a day ┤ POST /auth/session only
FHIR store (health data)   <── read/search, patient-pinned ┤
audit FHIR store (D15)     <── AuditEvent search/read ─────┤
HL7v2 store                <── messages.get / list ────────┤
cuff-sim  (private)        <── OIDC identity token ────────┤ POST /cuff/readings, GET /cuff/samples
health-sync (private)      <── OIDC identity token ────────┘ GET /status
```

It is **read-only** (D12): it writes nothing to any of our stores. Its one write is the share,
which creates a Patient, an Encounter and an Observation in OpenEMR (never updates or deletes).
Every input is treated as hostile.

## Layout

```text
services/fhir-proxy/
  pyproject.toml        the three libs, Flask, gunicorn, PyJWT, cryptography (exact pins)
  fhir_proxy/
    config.py           environment variables, checked at start-up (errors name variables only)
    signing_key.py      the mounted session-signing-key file, re-read after a short cache
    session_token.py    the session token: HS256 JWT signed with that key
    bot_check.py        the Fraud Defense assessment and the pass rule (valid, action, host, score)
    demo_counter.py     the daily bot-check cap: a counter object per UTC day in Cloud Storage
    session_limit.py    the per-address limit on POST /auth/session (a sliding hour)
    flood_log.py        one log line per address per minute under a flood of refusals
    client_address.py   which X-Forwarded-For entry is the client (TRUSTED_PROXY_HOPS)
    search.py           GET /fhir/Observation: strict validation, the R4 date rule, paging links
    store.py            reads from the three stores; the patient pin
    hl7.py              stored message -> control ID, and the Observation identifier v2-to-fhir writes
    messages.py         the messages log: AuditEvents + stored messages + transactions, newest first
    traces.py           heart-rate trace (reconstructed roll-up window) and cuff attempt trace
    status.py           GET /status: health-sync's state, counts, newest heart-rate write
    upstream.py         identity tokens and the calls to cuff-sim and health-sync
    capability.py       the CapabilityStatement, generated from the route table
    routes.py           the route table (the app and the CapabilityStatement both read it)
    extensions.py       where T15's share plugs in; the share-trace helper and its redaction
    share/              T15: POST /share to OpenEMR, the token, the pinned TLS, the probe
    errors.py           OperationOutcome with fixed wording; upstream errors to bounded reasons
    logs.py             JSON log lines, values never logged
    app.py              Flask app: the gate (cookie, Origin), security headers, thin handlers
    testing.py          test fakes (synthetic data only), like pulsebridge_gcp.testing
  tests/                pytest; no network beyond 127.0.0.1, no credentials, no GCP
```

## Routes as built

Paths are relative to `PATH_PREFIX` (empty by default; `/api` behind the Firebase Hosting
rewrite, see "For T22"). Every route except the three under `/auth/` needs a valid `__session`
cookie and answers `401 session-required` otherwise, **including** `GET /fhir/metadata` and
unknown paths. Every `POST` (any method but `GET` and `HEAD`) also needs an `Origin` header
that is exactly one of `ALLOWED_ORIGINS`, otherwise `403 origin-refused`, checked before the
cookie. Every error body is a FHIR
`OperationOutcome` whose `issue[0].details.coding[0]` (system `urn:pulsebridge:fhir-proxy:reason`)
is a fixed reason code and whose `diagnostics` is a fixed sentence.

| Method and path | Answer |
| --- | --- |
| `GET /auth/config` | no session: `{"recaptcha_site_key", "recaptcha_action"}` (the site key is public; the page fetches it rather than building it in) |
| `POST /auth/session` | no session: `{"recaptcha_token": "..."}` (exactly that, strict JSON, `application/json`, at most 10 KiB) → `200 {"expires_at"}` and `Set-Cookie: __session=<token>; Path=<PATH_PREFIX or />; Max-Age=86400; HttpOnly; Secure; SameSite=Strict`; `400 invalid-body`; `403 origin-refused`; `403 bot-check-failed`; `429 rate-limited` + `Retry-After`; `503 demo-busy` ("The demo is busy, try again tomorrow.", `Retry-After` to the next UTC midnight); `503 bot-check-unavailable`; `503 session-not-configured` (no usable signing key); `413` |
| `POST /auth/logout` | no session needed (so an expired cookie can be cleared), `Origin` needed: `204`, `Set-Cookie: __session=; Path=...; Max-Age=0; HttpOnly; Secure; SameSite=Strict` |
| `GET /status` | always `200` once authenticated: `{"watchSync": {state, reason, notes, strandedPreliminary, checkedAt, lastSyncAt}, "observationCounts": {watch: number \| null, cuff: number \| null}, "fhirStore": {reachable, checkedAt, detail}, "openemr": {...}}` |
| `GET /fhir/metadata` | the CapabilityStatement (`application/fhir+json`) |
| `GET /fhir/Observation?code=&date=&_sort=&_count=` | `searchset` Bundle of the configured patient's Observations; each entry has `fullUrl` = `PUBLIC_BASE_URL/fhir/Observation/<id>`; `link` `self` and `next` are path-absolute links to the proxy; `400` OperationOutcome for anything unsupported |
| `GET /fhir/Observation/{id}` | the Observation, if it is the configured patient's (`404` otherwise): what `fullUrl` names |
| `GET /trace/heart-rate/{observationId}` | `{"resources": Bundle (collection: Observation, Provenance?, Device?), "source": {"kind": "reconstructed", "window"} \| null}`; `404` not found, another patient's, or not a heart rate |
| `GET /trace/cuff-attempts/{attemptId}` | `{"attempt": SendAttemptEntry, "messageSent": "<ER7>" \| null, "resources": Bundle (collection: AuditEvent as stored, Observation?, Provenance?)}`; `404`; `422` AuditEvent unreadable |
| `GET /messages?kind=&outcome=&count=&cursor=` | `{"entries": [...], "nextCursor": string \| null, "unreadable": number, "partial": boolean}`, newest first by send time |
| `GET /cuff/samples` | cuff-sim's list, forwarded |
| `POST /share` | `{"observationId"}` → `200 {"outcome", "steps", "readBack"}`; `400`/`404`/`422`/`503` OperationOutcome before OpenEMR is called; `502`/`504` `{"operationOutcome", "steps"}` after (see "Share to OpenEMR (T15)") |
| `POST /cuff/readings` | forwarded to cuff-sim unchanged; its `200/400/413/422/429/500`, body and `Retry-After` unchanged; `502` cuff-sim unreachable or unusable; `504` timed out (may have been sent) |
| `OPTIONS <any path>` | not a CORS preflight any more: it goes through the gate like any other method (`403 origin-refused` without an allowed `Origin`, else `401` or `405`), and no `Access-Control-*` header is ever sent |

A token in the query string (`access_token`, `token`, `id_token`, `__session`) is refused with
`400 token-in-query`, even beside a valid cookie and on the public routes. `Authorization:
Bearer` is not accepted (a request carrying only that is `401 session-required`).

**Time budget.** Every request has one budget for all its upstream calls together:
`CUFF_FORWARD_TIMEOUT_SECONDS` (55 s) for `POST /cuff/readings`, `REQUEST_BUDGET_SECONDS`
(50 s) for everything else, both under Firebase Hosting's 60 s. Each Cloud Healthcare call gets
`min(FHIR_TIMEOUT_SECONDS, time left)`, and no call starts once the budget is spent
(`fhir_proxy.deadline`). Independent calls run at the same time. A route that runs out answers
with what is complete: `/status` reports the missing parts as unknown (`state: "unknown"`,
counts `null`), `/messages` returns its page with `partial: true`, and a route that has nothing
complete answers `504` (`upstream-timeout`).

### Contract amendments for the dashboard (web/README.md, contract.ts, mock-client.ts)

Each is a deliberate change from T16/T34's proposal; the client and mock should follow.

| # | What | Before (proposal / mock) | After (built) | Who follows |
| --- | --- | --- | --- | --- |
| 1 | `SendAttemptEntry.messageLookup` | absent | **new**: `'confirmed' \| 'not-found' \| 'failed' \| null`. `confirmed`: the store has this attempt's name (its labels are the outcome). `not-found`: it does not (the derived MLLP name was wrong, or the message was deleted). `failed`: the store could not be asked in time. `null`: no name (rejected, no acknowledgment). An accepted row whose lookup is `not-found` or `failed` has `application: null` and `observation: null`, and matches **no** `outcome` filter (see 19) | T19, mock |
| 2 | `FhirTransactionEntry.writer` | `'health-sync' \| 'v2-to-fhir'` | `'health-sync' \| 'v2-to-fhir' \| 'seed' \| 'unknown'` (the seeder's assembler is `PulseBridge seed (synthetic data)`) | T19, contract |
| 3 | Messages `count` | default 50, cap 100 (mock) | default **25**, cap **50** | T19, mock |
| 4 | Filtered log pages | mock filters a full list | with `outcome` or `kind`, a page may hold **fewer than `count` rows, even none, while `nextCursor` is not null**: the filter runs after the join. Follow `nextCursor` until null | T19, mock |
| 5 | `store-only` rows | not produced by the mock | produced. `id` = the stored message name, `transport` `'https'` when the message has cuff-sim's `transport` label, else `null`; `controlId` read from the bytes; `messageLookup: 'confirmed'`; `application` from **its own** labels. Sorted by the message's `createTime`. **No trace route**: `getCuffAttemptTrace` takes AuditEvent ids, so do not put a store-only row in the selection. Only messages that no attempt anywhere in the audit store claims are listed | T19, mock |
| 6 | `unreadable-send-attempt.id` | string | string, `""` when the AuditEvent had no id | T19 |
| 7 | Search refusals | mock: `422` for an unsupported code | **`400`** with an OperationOutcome (R4: a refused search parameter is a 400), `issue.code` `not-supported` or `invalid` | mock |
| 8 | Search Bundle | `link[next]` under the base URL, no `fullUrl` | `link` has `self` and `next` as **path-absolute** URLs (`/api/fhir/Observation?...`), never built from `Host`; **every entry has `fullUrl`** `"<PUBLIC_BASE_URL>/fhir/Observation/<id>"` (R4: SHALL for search results), which the new read route answers; no `total` (entries are post-filtered) | none (the client already resolves relative links; it can ignore `fullUrl`) |
| 9 | `watchSync.reason` when health-sync fails | `health-sync-unreachable` (example) | one of `health-sync-timeout` (no answer, or the request's budget ran out), `health-sync-unreachable`, `health-sync-error` (non-200, or any unexpected failure), `health-sync-bad-answer`, `health-sync-identity-token-unavailable`, `health-sync-not-deployed`; `state` is `unknown` | T17 (shows it as detail text) |
| 10 | `openemr` in `/status` | mock: reachable | `{"reachable": false, "detail": "not-checked"}` until T15 installs a probe; since T15 a real probe (`not-configured` without the settings; codes in "Share to OpenEMR") | T15 (done) |
| 11 | `503` on protected routes | only login | when the signing-key secret is missing or unusable **every** route answers `503` (`session-not-configured`; D9's `login-not-configured` until T43): no token can be checked | none (an `ApiError`) |
| 12 | Cuff trace of an unreadable AuditEvent | `404` (mock) | **`422`** (`unreadable-send-attempt`); the row is in the log. The page treats only 404/410 as final today: it should treat 422 as final too (retrying cannot help) | T18 follow-up |
| 13 | `lastSyncAt` | ISO | ISO in UTC with milliseconds (`2026-01-02T12:00:01.000Z`) | none |
| 14 | `store-only` row `possibleDuplicateOf` | absent | **new**: `string \| null`, an AuditEvent id. Set only when exactly **one** attempt qualifies as the one this message may belong to: an accepted MLLP attempt with the same control ID, whose own derived name the store does not have (`not-found`), sent at most 120 s before the message was created. Show it as a hint ("this may be the message of that send"); it moves nothing: the attempt keeps `application: null`, the message keeps its own outcome. Two or more candidates give `null` | T19, contract |
| 15 | `observationCounts` | `{watch: number, cuff: number}` | `{watch: number \| null, cuff: number \| null}`: `null` is "not known" (the store could not be asked in time), never 0. `0` means asked and none (also: no Patient yet) | T16 shell, contract, `parse.ts` (`count()` today turns null into 0) |
| 16 | `MessagesLogPage.partial` | absent | **new** `boolean`: true when the page was cut short by the time budget or a store answer that could not be read completely. Rows of its window may be missing (lookups not finished show `messageLookup: "failed"`; unchecked store-only rows are left out, never guessed). Reloading may fill it in | T19, contract |
| 17 | Messages order | `recordedAt` desc (mock) | **send time** desc (the AC): attempt rows by `sentAt`, store-only rows by the message's `createTime` (`recordedAt` on the row), transaction rows by `recordedAt`; ties by `id` desc | T19, mock |
| 18 | Transaction rows | every Provenance in the store | **pinned to the configured patient**: only Provenances whose targets are the patient's Observations; `resources` lists only those | none |
| 19 | `outcome` filter | matches `application.outcome` | matches only rows **with** an application-level outcome equal to it. A row with `application: null` (rejected, unacknowledged, or a name the store could not confirm) matches no filter, `pending` included: pending means "stored, not labelled yet", which is not known for such a row | T19, mock |
| 20 | `GET /fhir/Observation/{id}` | absent | **new** read route, what each search `fullUrl` names: the Observation if it is the configured patient's, else `404` (another patient's answers like a missing one); any query parameter (`_format`, `_elements`, `_summary`, ...) `400`; `ETag: W/"<meta.versionId>"` and `Last-Modified` from `meta.lastUpdated` when present; listed as `read` in the CapabilityStatement | none (the page need not call it) |
| 21 | Cursor | opaque | still opaque; a cursor whose instant is before 1970 or more than a day ahead is refused with `400` (`invalid-cursor`) | none |

Unchanged and as proposed: `expires_at` (now from `POST /auth/session`); `getHeartRateTrace`'s wire shape
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
   windows whatever the store does (tested with 290 stored windows). At an exact boundary the
   proxy's rule is inclusive for `ge`/`le` (a window ending exactly at a `ge` value, or starting
   exactly at an `le` value, is returned) and exclusive for `gt`/`lt`. Whether the store's own
   matching agrees is unverified; if it is narrower, windows could be missing (see "Not verified").
6. **Share failures and the CapabilityStatement route.** The CapabilityStatement is at
   `GET /fhir/metadata` and needs a token, as proposed. Share failures are T15's (the helpers
   for them are in "For T15").
7. **Forwarding cuff-sim's refusals.** Status, body and `Retry-After` (delay-seconds only) passed
   through for 200, 400, 413, 422, 429, 500; unreachable or any other status, a JSON one
   included, `502`; timeout or Cloud Run's own 504 `504`. **Firebase Hosting has a 60-second
   request timeout** for Cloud Run rewrites ("Firebase Hosting is subject to a 60-second
   request timeout ... you'll receive an HTTPS status code 504",
   firebase.google.com/docs/hosting/cloud-run), so the forward timeout defaults to **55 s**,
   and cuff-sim's own timeouts must be lowered to fit under it (the arithmetic is in "For T22").
   This departs from the work plan's "at least 90 s", which assumed no Hosting in front.
8. **`/status` from health-sync.** `googleHealth` is sent as `state`; `reason`, `notes`,
   `strandedPreliminary`, `checkedAt` passed on after checking each; health-sync not answering,
   any non-200, or a body that is not its shape → `state: "unknown"` with a `reason` (amendment 9).
   Never a 5xx from the proxy.
9. **`lastSyncAt`.** `meta.lastUpdated` of the most recently updated heart-rate Observation of
   the configured patient (`_sort=-_lastUpdated&_count=1`, Cloud Healthcare's documented default
   order), cached with the counts for `STATUS_CACHE_SECONDS` (60 s). It moves only when
   health-sync wrote a changed window.
10. **Store-only rows.** Listed: stored messages created in the page's time window
    (`create_time` filter) that no attempt on the page names, that are not about another
    patient (`patientIds`), and that no AuditEvent recorded around their creation names. That
    last check is **one** audit search over the span of all candidates (oldest − 60 s to newest
    + 10 min), read to its end; if it cannot be read completely, no message is called
    store-only and the page says `partial: true`.

## Demo sessions (D16, T43)

The demo is public: no password, no sign-up. A visitor clicks "View the live demo"; the page
runs an invisible bot check in the browser, and the proxy, after asking Google about it, sets a
signed 24-hour session cookie. The author accepts that their own data in the shown window is
public (D16). The contract the proxy, the page (T45) and the infrastructure (T44) share is in
docs/workplan.md, "The contract T43 to T45 share".

### The flow of `POST /auth/session`

Every step is cheaper than the next, so a flood of junk is refused before it costs anything:

1. **`Origin`** must be one of `ALLOWED_ORIGINS` (the gate, for every `POST`): `403
   origin-refused`. A refused request uses none of the address's attempts.
2. **Per-address limit** (`session_limit.py`): `SESSIONS_PER_ADDRESS_PER_HOUR` (5) attempts per
   client address in a sliding hour, checked and recorded under one lock before the body is
   read. **Every attempt counts**, a pass included: counting only failures would let a bot that
   passes mint sessions without limit, and a visitor needs one session a day. The next attempt
   gets `429 rate-limited` with `Retry-After` (whole seconds until the oldest attempt ages
   out). The address is the same key as D9's login limit (`client_address.py`, below), an IPv6
   /64 counted as one. The table is capped at 10,000 addresses; only fully aged-out entries
   make room, so a flood of fresh addresses cannot evict an abuser's history (a new address is
   refused for 60 s while the table is full of live entries). In memory per process: the real
   limit is up to 5 x instances. That is acceptable because the daily cap, which spans
   instances, bounds the cost.
3. **The body** `{"recaptcha_token": "<text>"}`, strict JSON, nothing else: `400 invalid-body`.
4. **A shape check of the token** (`[A-Za-z0-9_-]{20,8192}`): anything else is `403
   bot-check-failed` without an assessment or a count.
5. **The signing key** is read now: without one no session could be issued, so nothing is
   spent (`503 session-not-configured`).
6. **The daily cap** (`demo_counter.py`): one more assessment is counted for today **before**
   it is made. Reached: `503 demo-busy`, "The demo is busy, try again tomorrow.", with
   `Retry-After` until the next UTC midnight. Counter unreachable: `503 bot-check-unavailable`
   (fail closed: no counter, no assessment).
7. **The assessment** (`bot_check.py`). Fraud Defense unreachable, slow, refusing or answering
   nonsense: `503 bot-check-unavailable`. Not a pass: `403 bot-check-failed`.
8. **A pass**: `200 {"expires_at": "<RFC 3339>"}` and the cookie.

### Details the contract left open (for T44, T45 and the orchestrator)

- **Logout needs no session**, only an allowed `Origin`, so the page can always clear the
  cookie, an expired one included (with a valid session it behaves as the contract says).
- **`503 session-not-configured`** when the signing-key file is missing, shorter than 32 bytes
  or unreadable: on `POST /auth/session` (before anything is spent) and on every protected
  route. The page can treat it like any other 503.
- **`demo-busy` carries `Retry-After`** (seconds to the next UTC midnight); `413` when the body
  is over 10 KiB; a wrong `Content-Type` is `400 invalid-body`.
- **The key file's format**: the key is the file's bytes, at least 32; T44's script may store
  32 raw bytes or a base64 or hex text of them.
- **`Origin` is checked on every method but `GET` and `HEAD`**, not only `POST` (only `POST`
  routes exist).

### The bot check (Google Cloud Fraud Defense, formerly reCAPTCHA Enterprise)

Terms. The **site key** (`RECAPTCHA_SITE_KEY`) identifies a score-based web key; it is public,
because the page hands it to Google's script. That script runs in the visitor's browser,
watches how the page is used, and returns a **token**: a one-time, short-lived proof of what it
saw, tied to an **action** name (`view_demo`). The browser cannot be trusted to say "I passed",
so the proxy asks Google itself by creating an **assessment**: it sends the token and gets back
whether the token is genuine, which action and page host it was made for, and a **score** from
0.0 (very likely a bot) to 1.0 (very likely a person).

The request (`POST https://recaptchaenterprise.googleapis.com/v1/projects/<GCP_PROJECT_ID>/assessments`,
OAuth scope `cloud-platform`):

```json
{"event": {"token": "<token>", "siteKey": "<RECAPTCHA_SITE_KEY>", "expectedAction": "view_demo"}}
```

The fields of the answer the proxy reads (protobuf's JSON form leaves out default values, so a
missing `valid` is false and a missing `score` is 0.0):

```json
{"name": "projects/<number>/assessments/<id>",
 "tokenProperties": {"valid": true, "invalidReason": "INVALID_REASON_UNSPECIFIED",
                     "hostname": "<site>.web.app", "action": "view_demo", "createTime": "..."},
 "riskAnalysis": {"score": 0.9, "reasons": []}}
```

**A pass needs all four**: `tokenProperties.valid` is true; `action` is `RECAPTCHA_ACTION` (a
token made for another action is not replayable here); `hostname` is the host of one of
`ALLOWED_ORIGINS` (compared without case; someone copying our page elsewhere with our public
site key gets tokens for their own host); `riskAnalysis.score` is at least `RECAPTCHA_MIN_SCORE`
(0.5, inclusive). The caller is told only `bot-check-failed`, with the same bytes whatever
failed, so a bot learns nothing about what to change; the log says which (`invalid-token` with
Google's `invalidReason`, `wrong-action`, `wrong-hostname`, `low-score`) and the score, for
tuning the threshold. The visitor's IP address and user agent are not sent (both are optional
`event` fields): the token already carries what the browser script saw.

**The IAM role**: the proxy's service account needs `recaptchaenterprise.assessments.create`.
The narrowest predefined role that grants it is **reCAPTCHA Enterprise Agent**
(`roles/recaptchaenterprise.agent`, "Access to create and annotate reCAPTCHA Enterprise
assessments"); T44 grants it.

**Sources**, 19 November 2026. Google's documentation site (docs.cloud.google.com) could not be
fetched from the build session (its network does not allow that host), so the pages were read
through search results and checked against Google's own client library:

- REST reference, `projects.assessments.create`:
  https://docs.cloud.google.com/recaptcha/docs/reference/rest/v1/projects.assessments/create
- "Create assessments for websites" (the endpoint as `POST
  https://recaptchaenterprise.googleapis.com/v1/projects/PROJECT_ID/assessments`, the
  `tokenProperties` and `riskAnalysis` fields, one read per token):
  https://docs.cloud.google.com/recaptcha/docs/create-assessment
- "Google Cloud Fraud Defense roles and permissions" (`roles/recaptchaenterprise.agent`
  contains `recaptchaenterprise.assessments.create`):
  https://docs.cloud.google.com/iam/docs/roles-permissions/recaptchaenterprise, and "Access
  control with IAM": https://docs.cloud.google.com/recaptcha/docs/access-control
- `google-cloud-recaptcha-enterprise` 1.34.0 from PyPI (read, not installed): the field
  definitions of `Event` (`token`, `site_key`, `expected_action`, `user_agent`,
  `user_ip_address`), `TokenProperties` (`valid`, `invalid_reason` with `MALFORMED`, `EXPIRED`,
  `DUPE`, `MISSING`, `BROWSER_ERROR`, `hostname`, `action`, `create_time`), `RiskAnalysis`
  (`score`, "from 0.0 to 1.0 (1.0 means very likely legitimate traffic ...)") and the default
  host `recaptchaenterprise.googleapis.com`.

### The daily cap, and why not a counter in memory

`BOT_CHECKS_PER_DAY` (300) caps **assessments** (what costs money and what Google's free tier
counts), not sessions: a failed check is counted too. It must hold across instances and
restarts, which a counter in memory cannot: Cloud Run scales the proxy to **zero** when idle and
runs up to `max_instance_count` instances under load, so an in-memory count would start again
at 0 after every quiet spell and be kept separately per instance. A bot could reset the cap
simply by waiting for scale-to-zero. A Cloud Storage bucket shares the count, survives restarts
and costs nothing when idle (a database or Memorystore would bill by the hour).

One object per UTC day, `bot-checks/<YYYY-MM-DD>` in `DEMO_COUNTER_BUCKET`, the count in its
custom metadata (`metadata.count`). An increment cannot lose a count (**optimistic
concurrency**):

1. `objects.get` reads the count and the object's **generation** (a number Cloud Storage
   changes on every write); no object yet means count 0, generation 0.
2. At the cap: `demo-busy`.
3. A multipart `objects.insert` writes count + 1 with the **precondition**
   `ifGenerationMatch=<generation>`: Cloud Storage writes only if the object is still the one
   read (`0`: only if it still does not exist). If another instance wrote in between, the
   answer is `412` and nothing was written: read again and retry, at most 8 times.

Cloud Storage allows about one write a second to one object name (`429` above that, which waits
1 s and retries), so the counter admits about one assessment a second across all visitors:
plenty for a demo. The bucket's lifecycle rule deletes old days after 7 days (T44). Any other
failure is `bot-check-unavailable`, and so is an object whose count is not a number (fail
closed). Sources: https://docs.cloud.google.com/storage/docs/request-preconditions,
https://docs.cloud.google.com/storage/docs/json_api/v1/objects/insert, "Quotas and limits"
(https://docs.cloud.google.com/storage/quotas).

### The cookie and the session token

`Set-Cookie: __session=<token>; Path=/api; Max-Age=86400; HttpOnly; Secure; SameSite=Strict`

- **`__session`** is the only cookie name Firebase Hosting passes on to Cloud Run (it strips the
  others, and adds this one to its CDN cache key; firebase.google.com/docs/hosting/manage-cache,
  "Using cookies"). `Cache-Control: no-store` on every response keeps the CDN from caching anyway.
- **`Path`** is `PATH_PREFIX` (`/` when empty): the browser sends the cookie only to the proxy.
- **`Max-Age`** is `SESSION_TTL_SECONDS`, the token's own lifetime.
- **`HttpOnly`**: page script cannot read it, so a script injected into the page cannot steal
  the session. The page keeps no token at all; the browser attaches the cookie.
- **`Secure`**: sent over HTTPS only. **No `Domain`**: a host-only cookie.
- **`SameSite=Strict`**: see "Cross-site request forgery" below.

`POST /auth/logout` sends the same cookie with an empty value and `Max-Age=0`, which tells the
browser to delete it. Every other `Set-Cookie` is removed from every response.

The token inside is an HS256 JWT (PyJWT): claims `iss`, `aud` (both `TOKEN_ISSUER`), `iat`,
`exp`, `jti` (random; T46 keys its per-session limits on it), no personal data. The HMAC key is
the mounted `session-signing-key` secret (`SESSION_SIGNING_KEY_FILE`, version `latest`): at least
32 random bytes, used as they are (raw bytes, or a base64 or hex text of them: one trailing
newline is dropped when the rest is still 32 bytes or more). With 256 random bits nothing can be
guessed offline from a token, so D9's scrypt derivation is gone. The file is re-read after 30 s,
so **a new secret version ends every session** within that time. Verification accepts `HS256`
only, requires every claim, checks `iss`/`aud`, refuses a lifetime longer than configured, and
checks expiry against an injected clock (valid while `now < exp`). A missing cookie, an expired
or altered token and one signed with another key all answer `401 session-required` with
`WWW-Authenticate: Cookie realm="pulsebridge", cookie-name="__session"` (RFC 9110 section
15.5.2: a 401 must carry a challenge; no registered scheme describes a cookie session).

### Cross-site request forgery, and why both defences

**Cross-site request forgery (CSRF)**: a page on another site (say, `evil.example`) makes the
visitor's browser send a request to our proxy, for example a form that posts to
`/api/cuff/readings`. With an ordinary cookie the browser would attach our session cookie
automatically, and the proxy would accept a request the visitor never meant to make. The attacker
cannot read the answer, but it does not need to: the write (a cuff reading, a share to OpenEMR)
has already happened. Two independent defences:

1. **`SameSite=Strict`** on the cookie: the browser attaches it only to requests that the page's
   own site starts. A request started from another site, by a link, a form or a script, goes
   without it and gets `401`.
2. **The `Origin` check**: every `POST` must carry `Origin` equal to one of `ALLOWED_ORIGINS`.
   Browsers set `Origin` themselves on every `POST` (Fetch standard), and page script cannot
   change it, so a request from another site says so.

Why both: `SameSite` is a browser feature, and its "site" is the registrable domain, not the
exact origin (a sibling subdomain counts as the same site, older browsers ignore the
attribute, and its rules have changed over the years). The `Origin` check is the server's own,
exact, and it also protects `POST /auth/session`, which runs before any cookie exists (a
foreign page could otherwise spend our assessments). Neither alone is something to rely on for
writes; together a forged request needs both a browser bug and a lie in `Origin`. A `GET`
needs no `Origin`: every `GET` is read-only (D12), and a cross-site page cannot read the answer
because no CORS header is ever sent.

**No CORS** (cross-origin resource sharing, the headers that let a page on another origin read
an answer): the page and the proxy share one origin through Hosting's `/api` rewrite, so the
proxy sends no `Access-Control-*` header and `OPTIONS` is not answered as a preflight. Local
development uses the page's mock client or Vite's dev-server proxy (T45).

### The client address

`client_address.py` decides which `X-Forwarded-For` entry is the client, for the per-address
limit and the logs (unchanged from D9). `X-Forwarded-For` is appended to by each proxy, so only
right-hand entries are trustworthy. Google documents the load balancer's behaviour ("appends two
IP addresses ... `EXISTING_VALUE, CLIENT_IP, LOAD_BALANCER_IP`" and "does not verify any IP
addresses that precede", docs.cloud.google.com/load-balancing/docs/https, "X-Forwarded-For
header", read 2026-11-15). Cloud Run's own documentation says nothing about the header. Measured
on dev on 17 November 2026 (infra/envs/README.md, P1): `*.run.app` appends one entry, the
address it saw, after whatever the caller sent. So the default `TRUSTED_PROXY_HOPS=1` takes the
**last** entry (the service alone at `*.run.app`); `2` takes the second from the right (Firebase
Hosting in front, measured, which module phr sets); `0` ignores the header (local use). With
fewer entries than hops the left-most is used; an entry that is not an address goes to one
shared bucket. Under 2, a request sent straight to `*.run.app` with its own header chooses its
key (module phr's README, "The client address and the direct run.app URL"): such a caller is
not held by the per-address limit, and can fill another visitor's bucket if it knows the
address. What bounds it then is the daily cap.

### Logs

`session-created` (`score`, `checks_today`), `bot-check-failed` (`reason`, `invalid_reason`,
`score`, `checks_today`; `reason=malformed-token` for the shape check), `bot-check-unavailable`
and `demo-counter-unavailable` (`problem`: a fixed code such as `timeout`, `http-403`,
`bad-object`, `contention`), `demo-busy` (`cap`), `session-rate-limited` (`client_address`,
`retry_after`) and `origin-refused` (`client_address`, `origin_present`), both sampled under a
flood (the first per address per minute, then one `session-events-suppressed` count),
`session-signing-key-unavailable` (`problem`), `token-refused` (`reason`). Never a token, the
key or the refused `Origin` value.

What it is not: sign-in. Every visitor is the same anonymous "demo"; nothing records who viewed
what. A real multi-patient deployment would use SMART on FHIR or an identity provider and an
audit log. The CapabilityStatement says so and claims no `restful-security-service` code.

## Response headers

On every response: `Cache-Control: no-store` (also keeps Firebase Hosting's CDN from caching
health data), `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none';
frame-ancestors 'none'`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, HSTS,
`Vary: Origin, Cookie`. No `Access-Control-*` header. No `Set-Cookie` except the session's. No
server version (gunicorn's `Server` header carries none; Cloud Run's front end adds its own).

## Robustness

- Request bodies are capped at 16 KiB (cuff-sim's own limit; `413` in cuff-sim's shape),
  session bodies at 10 KiB, by the proxy itself (`bodies.read_body`): it reads at most limit + 1 bytes
  and refuses when there are more, and refuses a body shorter than its `Content-Length`.
  Werkzeug's `MAX_CONTENT_LENGTH` is not used, because under gunicorn a chunked body (no
  `Content-Length`, `wsgi.input_terminated`) is silently cut at the limit and would have been
  forwarded truncated. Tested with gunicorn's environ at 16384, 16385 and 40000 bytes and against
  a real gunicorn on 127.0.0.1 with `Transfer-Encoding: chunked`. Session JSON is strict:
  UTF-8, no `NaN`, no repeated keys, exactly one field.
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
- Every upstream call has a deadline, inside the request's overall budget ("Time budget" above):
  `FHIR_TIMEOUT_SECONDS` per Cloud Healthcare request (no retries), the cuff and health-sync
  timeouts, 10 s for minting an identity token, each capped by the time left. An answer over
  256 KiB from cuff-sim or health-sync is refused. Redirects are never followed.
- The search post-filters every entry to the configured patient even though the store query
  is already pinned (`subject`), so a store that ignored the parameter would still not leak
  another patient's data (tested with a fake that ignores it).
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
- **Order of the log**: newest first by **send time**. Attempt rows sort by `sentAt` (T31's
  `period.start`), store-only rows by the message's `createTime` (a sample's MSH-7 is a fixed
  date years earlier, so `sendTime` would misplace it), transaction rows by `recordedAt`; ties
  break on the row id, descending. The audit store can only be searched by `recorded`, which
  is never before the send time and at most `SEND_RECORD_SKEW` (10 minutes; cuff-sim's worst
  case is about 80 s) after it, so the search reaches that far above the cursor, and a row is
  shown only once no unread attempt can sort before it.
- **Paging the log**: the cursor is a send-time instant plus the ids already shown at that
  instant (base64url JSON, checked strictly). Each source is asked for its newest items at or
  before it; only items no source could still be missing are shown (a source that filled its
  page may have more just below what it returned).
- **Transaction rows** are pinned: the Provenance search includes its targets
  (`_include=Provenance:target`), and only Provenances whose targets are the configured
  patient's Observations make a row.
- **A wrong derived-name rule.** An accepted MLLP attempt whose derived name is not in the
  store stays `messageLookup: "not-found"` with no outcome and no Observation: no outcome label
  is ever moved from one record to another. Its real message, if stored, is a store-only row,
  and carries `possibleDuplicateOf: <attempt id>` only when exactly one attempt qualifies (same
  control ID, its own derived name `not-found`, sent at most 120 s before the message was
  created). The candidates come from the same audit search that decides "store-only", which
  reaches 130 s before the oldest message, so a page boundary cannot hide the attempt; only
  messages no attempt anywhere claims are considered. The trace route shows the same lookup.
- **Cost of one page** (50 rows): one attempt search (more pages only while their order is not
  settled) and one Provenance search, in parallel; then up to 50 message `get`s, in parallel,
  alongside the store listing (up to 3 pages); one audit search for all store-only candidates;
  one Observation identifier search. About five rounds in a row, all inside the budget.

## Share to OpenEMR (T15)

`POST <prefix>/share` sends ONE final reading from the health-data store to OpenEMR (the
"clinic") through OpenEMR's FHIR R4 API, and answers what happened step by step. The **contract
is the dashboard's**: web/README.md, "For T15: the share route" (with `src/api/contract.ts` and
`parse.ts`); this section says how it is built and where it differs.

Code: `fhir_proxy/share/` (`service.py` the route and the sequence, `oauth.py` the token,
`document.py` the client document and the pinned TLS context, `transport.py` one HTTPS call,
`resources.py` what is sent, `probe.py` the status strip, `testing.py` the fake OpenEMR). It
plugs in through `fhir_proxy.extensions` (below, "How `share/` plugs in"): a token-gated `view`
route, listed in the CapabilityStatement's `rest.documentation`, not as a FHIR interaction
(it is not one: it is a PulseBridge operation that makes several FHIR calls elsewhere).

### Terms

- **OpenEMR**: an open-source medical record system with a FHIR R4 API, running on the edge VM
  (module openemr). It plays the clinic.
- **Encounter**: a FHIR resource for a visit. OpenEMR files vital signs as a form on a visit and
  refuses an Observation without one (spike S1), so the share creates or reuses one.
- **Client credentials grant** and **`private_key_jwt`**: OAuth 2.0 for one server talking to
  another. The proxy signs a short JWT (the *client assertion*) with a private key; OpenEMR checks
  it against the registered public key and issues a 300-second *access token*. No password, no
  user. See `share/oauth.py`.
- **Conditional create**: a FHIR create that only happens if a search finds nothing
  (`If-None-Exist`). OpenEMR ignores it (S1), so the proxy searches first.
- **Pinning**: trusting exactly one certificate (OpenEMR's self-signed one) instead of the public
  authorities.

### The request and the refusals (before OpenEMR is called)

`{"observationId": "<FHIR id>"}`, exactly that, strict JSON, at most 512 bytes, `Content-Type:
application/json`. Every refusal is an OperationOutcome from `Problem`, and **no request has
reached OpenEMR** (tests assert the fake received nothing):

| Status | Reason | When |
| --- | --- | --- |
| 503 | `share-not-configured` | no `OPENEMR_*` settings; or the client document is missing, unreadable, not JSON, of another `format`, lacks or misshapes a field, has a key that is not RSA ≥ 2048 bits or a certificate that does not load, names another address than `OPENEMR_SITE_ADDRESS`, or lists other scopes than `OPENEMR_SCOPES` (one fixed sentence each; nothing from the document is quoted) |
| 400 | `invalid-body` | anything but exactly `{"observationId": <SAFE_ID>}`, including a body over 512 bytes (not 413: the page reads only 400/404/422/503 as "nothing was sent") and a non-JSON content type |
| 404 | `not-found` | no such Observation, or another patient's (answered alike) |
| 422 | `observation-not-final` | `status` is not `final` (`amended`, `preliminary`, `corrected`, `entered-in-error`, ...); an unknown value is not echoed |
| 422 | `observation-not-shareable` | not a heart rate (LOINC 8867-4 with UCUM `/min`) or a blood-pressure panel (85354-9 with 8480-6 and 8462-4 in `mm[Hg]`), or no time with a zone |
| 422 | `patient-incomplete` | `patient_for_sharing` refuses the stored Patient; `diagnostics` names what is missing (T06's fixed phrases) |
| 503 | `share-busy` | **new**: another share holds this instance's lock and did not finish in time, or the budget ran out before the first OpenEMR call; `Retry-After: 5`; nothing was sent |

A Cloud Healthcare failure while reading the Observation or Patient is T14's bounded
`upstream-*` answer (502/503/504 without steps). Nothing was sent then either, but the page reads
it as "not known what reached the clinic": cautious, not wrong.

### The sequence, and exactly what is sent

1. **`token`** `POST /oauth2/default/token`, form-encoded: `grant_type=client_credentials`,
   `scope=<the document's scope>`, `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`,
   `client_assertion=<RS384 JWT>` (header `kid` from the document; claims `iss` = `sub` =
   `client_id`, `aud` = `OPENEMR_TOKEN_URL`, `iat`, `exp` = `iat` + 60, a random 128-bit `jti`).
   A token still valid in memory is reused instead: the step's `result` is then **`reused`**,
   with `status` 0, no bodies and a label saying no token request was sent.
2. **`patient-search`** `GET /apis/default/fhir/Patient?identifier=<PT value>`. A found Patient
   must carry that value (otherwise the share stops rather than risk a duplicate); several
   found (a past race): the first by id, said in the label.
3. **`patient-create`** only after `not-found`: `POST /apis/default/fhir/Patient` with
   `patient_for_sharing(stored)`: the `PT` identifier, the official name, gender, birth date.
   Never the Google Health user id, our `id` or `meta`.
4. **`encounter-search`** `GET /apis/default/fhir/Encounter?patient=<OpenEMR's id>&date=<YYYY-MM-DD>`,
   the UTC day of the time sent (`effectiveDateTime`; a heart-rate window's start). Only an
   Encounter whose `subject.reference` is exactly `Patient/<OpenEMR's id>` is reused (another
   patient's, or one with no subject, never is); several: the oldest `period.start`, said in
   the label.
5. **`encounter-create`** only after `not-found`:
   `{"resourceType": "Encounter", "status": "finished", "class": {v3-ActCode AMB}, "subject": {"reference": "Patient/<OpenEMR's id>"}, "period": {"start": "<day>T00:00:00Z", "end": "<day>T23:59:59Z"}}`.
6. **`observation-create`** `POST /apis/default/fhir/Observation`, built from **allow-lists at
   every depth** (`resources.py`): `status`; `category`, `code` and `method` as
   CodeableConcepts (`coding`, `text`), each Coding only `system`/`code`/`display` and only from
   the system the builders use (observation-category, LOINC; `method` text only);
   `valueQuantity` and each `component`'s `code` and `valueQuantity`, Quantities only
   `value`/`unit`/`system` (UCUM)/`code`; one `effectiveDateTime`; then `subject` and
   `encounter` set to OpenEMR's references. Nothing else at any depth: no `id`, `meta`,
   `identifier`, `device`, `performer`, `extension`, `_`-prefixed keys, notes or references,
   and no empty object. Before the lock is taken, a guard checks what is about to be sent for
   our internal ids, the Google Health id and any `urn:pulsebridge:` text (allowed only as the
   Patient's PT identifier system); if one is there the share answers **500 `share-blocked`**
   (an OperationOutcome, no steps: nothing was sent).
   **A heart-rate window is sent at one time**: OpenEMR 8.5.0's vitals write
   (`FhirObservationVitalsService`) reads only `effectiveDateTime`, refuses an Observation
   without it, and never reads `effectivePeriod`. So `effectiveDateTime` is the window's start
   with its offset, and no `effectivePeriod` is sent. OpenEMR files one pulse per time and skips
   component codes it has no column for, so it keeps the average, not the minimum and maximum
   (the min and max components are still sent; it ignores them). From reading the 8.5.0 source;
   not verified live.
   **Success is only a 201 whose JSON object names a valid id** (the same for the Patient and
   the Encounter). Any other 2xx (no id, a hostile id such as `..` or `a/b`, HTML, a 200) is
   "answered but not understood": the step is `failed` with OpenEMR's status, and the share
   stops with the envelope, because whether it was created is not known.
   **`already-shared`** only for OpenEMR 8.5.0's own refusal with the code that was sent:
   `A "<code>" reading already exists for this encounter at this effectiveDateTime
   (Observation/<uuid>); POST does not overwrite ...` (the id only when OpenEMR could map it).
   Exactly one id named: it is read back; none, or several different ones: the step says so
   and nothing is read back (no guess). Any other 400 is a refusal.
7. **`patient-read`**, **`encounter-read`**, **`observation-read`** by id (D13). A failure is a
   `failed` step and `stored: null`; the outcome stays `shared`/`already-shared`. A read-back
   that answers another resource or id is not shown, and the Observation read back must have
   `subject` `Patient/<our OpenEMR Patient>`, our `encounter` and the code that was sent
   (otherwise "not this reading", `stored: null`: an id OpenEMR named is never trusted blindly). OpenEMR's own address is removed from
   everything passed on (`fullUrl`, `link`), so the page never sees a host.

Literal examples of what is sent for each kind are in `tests/share/test_share_flow.py`
(`BP_SENT`, `HR_SENT`, `PATIENT_SENT`, `ENCOUNTER_SENT`), asserted request by request.

**200** `{"outcome", "steps", "readBack": {patient, encounter, observation: {sent, stored}}}`,
`Content-Type: application/json`. `sent` is null when nothing was sent (Patient found, Encounter
reused). `observation.sent` is set for `already-shared` too: it was sent, and refused as a repeat.

### Failures after OpenEMR was called

The 502/504 envelope `{"operationOutcome": <Problem's OperationOutcome>, "steps": [...]}`,
`Content-Type: application/json`, never a bare 5xx:

| Where | Status, reason | What the page is told |
| --- | --- | --- |
| no connection, TLS refused (wrong certificate or IP), connection broken | 502 `openemr-unreachable` | the steps so far, the failing one with status 0 and the cause in its label |
| no answer within the call's limit (ONE deadline for connect, handshake, send, status line and every read) | 504 `openemr-timeout` | the failing step with status 0; "not known whether that step took effect" |
| the request budget spent before the next call | 504 `openemr-timeout` | the steps that completed; the unsent step is NOT listed (nothing about it happened); the text names it |
| an unexpected status (4xx/5xx, a 3xx: never followed), a body that is not JSON (not passed on, a too-deep one included) or not the expected shape | 502 `openemr-refused` | the failing step with OpenEMR's status |
| an answer over 1 MiB | 502 `openemr-refused` | the failing step with **status 0** ("answer too large to read (HTTP n)"): it was not read, so nothing is claimed |
| a create answered 2xx but not as a 201 with a valid id | 502 `openemr-refused` | the create step `failed` with its status; "not known whether it was created" |
| token refused with `invalid_client` | the document is read again and the request sent once more; then 502 `openemr-refused` with both token steps | |
| a FHIR call answered 401 | 502 `openemr-refused`; the cached token is dropped | |
| **the proxy fails between sending a request and recording its step** (any exception) | 502 `share-internal-error` | that request IS listed: status 0, `failed`, "sent, but the proxy failed before it could read the answer; whether it took effect is not known" (so the page never says it was not sent) |
| a bug elsewhere after the first call | 502 `share-internal-error` | the steps so far; logged as `bug` by class name |
| the leak guard | 500 `share-blocked` | nothing was sent |
| a bug before the first call | T14's 500 `internal-error` | nothing was sent |

**Credentials echoed back.** Redaction by key name cannot catch OpenEMR quoting the assertion in
an OAuth `error_description`, or the access token in a FHIR error message. So every string of
every recorded step (labels and bodies, at any depth) is also scrubbed of this run's signed
assertions and access token (`Bearer <token>` included), as it is of OpenEMR's address.

### Security

- **The document, the assertion and the token** never appear in a log line, an answer or a
  trace. The trace's redaction is the backstop, not the plan: the token step's request body is
  the form with `client_assertion` replaced, its response with `access_token` replaced; no
  header is ever part of a step. `ClientDocument` and the cache keep keys and tokens out of
  `repr`. A test pushes generated key material, every assertion and every token through all
  paths (success, timeout, unreachable, a bug quoting a token, `invalid_client`, a bug quoting
  the key) and searches every log line and answer.
- **Trace redaction (changed from T14)**: in the `token` step's bodies every OAuth parameter
  (`extensions.TOKEN_STEP_SECRET_KEYS`, including `code` and `token`); in every other step only
  names that can never be FHIR elements (`FHIR_STEP_SECRET_KEYS`), so `Observation.code`,
  `Coding.code` and `Quantity.code` are visible. The lists equal the page's
  `SHARE_TOKEN_STEP_SECRETS` and `SHARE_FHIR_STEP_SECRETS`; `tests/share/test_redaction_and_wire.py`
  reads them from `contract.ts`. Search parameters go in `request.query` (an object), never in
  the url, and a credential name is refused there.
- **TLS**: a context that trusts **only** the document's `server_certificate_pem`
  (`ssl.create_default_context(cadata=...)`: no system authorities), hostname (IP) checking on,
  `CERT_REQUIRED`, TLS 1.2 or later, Python 3.13's strict X.509 checks. Verification is never
  switched off (a test greps the package). Tested over real TLS on 127.0.0.1: another
  certificate, and OpenEMR's certificate for another IP, are both refused before a request is
  sent.
- **Addresses** come only from configuration; the request carries nothing but an Observation
  id. No redirect is followed; answers are capped at 1 MiB; every call is bounded by
  `OPENEMR_TIMEOUT_SECONDS` and by the request budget (`deadline.capped`), and runs on the
  request thread (no executor).
- **Scopes**: module openemr's default scopes (`.read`/`.write`) also allow update and delete
  in OpenEMR. The proxy itself only searches, reads and creates (`POST`/`GET`, nothing else
  in the code). `OPENEMR_SCOPES` must cover search+read+create on Patient and Encounter and
  create+read on Observation, checked at start; the narrower SMART v2 set
  `system/Patient.crs system/Encounter.crs system/Observation.cr` is accepted.

### The token cache, the lock, the document, the probe

- **Token cache**: in memory, per (client ID, token URL, scope), thread-safe, reused until 60 s
  before it expires (OpenEMR's 300 s tokens: 240 s), so a burst of shares asks once. A new
  registration (new client ID) never uses an old token. Dropped on `invalid_client` and on a
  401 from a FHIR call. Per instance: two instances each fetch their own.
- **Lock**: one share at a time per instance. A second share **waits** for the first, up to
  20 s and only while 20 s of its own budget would remain; otherwise it answers 503
  `share-busy` with nothing sent. Chosen over an immediate 409: two clicks in a row (or two
  tabs) both succeed, the second finding what the first created; a 409 would read on the page
  as "not known what reached the clinic". The lock is released on every path (tested).
- **Concurrency across instances**: Cloud Run may run 2 instances (`max_instance_count`), and
  the lock does not span them. Two **first** shares at the same moment on two instances can
  both find no Patient and both create one (S1: search-then-create is not atomic; concurrent
  creates were not verified), or both create that day's Encounter. The consequence is a
  duplicate Patient or Encounter at the clinic; later shares then reuse the first by id / the
  oldest and say "duplicate" in the label. The readings themselves are refused as repeats only
  within one Encounter, so a duplicate Encounter could also hold a duplicate reading. Accepted
  for a one-patient demo; the fix would be a lock outside the instances.
- **The client document** is read on every share and every probe, never cached: the mounted
  secret may not have a version yet, and a re-registration replaces key, client ID and
  certificate together. On `invalid_client` it is read once more.
- **The probe** (`/status` `openemr`): `GET /meta/health/readyz` over the pinned TLS, no token,
  at most 5 s and within `/status`'s budget, run in parallel with the other parts, cached 30 s.
  `detail`: `null` (reachable), `not-configured`, `client-not-registered`,
  `client-document-invalid`, `tls-verification-failed`, `timeout`, `unavailable`, `not-ready`,
  `error`. Never a 5xx.

### Differences from the page's contract (for T19 / the orchestrator)

| What | Contract or mock | As built | Why |
| --- | --- | --- | --- |
| Busy | not listed | 503 `share-busy`, OperationOutcome, no steps, `Retry-After` | now in the contract (`ShareFailureReason`); the page says "another share was in progress on the server; try again in a few seconds" and "Nothing was sent" |
| A bug after OpenEMR was called | 502 `openemr-unreachable`/`openemr-refused` | 502 `share-internal-error` in the envelope | "refused" would blame OpenEMR; now in the contract |
| A cached token | the mock always showed a 200 token step | `token` step **`result: "reused"`**, status 0, no bodies | now in the contract; the page says "Token reused from the server's memory: no request sent" with no "→ no answer" line; the mock reuses its token too |
| A step never sent (budget spent) | "the failing one with status 0" | not listed; the 504's text names it | the page then says "the reading was not sent", which is true |
| A heart-rate window | `effectivePeriod` sent | `effectiveDateTime` = the window's start | OpenEMR 8.5.0 never reads `effectivePeriod`; the mock and the page's preview follow |
| Leak guard | not listed | 500 `share-blocked`, no steps | a server fault before anything was sent |
| Probe `detail` | `not-checked` before T15 | the codes above | the strip words each one (neutral "Not configured", "Not registered yet"; warning "Misconfigured", "Certificate mismatch", "Starting or not ready", "Unreachable") |

### Configuration (for T22 / T25)

All five or none (`config.OPENEMR_GROUP`), checked at start (errors name variables only). The
names are the ones module phr already sets; nothing in `infra/` changed.

| Variable | Value |
| --- | --- |
| `OPENEMR_SITE_ADDRESS` | module openemr's `site_address`: `https://<private IP>[:port]`, no path (the VM's internal address, D14) |
| `OPENEMR_TOKEN_URL` | `token_url`: exactly the site address + `/oauth2/default/token` |
| `OPENEMR_FHIR_BASE_URL` | `fhir_base_url`: exactly the site address + `/apis/default/fhir` |
| `OPENEMR_SCOPES` | `scopes`; must equal the document's `scope` as a set |
| `OPENEMR_CLIENT_FILE` | `/secrets/openemr-client/value`: module openemr's `client_secret_id`, mounted as a file at version `latest` |
| `OPENEMR_TIMEOUT_SECONDS` | optional, default 10 (1-30), per OpenEMR call |

T22/T25 pass module phr's `openemr = { ... }` (its README, "For T15"); fhir-proxy's account
already has `secretAccessor` on the secret, and Direct VPC egress reaches the VM.

### Verify against a live OpenEMR (T25), in order

1. **Token**: share once; the `token` step is `ok` with `expires_in: 300`; a second share within
   4 minutes shows `reused`. If it is `invalid_client`, compare the document's
   `site_addr_oath` with OpenEMR's `site_addr_oath` setting. Also check **whether OpenEMR's OAuth
   errors echo the assertion** (a deliberately wrong `aud` once): the trace must still show
   `[not shown]`.
2. **A heart rate is accepted, and what is stored**: share a heart-rate window; it must be a
   201 at the window's start (`effectiveDateTime`), and the read-back shows what OpenEMR keeps
   (expected from the source: the pulse only, no min/max components).
3. **The duplicate text**: share the same blood pressure and the same heart rate twice; each
   repeat must be OpenEMR's `A "<code>" reading already exists ... (Observation/<uuid>)` 400,
   giving `already-shared` with no second Patient, Encounter or Observation.
4. **Encounter date search with a non-UTC server zone**: with OpenEMR's time zone not UTC,
   share a reading taken late in the evening west of UTC; check that `Encounter?date=<UTC day>`
   finds the Encounter created for that day (OpenEMR stores the encounter date in its own zone).
5. **The 201 bodies**: confirm the create answers are `{"pid", "uuid"}` (Patient),
   `{"euuid", "encounter", "pid"}` (Encounter) and `{"uuid"}` (Observation), each a 201.
6. **`/meta/health/readyz` without a token**: `/status` shows OpenEMR reachable.
7. **Concurrent first shares**: from two tabs at once on a fresh OpenEMR (one instance
   serialises them; with two instances up, see "Concurrency").
8. **Re-registration**: delete the TLS directory on the VM and re-run the startup script; the
   next share must work without a redeploy (new document via `latest`; one `invalid_client` and
   a re-read at most).
9. **Narrower scopes**: `system/Patient.crs system/Encounter.crs system/Observation.cr` with
   `client_credentials`.

**Results of the first dev deploy** (16 November 2026, OpenEMR 8.5.0 on the edge VM, synthetic
patient). Settled: **2** (the heart rate was a 201 at the window's start, and OpenEMR kept the
average only, no components), **3** (a repeat of each, blood pressure and heart rate, answered
the predicted duplicate text naming the Observation id and gave `already-shared` with the same
Observation id), **5** (the 201 bodies carry the
`uuid` the proxy reads), **6** (`/status` reached readyz without a token). Partly: **1** (the
token request worked and a repeat share within minutes reused the token; `expires_in` and the
error echo were not checked), **4** (`Encounter?date=<UTC day>` found the day's Encounter;
whether OpenEMR's zone was other than UTC was not checked). Not done: 7, 8, 9.

### How `share/` plugs in (the extension seam)

- Create the package **`services/fhir-proxy/fhir_proxy/share/`** (a subpackage, so it is
  installed with the service), with its tests under **`services/fhir-proxy/tests/share/`**
  (the orchestrator is amending T15's Owns to these two paths).
- Its `__init__.py` defines `register(proxy: ProxyContext) -> None`. `create_app` imports it if
  present and calls it once at start-up (`extensions.load_extension`). In `register`:
  - `proxy.add_route(Route("POST", "/share", "share", "view", "<summary>"), handler)`: the route
    joins the same table, so it gets the cookie and `Origin` checks, the headers, the request budget,
    error handling and a line in the CapabilityStatement. Refused: a public route (the public
    set comes from the core table only), a core endpoint name, or a method and path that exist.
  - Register only token-gated routes with **distinct literal paths** (no converters that could
    overlap a core URL; such a rule is refused, and the app will not start if one slips by).
  - Read the request body with `fhir_proxy.bodies.read_body(limit)`, never
    `request.get_data()` (see "Robustness").
  - The share's OpenEMR calls run inside the request budget (50 s): use
    `fhir_proxy.deadline.capped(timeout)` for each, and bound `openemr_probe` the same way
    (it runs inside `/status`'s budget).
  - Never submit to the shared executor from inside a task running on it (a nested submission
    can wait on itself); run independent calls with one flat `fhir_proxy.deadline.run_parallel`
    from the request thread.
  - `proxy.set_openemr_probe(fn)`: `fn()` returns `{"reachable", "checkedAt", "detail"}` for the
    status strip.
  - `proxy.stores`: `patient_references()`, `read(type, id)`, `is_ours(resource, refs)`: read
    the Observations to share with the patient pin.
  - `proxy.config`: the proxy's configuration; read T15's own variables (OpenEMR URL, audience,
    CA, key file) in its own config module and fail at `register` time, naming variables only.
- Refuse with `raise Problem(422, "required", "<reason>", "<fixed sentence>")` before calling
  OpenEMR, `Problem(502, ...)` when it is unreachable (`fhir_proxy.errors`).
- Build each trace step with `extensions.trace_step(...)`: the dashboard's `ShareStep` shape,
  redacted by the final rule (the token step's OAuth list; elsewhere only names that are never
  FHIR elements), `url` required to be a path, search parameters in `query`.
- `proxy.clock`, `proxy.monotonic`: the app's clocks; `proxy.options`: what
  `create_app(extension_options=...)` passed (tests inject the share's fake OpenEMR transport).
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
| `SESSION_SIGNING_KEY_FILE` | yes | | absolute path of the mounted `<prefix>-session-signing-key` secret file (at least 32 random bytes) |
| `ALLOWED_ORIGINS` | yes | | comma-separated exact origins of the page (`https://<site>.web.app`, ...; `http://localhost:<port>` for development): every `POST`'s `Origin` must be one, and the bot check's hostname must be one of their hosts |
| `RECAPTCHA_SITE_KEY` | yes | | the score-based reCAPTCHA key's ID (public; `GET /auth/config` returns it) |
| `DEMO_COUNTER_BUCKET` | yes | | the counter bucket's name (`<prefix>-demo-counters`) |
| `RECAPTCHA_ACTION` | no | `view_demo` | the action name; `[A-Za-z0-9/_]`, 1-100 |
| `RECAPTCHA_MIN_SCORE` | no | `0.5` | lowest passing score, 0-1, inclusive |
| `SESSIONS_PER_ADDRESS_PER_HOUR` | no | `5` | 1-100; every attempt counts |
| `BOT_CHECKS_PER_DAY` | no | `300` | assessments per UTC day across all instances, 1-100000 |
| `CUFF_SIM_URL` | yes | | cuff-sim's Cloud Run URL (`https://...run.app`); also the identity token's audience |
| `PUBLIC_BASE_URL` | yes | | the URL the page reaches the proxy at, for `Bundle.entry.fullUrl`: behind the Firebase Hosting rewrite `https://<hosting domain>/api` (its path must equal `PATH_PREFIX`). Never taken from `Host` or forwarded headers |
| `HEALTH_SYNC_URL` | no | empty | health-sync's Cloud Run URL; empty: `state: unknown`, `health-sync-not-deployed` |
| `PATIENT_ASSIGNING_AUTHORITY`, `PATIENT_FAMILY_NAME`, `PATIENT_GIVEN_NAME` | no | empty | the same values as cuff-sim's; all or none. With them the trace shows a sent sample's exact bytes |
| `PATH_PREFIX` | no | empty | `/api` when the page reaches the proxy through the Firebase Hosting rewrite (Hosting forwards the full path) |
| `SESSION_TTL_SECONDS` | no | `86400` | session lifetime (token `exp` and cookie `Max-Age`), 60-86400 |
| `TOKEN_ISSUER` | no | `pulsebridge-fhir-proxy` | JWT `iss`/`aud` |
| `TRUSTED_PROXY_HOPS` | no | `1` | `1` for the service alone at `*.run.app`; **`2` behind Firebase Hosting** (measured; module phr sets it). See below |
| `FHIR_TIMEOUT_SECONDS` | no | `10` | per Cloud Healthcare request, 1-30 |
| `CUFF_FORWARD_TIMEOUT_SECONDS` | no | `55` | 10-3000, at least 10 s below `REQUEST_TIMEOUT_SECONDS`; the budget of the cuff send (below) |
| `REQUEST_BUDGET_SECONDS` | no | `50` | 5-3000, at least 10 s below `REQUEST_TIMEOUT_SECONDS`; the budget of every other request |
| `SAMPLES_FORWARD_TIMEOUT_SECONDS` | no | `15` | |
| `HEALTH_SYNC_TIMEOUT_SECONDS` | no | `25` | health-sync budgets 20 s for `/status` |
| `REQUEST_TIMEOUT_SECONDS` | no | `120` | **set the Cloud Run request timeout to the same number** |
| `STATUS_CACHE_SECONDS` | no | `60` | reuse of the store-derived status |
| `OPENEMR_SITE_ADDRESS`, `OPENEMR_TOKEN_URL`, `OPENEMR_FHIR_BASE_URL`, `OPENEMR_SCOPES`, `OPENEMR_CLIENT_FILE` | no (all or none) | empty | the share to OpenEMR (T15); see "Share to OpenEMR (T15)", "Configuration" |
| `OPENEMR_TIMEOUT_SECONDS` | no | `10` | per OpenEMR call, 1-30 |

Removed by T43 (D16): `REVIEWER_PASSWORD_FILE`, `PASSWORD_CACHE_SECONDS`,
`LOGIN_FAILURES_PER_ADDRESS`, `LOGIN_FAILURES_TOTAL`, `LOGIN_WINDOW_SECONDS`.

**Secret mount.** Mount `<prefix>-session-signing-key` as a **file volume** with version
**`latest`** (not an environment variable, which is read once at start), and point
`SESSION_SIGNING_KEY_FILE` at the file. Only the proxy's service account has `secretAccessor`
on it (`infra/modules/identity`). A volume secret needs a version at deploy time: add it with
T44's script before deploying a proxy that mounts it.

**Google APIs the proxy calls for sessions** (T44): `recaptchaenterprise.googleapis.com`
enabled; `roles/recaptchaenterprise.agent` for the proxy's service account (in
`GCP_PROJECT_ID`, where the assessment is created and where the key must live); on the counter
bucket only, a role with `storage.objects.get`, `storage.objects.create` and
`storage.objects.delete` (overwriting an object needs delete as well as create;
`roles/storage.objectUser` has all three, `roles/storage.objectCreator` alone does not). The
proxy uses the same Application Default Credentials (scope `cloud-platform`) as for Cloud
Healthcare.

**Invoker bindings the proxy needs**: `roles/run.invoker` for the `fhir-proxy` service account
on **cuff-sim** (D10, already in the identity README) **and on health-sync** (its README, "For
T20/T22"). The proxy itself is public: Cloud Run invoker IAM check disabled
(`invoker_iam_disabled = true`); no `allUsers` binding. The session token is the gate.

**One front door.** The page reaches the proxy one way only: through the Firebase Hosting
rewrite (`PATH_PREFIX=/api`, `PUBLIC_BASE_URL=https://<hosting domain>/api`). It must not call
`*.run.app` for some routes and Hosting for others: the two put different entries at the right
of `X-Forwarded-For`, and no single `TRUSTED_PROXY_HOPS` fits both.

**`TRUSTED_PROXY_HOPS`: 2 behind Firebase Hosting (measured), 1 without it.** Measured on the
dev deploy on 17 November 2026 (infra/envs/README.md, checklist P1): through the rewrite the
service receives two entries, `<visitor>, <Hosting's address>`, also when the visitor sends its
own `X-Forwarded-For` (Hosting replaces it), so the visitor is second from the right. With 1 the
key was Hosting's address, which changed between requests and was shared by visitors. Module phr
therefore sets 2 (its default). This service's own default stays **1**: called at `*.run.app`
with nothing in front, it receives exactly one entry, and a default of 2 would let any direct
caller write its own key. While `*.run.app` stays reachable directly (Hosting needs it), the
hop count of 2 lets a direct caller that sends its own header choose its key (the left of two
entries), so the per-address session limit does not hold it; the daily cap does (see "Demo
sessions" and module phr's README, "The client address and the direct run.app URL"). On a new
deployment, confirm the shape with session requests through the hosting domain (one with a
made-up `X-Forwarded-For: 192.0.2.1`) and read `client_address` in the `session-rate-limited`
lines (set `SESSIONS_PER_ADDRESS_PER_HOUR=1` for the test).

**Possible follow-up: `TRUSTED_PROXY_CIDRS`.** A hop count cannot tell a Hosting-forwarded request
from a direct `*.run.app` one. A setting listing Firebase Hosting's egress address ranges would:
trust the second-from-right `X-Forwarded-For` entry only when the right-most entry, which Cloud
Run itself appends, lies inside those ranges, and otherwise key on the right-most entry. A direct
caller could then not choose its own key. Not built; it needs ranges that hold Hosting's addresses and
not addresses other Google Cloud customers can send from (none is documented; the first
measurement saw only a Google address that changed between requests) and a way to keep them
current.

**Timeouts under Firebase Hosting's 60 s.** Hosting ends a rewritten request at 60 s
(documented). Everything is sized to answer before that:

- The proxy's own budget: `REQUEST_BUDGET_SECONDS=50` for every route but the cuff send,
  `CUFF_FORWARD_TIMEOUT_SECONDS=55` for the cuff send. Set the Cloud Run request timeout to
  `REQUEST_TIMEOUT_SECONDS` (default 120; module `phr` sets 70) as the hard stop above both.
- **cuff-sim must fit in 55 s.** Its worst case (services/cuff-sim/README.md, "Time limits";
  T = its `HTTPS_TIMEOUT_SECONDS`, each call up to 2 × T, doubled by a token refresh and 401
  retry): MLLP `MLLP_TIMEOUT_SECONDS + 4 × T`, HTTPS `8 × T`. With its defaults (5 s, T = 10)
  that is 45 s and 80 s: HTTPS does not fit. **Set cuff-sim's `HTTPS_TIMEOUT_SECONDS=6`**: MLLP
  5 + 24 = 29 s, HTTPS 48 s, both under 55. Also set cuff-sim's own Cloud Run request timeout
  to 55 s or a little more (its README says 90 s for the old defaults).
- Not behind Hosting (calling `*.run.app` directly, for example in a dev setup without the
  rewrite), the limits can be raised together: `CUFF_FORWARD_TIMEOUT_SECONDS` above cuff-sim's
  worst case (95 for its defaults), `REQUEST_BUDGET_SECONDS` as wanted, and
  `REQUEST_TIMEOUT_SECONDS` at least 10 s above both.

**Instances and concurrency.** `max_instance_count = 2` (s6). The per-address session limit is
in memory per process and the image runs one worker, so the real limit is up to 5 × instances
an hour (the daily cap is in the bucket and holds across instances). Do not raise `--workers`. Set Cloud Run's
**per-instance concurrency to the thread count (8)**: each request holds one gunicorn thread
for its whole life, and a slow unauthenticated client trickling a body holds one until the body
is complete or Cloud Run's request timeout (`REQUEST_TIMEOUT_SECONDS`: 120 s by default, 70 s as deployed by module `phr`) ends it. That is
what bounds it: at most 8 such requests per instance, for at most the request timeout each;
with a higher concurrency Cloud Run would queue requests inside a busy instance instead of
starting another one. (The body itself is capped at 16 KiB.)

**`PUBLIC_BASE_URL` is required**: the service does not start without it.

**cuff-sim's own request timeout** must be above the proxy's forward timeout (for example 60 s
against the proxy's 55 s), so a send the proxy has answered 504 for can still finish and record
its AuditEvent. services/cuff-sim/README.md, "Time limits", says so (wording updated by T14).

**Egress: Direct VPC egress with `ALL_TRAFFIC`.** health-sync and cuff-sim are internal-only
(`INGRESS_TRAFFIC_INTERNAL_ONLY`). A call from another Cloud Run service counts as internal only
when it goes through the VPC ("Requests from Cloud Run ... require additional configuration
before they are recognized as internal", docs.cloud.google.com/run/docs/securing/
private-networking, quoted in infra/modules/ingestion/README.md "For T22"). So the proxy uses
Direct VPC egress on project-base's `serverless` subnet with **`egress = "ALL_TRAFFIC"`** (not
`PRIVATE_RANGES_ONLY`): its calls to `*.run.app` and to Google APIs go through the VPC, and the
subnet's Private Google Access reaches them; no Cloud NAT is needed. T15's OpenEMR calls to the
edge VM use the same path (D14).

## For T24/T35: the image

The Dockerfile was added by T22, following services/README.md "Adding an image":

- `COPY` and install `libs/pulsebridge_fhir`, `libs/pulsebridge_hl7v2` (with its `samples/`
  package data: the cuff trace loads T07's samples) and `libs/pulsebridge_gcp`, then
  `services/fhir-proxy`.
- `CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "0", "--preload", "fhir_proxy.app:create_app()"]`
  (the "Running" command without `--bind`). **One worker**: the session limit is per process.
- `.dockerignore` allow-list: `services/fhir-proxy/pyproject.toml`, `README.md`, `fhir_proxy/`.
- Smoke case: `GET /status` with no cookie answers `401 session-required` without calling Google; start-up with
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
gunicorn --bind ":${PORT:-8080}" --workers 1 --threads 8 --timeout 0 --preload "fhir_proxy.app:create_app()"
```

One worker (the per-address session limit is per process), eight threads (the messages log looks up stored
messages in parallel), `--timeout 0` (Cloud Run's request timeout is the limit), `--preload`
(`create_app()`, which checks the configuration, runs in gunicorn's master before the port
opens, so a bad configuration exits without ever listening and Cloud Run's startup probe fails;
services/README.md, "Start-up failures"). The image (`Dockerfile`) runs this command without
`--bind` (it sets `PORT=8080`). Fork safety with `--preload`: `create_app()` starts no thread
(its `ThreadPoolExecutor`s start threads on first submit), holds no lock and opens no
connection; on Cloud Run, finding Application Default Credentials asks the metadata server
once, as for the other services. Credentials are
Application Default Credentials: on Cloud Run the `fhir-proxy` service account (FHIR reader on
both FHIR stores, HL7v2 consumer, secret accessor on `session-signing-key`, reCAPTCHA
Enterprise Agent, object read and write on the counter bucket); identity tokens for
cuff-sim and health-sync come from the metadata server. No key file.

## Logging

One JSON object per line on stdout. Events: the session events in "Demo sessions", "Logs"
(`session-created`, `bot-check-failed`, `bot-check-unavailable`, `demo-counter-unavailable`,
`demo-busy`, `session-rate-limited`, `origin-refused`, `session-events-suppressed`,
`session-signing-key-unavailable` with `problem` `missing`/`too-short`/`unreadable`),
`token-refused` (`reason`), `unreadable-send-attempt` (`attempt_id`), `attempts-skipped`
(`count`), `message-lookup-failed` (`error_type`), `store-only-scan-truncated`,
`store-only-check-truncated`, `messages-frontier-degenerate` (`rows`), `paging-link-unrecognised`, `health-sync-status` (`outcome`,
`http_status`), `fhir-store-status`, `cuff-forwarded` / `cuff-forward-failed` (`route`,
`http_status`, `reason`, `duration_ms`), `openemr-probe-failed` (`reason` or `error_type`),
`share-finished` (`outcome`, `steps`, `duration_ms`), `share-failed` (`reason`, `step`,
`http_status`, `steps`), `openemr-client-document-unusable` (`problem`, `field`: a field
name), `bug` (`error_type`,
`endpoint`). Never logged: the signing key, tokens of any kind, the patient identifier or name, observation
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
redirect, size cap). Fraud Defense and the counter bucket are fakes of their REST APIs
(`FakeFraudDefense`, `FakeBucket`) behind the proxy's real clients: the fake bucket keeps
generations and answers `412` to a failed precondition, so the counter race is tested for real
(an injected write between read and write; threads on two instances sharing one bucket). All data
is synthetic; the signing keys are obviously fake.

## Not verified

**T43 (demo sessions) made no call to Google.** Fraud Defense and Cloud Storage were tested
against fakes built from the documentation. To confirm on the first real deploy (the rollout),
in this order:

1. With the real key, a click on "View the live demo" through the hosting domain gives `200`
   and a `session-created` line with a score; `curl` with a made-up token gives `403` and a
   `bot-check-failed` line with `reason=invalid-token`, `invalid_reason=MALFORMED`. This shows
   the endpoint, the request body, the role and the answer's field names.
2. **Whether Fraud Defense answers a well-shaped junk token with `200` and `valid: false`** (as
   assumed) or with `400`: a `400` would show as `503 bot-check-unavailable` and
   `bot-check-unavailable problem=http-400` in the log. Then map that 400 to `bot-check-failed`.
3. **A real token's length and alphabet** fit the shape check `[A-Za-z0-9_-]{20,8192}` (a pass
   in step 1 shows it does; a `reason=malformed-token` line for a real click would show it
   does not).
4. **`tokenProperties.hostname`** is the bare host (`<site>.web.app`, no port), as compared.
5. **Firebase Hosting forwards `Origin`** on a `POST` to Cloud Run (step 1 passes only if it
   does), passes `__session` in both directions (`Set-Cookie` on the answer, `Cookie` on later
   requests), and does not cache any `/api` answer.
6. **The counter bucket**: the first check of the day creates `bot-checks/<day>`; a second
   check updates it (this needs `storage.objects.delete` as well as `create`); the object shows
   `metadata.count`. A `403` here shows as `demo-counter-unavailable problem=http-403`.
7. The assessment is billed to and counted in `GCP_PROJECT_ID`, and the key belongs to the same
   project (a key from another project would be refused).
8. Rotating the signing key (a new secret version) ends sessions within about 30 s.

T15 made no call to a real OpenEMR: the share was tested against `fhir_proxy.share.testing`'s
fake (S1's recorded behaviour, over an injected transport and over real TLS on 127.0.0.1). The
first dev deploy (16 November 2026) then settled part of the nine-point checklist in "Share to
OpenEMR (T15)" against a live OpenEMR 8.5.0 ("Results of the first dev deploy" there): a heart
rate sent at its window's start is accepted and only the average is kept; the duplicate text is
as predicted for blood pressure and heart rate; the Encounter date search works; the 201 bodies
carry `uuid`; readyz answers without a token. Still open: the Encounter date search with a
non-UTC server zone; whether OAuth errors echo the assertion; concurrent first shares;
re-registration; the narrower scopes. The fake's OpenEMR behaviour beyond spike S1
(`effectiveDateTime` required, `effectivePeriod` ignored, unknown components skipped, the exact
duplicate text, the Encounter date being `period.start`) comes from reading the 8.5.0 source
(`FhirObservationVitalsService`, `FhirEncounterService`), not from a running server.

T14 made no GCP call. Everything about Google's services is from documentation or other tasks'
notes, and must be confirmed on the first deployment (T25):

- **X-Forwarded-For on `*.run.app`** (one appended entry) and **behind Firebase Hosting**: not
  in Google's Cloud Run documentation. Measured on dev on 17 November 2026 (infra/envs/README.md,
  P1): a plain request to `*.run.app` arrives with 1 entry, and one with its own header arrives
  with that header plus one entry (Cloud Run appends); through Hosting it arrives with 2 whether
  or not the visitor sent its own header (Hosting replaces it). The direct measurement with a
  header ran behind an egress proxy, so its right-most entry was not the session's own address;
  the caller's entries were logged exactly as sent. See "For T22".
- Firebase Hosting's rewrite forwards the `Authorization` header (D9's login, no longer used
  since T43; confirmed on the first dev deploy: login, `/status` and the searches worked through `/api`); the full path and its 60 s
  timeout in practice are still open.
- Cloud Healthcare FHIR search: `gt`/`lt` on `effectivePeriod` (the proxy re-applies R4's rule,
  but cannot add windows the store left out); `_sort=date` on Observation; reusing a
  `_page_token` from a `next` link with the proxy's own parameter order; `_total=accurate&_count=0` (not `_summary=count`, which Cloud Healthcare refuses: found on the
  first deploy) giving `total`; `_sort=-_lastUpdated` with `_elements=id,meta` returning `meta.lastUpdated`;
  identifier OR lists; AuditEvent `type`/`subtype`/`date`/`_sort=-date` (T31 flagged these);
  Provenance `recorded` search and sort, and `_include=Provenance:target`. **Indexing delay**: Cloud Healthcare indexes
  non-identifier searches asynchronously (its search documentation), so a just-written reading
  may lag in the Observation search, the counts and `lastSyncAt`; identifier lookups do not lag.
- HL7v2 `messages.list` with a `create_time` filter (only accepted syntax was checked, spike S5)
  and with `view=RAW_ONLY`; `hl7V2Consumer` allowing list and get.
- The derived MLLP message name rule (spike S5, one message): the proxy confirms it with a
  `get` and reports `not-found` when wrong, so a wrong rule shows up rather than misleads; the
  `possibleDuplicateOf` hint is a heuristic pointer by design.
- That an AuditEvent's `recorded` is never more than `SEND_RECORD_SKEW` (10 minutes) after its
  send time: true for cuff-sim as written (it records once, after at most about 80 s); a row
  recorded later than that could be placed out of order or missed across a page boundary.
- Timings: the request budget and the parallel calls were tested with slow fakes, not against
  Cloud Healthcare's real latencies.
- That Cloud Run accepts an identity token whose audience is the service URL, and that the
  metadata server mints one for the proxy's service account: confirmed for cuff-sim on the first
  dev deploy (cuff sends through the proxy worked); health-sync (real data) not reached.
- That a mounted secret file reflects a new `latest` version on read without a restart (s6,
  from a search summary).
- Least-privilege roles: that `fhirResourceReader` allows every search above.
