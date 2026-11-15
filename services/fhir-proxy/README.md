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
| `GET /status` | always `200` once authenticated: `{"watchSync": {state, reason, notes, strandedPreliminary, checkedAt, lastSyncAt}, "observationCounts": {watch: number \| null, cuff: number \| null}, "fhirStore": {reachable, checkedAt, detail}, "openemr": {...}}` |
| `GET /fhir/metadata` | the CapabilityStatement (`application/fhir+json`) |
| `GET /fhir/Observation?code=&date=&_sort=&_count=` | `searchset` Bundle of the configured patient's Observations; each entry has `fullUrl` = `PUBLIC_BASE_URL/fhir/Observation/<id>`; `link` `self` and `next` are path-absolute links to the proxy; `400` OperationOutcome for anything unsupported |
| `GET /fhir/Observation/{id}` | the Observation, if it is the configured patient's (`404` otherwise): what `fullUrl` names |
| `GET /trace/heart-rate/{observationId}` | `{"resources": Bundle (collection: Observation, Provenance?, Device?), "source": {"kind": "reconstructed", "window"} \| null}`; `404` not found, another patient's, or not a heart rate |
| `GET /trace/cuff-attempts/{attemptId}` | `{"attempt": SendAttemptEntry, "messageSent": "<ER7>" \| null, "resources": Bundle (collection: AuditEvent as stored, Observation?, Provenance?)}`; `404`; `422` AuditEvent unreadable |
| `GET /messages?kind=&outcome=&count=&cursor=` | `{"entries": [...], "nextCursor": string \| null, "unreadable": number, "partial": boolean}`, newest first by send time |
| `GET /cuff/samples` | cuff-sim's list, forwarded |
| `POST /cuff/readings` | forwarded to cuff-sim unchanged; its `200/400/413/422/429/500`, body and `Retry-After` unchanged; `502` cuff-sim unreachable or unusable; `504` timed out (may have been sent) |
| `OPTIONS <any path>` | CORS preflight, no token: `204` for a known route from an allowed origin with its method and headers; the same `403` for anything else, an unknown path included |

A token in the query string (`access_token`, `token`, `id_token`) is refused with `400` and
`WWW-Authenticate: Bearer error="invalid_request"`, even beside a valid header. The `Bearer`
scheme name is matched without regard to case (RFC 9110 section 11.1).

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
| 10 | `openemr` in `/status` | mock: reachable | `{"reachable": false, "detail": "not-checked"}` until T15 installs a probe | T15 |
| 11 | `503` on protected routes | only login | when the secret is missing **every** route answers `503` (`login-not-configured`): no token can be checked | none (an `ApiError`) |
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
  60), two limits with different jobs:
  - **5 per client address, checked before the password.** Once an address has 5 failures, its
    sixth attempt and every one after it in the window get `429` with `Retry-After` (whole
    seconds until the oldest failure ages out), right password or not, so a blocked address
    learns nothing more. The slot is reserved under one lock before the password is checked, so
    concurrent attempts from one address cannot all slip through (tested with 12 threads: 5 get
    `401`, 7 get `429`). A success gives its own slot back but does not clear earlier failures (a
    NAT-mate attacker could otherwise reset the count on every reviewer login). A malformed body
    is not a guess and uses no slot. IPv6 clients are counted per /64.
  - **60 in total per instance, for failures only.** Checking the total before the password
    would let about one wrong login a second, spread over twelve addresses, lock every reviewer
    out for good. So once the total is exceeded the password is still checked (a digest
    comparison and a cached key): a correct one gets in (`200`), a wrong one gets `429` and is
    not counted again.
  - **What bounds guessing, stated plainly.** Once the total is exceeded it no longer limits
    how many passwords can be tried; verifications are bounded only per address:

    | Attacker | Guesses a minute |
    | --- | --- |
    | one address (an IPv6 /64 counts as one) | 5 |
    | N addresses | 5 × N |
    | a wrong `TRUSTED_PROXY_HOPS` (the key is then a value the caller writes) | as many as the service answers: bounded only by throughput (instances × threads) |

    What protects the login then is the password: a random 16-character password (T27's
    enforced minimum; 24 or more is recommended) has far more values than any of these rates
    can try. T22 must set `TRUSTED_PROXY_HOPS` right for the one path that reaches the service.
    The costs of a flood are log volume and Cloud Run usage (requests, CPU, instances up to
    `max_instance_count`). No hard ceiling on verifications is applied: it would bring the
    lockout back.
  - **Log volume under a flood.** Below the total every failure is logged (at most 60 a minute
    per instance). Above it, and for refusals by the per-address limit, the first event per
    address per window is logged and the rest only counted; when the window has passed, one
    `login-events-suppressed` line reports the count (tested: 200 refused attempts from 20
    addresses write 3 + 20 + 20 lines, then two summaries).
  - **The address table is capped** (10,000). Only entries whose window has expired are dropped
    to make room, so a flood of fresh addresses cannot evict an attacker's history. When the
    table is full of live entries, a new address is treated as at its limit: its password is
    still checked, so a correct one gets in, and a wrong one gets `429`.
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
  bodies at 1 KiB, by the proxy itself (`bodies.read_body`): it reads at most limit + 1 bytes
  and refuses when there are more, and refuses a body shorter than its `Content-Length`.
  Werkzeug's `MAX_CONTENT_LENGTH` is not used, because under gunicorn a chunked body (no
  `Content-Length`, `wsgi.input_terminated`) is silently cut at the limit and would have been
  forwarded truncated. Tested with gunicorn's environ at 16384, 16385 and 40000 bytes and against
  a real gunicorn on 127.0.0.1 with `Transfer-Encoding: chunked`. Login JSON is strict: UTF-8,
  no `NaN`, no repeated keys, exactly one field.
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

## For T15: how `share/` plugs in

- Create the package **`services/fhir-proxy/fhir_proxy/share/`** (a subpackage, so it is
  installed with the service), with its tests under **`services/fhir-proxy/tests/share/`**
  (the orchestrator is amending T15's Owns to these two paths).
- Its `__init__.py` defines `register(proxy: ProxyContext) -> None`. `create_app` imports it if
  present and calls it once at start-up (`extensions.load_extension`). In `register`:
  - `proxy.add_route(Route("POST", "/share", "share", "view", "<summary>"), handler)`: the route
    joins the same table, so it gets the token check, CORS, the headers, the request budget,
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
| `PUBLIC_BASE_URL` | yes | | the URL the page reaches the proxy at, for `Bundle.entry.fullUrl`: behind the Firebase Hosting rewrite `https://<hosting domain>/api` (its path must equal `PATH_PREFIX`). Never taken from `Host` or forwarded headers |
| `HEALTH_SYNC_URL` | no | empty | health-sync's Cloud Run URL; empty: `state: unknown`, `health-sync-not-deployed` |
| `PATIENT_ASSIGNING_AUTHORITY`, `PATIENT_FAMILY_NAME`, `PATIENT_GIVEN_NAME` | no | empty | the same values as cuff-sim's; all or none. With them the trace shows a sent sample's exact bytes |
| `PATH_PREFIX` | no | empty | `/api` when the page reaches the proxy through the Firebase Hosting rewrite (Hosting forwards the full path) |
| `SESSION_TTL_SECONDS` | no | `28800` | token lifetime, 60-86400 |
| `TOKEN_ISSUER` | no | `pulsebridge-fhir-proxy` | JWT `iss`/`aud`; also salts the key derivation (set a deployment-specific value to defeat shared precomputation) |
| `PASSWORD_CACHE_SECONDS` | no | `30` | rotation delay, 0-300 |
| `TRUSTED_PROXY_HOPS` | no | `1` | see below |
| `LOGIN_FAILURES_PER_ADDRESS` / `LOGIN_FAILURES_TOTAL` / `LOGIN_WINDOW_SECONDS` | no | `5` / `60` / `60` | the login limit |
| `FHIR_TIMEOUT_SECONDS` | no | `10` | per Cloud Healthcare request, 1-30 |
| `CUFF_FORWARD_TIMEOUT_SECONDS` | no | `55` | 10-3000, at least 10 s below `REQUEST_TIMEOUT_SECONDS`; the budget of the cuff send (below) |
| `REQUEST_BUDGET_SECONDS` | no | `50` | 5-3000, at least 10 s below `REQUEST_TIMEOUT_SECONDS`; the budget of every other request |
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
T20/T22"). The proxy itself is public: Cloud Run invoker IAM check disabled
(`invoker_iam_disabled = true`); no `allUsers` binding. The session token is the gate.

**One front door.** The page reaches the proxy one way only: through the Firebase Hosting
rewrite (`PATH_PREFIX=/api`, `PUBLIC_BASE_URL=https://<hosting domain>/api`). It must not call
`*.run.app` for some routes and Hosting for others: the two put different entries at the right
of `X-Forwarded-For`, and no single `TRUSTED_PROXY_HOPS` fits both.

**`TRUSTED_PROXY_HOPS`: measure it on the first deploy.** Behind the rewrite the right-most entry
may be Firebase's own egress address, which would put every reviewer in one bucket. Make two
failed logins through the hosting domain (one with a made-up `X-Forwarded-For: 192.0.2.1`
header) and read the `login-failed` log lines: the `forwarded_for_entries` count (without the
made-up header) and which `client_address` came out tell you whether `1` or `2` is right. While
`*.run.app` stays reachable directly, a hop count of 2 lets a direct caller choose its own key
(the left of two entries), and then only throughput bounds its guesses (see "Reviewer login").

**Possible follow-up: `TRUSTED_PROXY_CIDRS`.** A hop count cannot tell a Hosting-forwarded request
from a direct `*.run.app` one. A setting listing Firebase Hosting's egress address ranges would:
trust the second-from-right `X-Forwarded-For` entry only when the right-most entry, which Cloud
Run itself appends, lies inside those ranges, and otherwise key on the right-most entry. A direct
caller could then not choose its own key. Not built; it needs the ranges Hosting really uses
(measured on the first deploy) and a way to keep them current.

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

**Instances and concurrency.** `max_instance_count = 2` (s6). The login limit is in memory per
process and the image runs one worker, so the real per-address limit is up to 5 × instances a
minute and the total up to 60 × instances. Do not raise `--workers`. Set Cloud Run's
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
gunicorn --bind ":${PORT:-8080}" --workers 1 --threads 8 --timeout 0 --preload "fhir_proxy.app:create_app()"
```

One worker (the login limit is per process), eight threads (the messages log looks up stored
messages in parallel), `--timeout 0` (Cloud Run's request timeout is the limit), `--preload`
(`create_app()`, which checks the configuration, runs in gunicorn's master before the port
opens, so a bad configuration exits without ever listening and Cloud Run's startup probe fails;
services/README.md, "Start-up failures"). The image (`Dockerfile`) runs this command without
`--bind` (it sets `PORT=8080`). Fork safety with `--preload`: `create_app()` starts no thread
(its `ThreadPoolExecutor`s start threads on first submit), holds no lock and opens no
connection; on Cloud Run, finding Application Default Credentials asks the metadata server
once, as for the other services. Credentials are
Application Default Credentials: on Cloud Run the `fhir-proxy` service account (FHIR reader on
both FHIR stores, HL7v2 consumer, secret accessor on `reviewer-password`); identity tokens for
cuff-sim and health-sync come from the metadata server. No key file.

## Logging

One JSON object per line on stdout. Events: `login-failed` (`client_address`,
`forwarded_for_entries`), `login-rate-limited` (`client_address`, `retry_after`),
`login-succeeded`, `token-refused` (`reason`), `reviewer-password-unavailable` (`problem`:
`missing`/`empty`/`unreadable`), `unreadable-send-attempt` (`attempt_id`), `attempts-skipped`
(`count`), `message-lookup-failed` (`error_type`), `store-only-scan-truncated`,
`store-only-check-truncated`, `messages-frontier-degenerate` (`rows`), `paging-link-unrecognised`, `health-sync-status` (`outcome`,
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
  metadata server mints one for the proxy's service account.
- That a mounted secret file reflects a new `latest` version on read without a restart (s6,
  from a search summary).
- Least-privilege roles: that `fhirResourceReader` allows every search above.
