# PulseBridge dashboard (`web/`)

The single-page PHR dashboard: one page, no navigation. Vite and TypeScript, no UI framework (D5).
It talks to exactly one backend, `fhir-proxy` (D10), through a typed client that has a mock
implementation, so the whole page runs locally with synthetic data and no server.

This directory was set up by task T16 as a **shell**: layout, login, status strip, the figure,
empty feature areas, the API client and its contract, and the conventions below. T17, T18 and T19
fill the feature areas.

## Run it

Needs Node 22.12 or newer.

```bash
cd web
npm ci            # exact versions from package-lock.json
npm run dev       # mock mode on http://localhost:5173 (any non-empty password logs in)
```

| Command                | What it does                                                                    |
| ---------------------- | ------------------------------------------------------------------------------- |
| `npm run dev`          | Dev server in **mock mode**: synthetic data, no network.                        |
| `npm run dev:http`     | Dev server against a real proxy (`VITE_FHIR_PROXY_URL`, see below).             |
| `npm run build`        | Type check, production build (HTTP client) into `dist/`, then the bundle check. |
| `npm run build:mock`   | Mock-mode build into `dist-mock/` (used by the browser test).                   |
| `npm test`             | Unit tests (Vitest, jsdom).                                                     |
| `npm run test:browser` | Builds the mock page and runs the Playwright tests in Chromium.                 |
| `npm run lint`         | ESLint (type-aware) and Prettier check. `npm run format` fixes formatting.      |
| `npm run typecheck`    | `tsc --noEmit` (strict).                                                        |

From the repository root: `make web-install`, `make web-lint`, `make web-test`, `make web-build`,
`make web-browser-test`, or `make web-check` for all of them. They are not part of `make check`,
which must keep working on a machine without Node; CI runs them in the "Web (lint, test, build)"
job, browser test included (CI installs Chromium with `npx playwright install --with-deps chromium`).

### Configuration

| Variable              | Meaning                   | Default                                                            |
| --------------------- | ------------------------- | ------------------------------------------------------------------ |
| `VITE_FHIR_PROXY_URL` | Base URL of `fhir-proxy`. | `/api` on the page's own origin (the Firebase Hosting rewrite, D4) |

**Mock data sets.** In mock mode, `?mock-data=<id>` on the page URL picks the data set
(`src/api/mock-data.ts`, `MOCK_DATA_SETS`): `utc` (the default) writes every heart-rate time in
UTC; `dst-new-york` writes them in the fixed IANA zone America/New_York with the offset in force at
each instant (`2026-11-01T01:30:00-04:00`, then `...-05:00`), as health-sync writes the user's
zone. The zone belongs to the data, never to the browser. A range crosses a clock change when one
falls inside it: the browser test `e2e/mock-dst.e2e.ts` pins the page's clock to 2026-11-01 12:00
EST, eleven hours after the autumn change.

Two more switches exist for browser tests, in mock builds only (both are parsed in
`src/api/mock-data.ts`, and `scripts/check-bundle.mjs` fails an HTTP build that contains them):

- `?mock-data=hostile`: UTC data plus markup (`HOSTILE_MARKUP`: an `<img onerror>`, a
  `<script>` and a 612-character unbroken token) in every text that comes from another system:
  each sample's description, an AE's MSA-3 reason and so the raw ACK, an extra NTE segment in a
  malformed sample's message text, and the diagnostics of a refusal (a built reading with
  systolic 333 is refused with 422). `src/cuff/cuff.e2e.ts` shows it is rendered as text, with no
  CSP violation and no page scroll.
- `?mock-audit=build|write`: every cuff send fails to record its AuditEvent
  (`MockClientOptions.auditFailure`), so "not recorded" rows can be seen in the browser.

Put a local value in `web/.env.local` (git-ignored); `.env.example` shows the format. Vite copies
every `VITE_` variable into the public bundle, so **never put a secret in one**. The mode (mock or
HTTP) is not a variable: it is `vite --mode mock`, fixed at build time, and an HTTP build does not
contain the mock client at all (`scripts/check-bundle.mjs` fails the build if it does).

## Page layout and who fills what

```
+--------------------------------------- header -----------------------------------------+
| PulseBridge [Mock data]  status strip: newest data, counts, FHIR store, OpenEMR Log out |
+----------------------------+--------------------------+-------------------------------+
| Blood pressure (episodic)  |       line-art figure     | Heart rate (continuous)       |
|                            |   <-- cuff    watch -->   |                               |
| [cuff-panel]          T18  |                          | [heart-rate-panel]       T17  |
|                            | [share-action]           |                               |
| [cuff-trace]          T18  |   button T16, action T19 | [heart-rate-trace]       T17  |
|                            | [share-trace]       T19  |                               |
|                            | [share-readback]    T19  |                               |
+----------------------------+--------------------------+-------------------------------+
| > Messages log  [messages-summary] T19     collapsible; [messages-log] T19             |
+----------------------------------------------------------------------------------------+
```

On a phone the same areas stack: figure, blood pressure, heart rate, share, with the log strip
pinned to the bottom. The DOM order is that phone order, so keyboard order is the same at every
width: header, cuff side, watch side, share, log.

## Conventions for T17, T18 and T19

### Who owns which files

T17, T18 and T19 run in parallel, so every file has exactly one owner (orchestrator decision after
the T16 review):

| Path                                                                                                                                                                 | Owner                                                           | Rule for the others                                                                               |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `src/heart-rate/`                                                                                                                                                    | T17 (T34 made the edits a contract change needed, listed below) | do not edit                                                                                       |
| `src/cuff/`                                                                                                                                                          | T18                                                             | do not edit                                                                                       |
| `src/share/`, `src/messages/`                                                                                                                                        | T19                                                             | do not edit                                                                                       |
| `package.json`, `package-lock.json`                                                                                                                                  | T17, **only** to add the chart library (D5)                     | T18 and T19 add no dependencies                                                                   |
| `src/api/` (contract, wire parsing `parse.ts`, errors, HTTP client, mock client, mock data, T31 example copies)                                                      | the shell (T16, then T34)                                       | a task that needs a contract or mock change **reports it to the orchestrator** instead of editing |
| `src/shell/`, `src/ui/`, `src/state/`, `src/styles/`, `e2e/` (shell and mock-data browser tests), configs, `scripts/` (bundle check and its rules, mock-sample sync) | the shell (T16, then T34)                                       | same: report, do not edit                                                                         |

### Mounting

Each feature exports **one** mount function from its `index.ts`. The shell (`src/shell/app.ts`)
calls it once after login. A feature only touches the elements in its `slots`.

| Directory         | Task | Export           | Slots                                                          |
| ----------------- | ---- | ---------------- | -------------------------------------------------------------- |
| `src/heart-rate/` | T17  | `mountHeartRate` | `panel`, `trace`                                               |
| `src/cuff/`       | T18  | `mountCuff`      | `panel`, `trace`                                               |
| `src/share/`      | T19  | `mountShare`     | `action`, `trace`, `readback`                                  |
| `src/messages/`   | T19  | `mountMessages`  | `log`, `summary` (text only; it sits inside the toggle button) |

Each `index.ts` currently holds a placeholder. Replace the function body; keep the name and the
signature `(context: FeatureContext<Slot>) => void` (`src/shell/features.ts`). The context gives:

- `client`: the API client (below).
- `selection`: the page-wide selection store (below).
- `slots`: the elements you own, each empty.
- `signal`: an `AbortSignal` aborted when the dashboard is torn down (logout or session end).
  Pass it to every client call (`{ signal }`), to `addEventListener(..., { signal })`,
  `selection.subscribe(fn, signal)`, `onDataChanged(fn, signal)` and `el(..., { signal })`, so
  nothing outlives the dashboard and no request answers after the next login.
- `notifyDataChanged(kind)` and `onDataChanged(listener, signal)`: see "Telling each other data changed".
- `refreshStatus()`: reload the header's status strip. Rarely needed: the strip already reloads
  on every data change, every 5 minutes while the tab is visible (`STATUS_REFRESH_MS`) and when
  the tab becomes visible again (unless a load started in the last minute), and announces
  `'sync'` itself when the time of the newest watch data changes.

If a mount function throws, the shell logs it and shows "This area failed to load" in that
feature's first slot; the rest of the page keeps working. Split your code into more files inside
your directory as you like.

### Using the client

```ts
import { LOINC } from '../api/fhir';

const observations = await client.searchObservations(
  {
    code: LOINC.heartRate,
    range: { start: new Date(Date.now() - 24 * 3600_000), end: new Date() },
  },
  { signal },
);
```

- Every method is in `PulseBridgeApi` (`src/api/contract.ts`); both the mock and the HTTP client
  implement it, so code written against the mock works unchanged against the proxy.
- **401 is handled for you.** The client clears the token and the shell shows the login form; the
  call rejects with `UnauthorizedError`, which you can ignore (the dashboard is about to go).
- **Aborted calls** (you passed `signal` and the dashboard was torn down) reject with an
  `AbortError`; ignore it.
- Other failures reject with `ApiError` (`status`, `message`, `outcome`, the FHIR
  `OperationOutcome` when the proxy sent one, and `retryAfterSeconds` on a 429). Status 0 means
  the network failed. Show `error.message` to the user.
- **The cuff route's refusals are typed.** `submitCuffReading` rejects with `CuffSendError`
  (`src/api/errors.ts`, a subclass of `ApiError`) whose `reason` is cuff-sim's documented case:

  | Status | `reason`        | When (services/cuff-sim/README.md)                                                               | Sent? |
  | ------ | --------------- | ------------------------------------------------------------------------------------------------ | ----- |
  | 400    | `invalid-body`  | the body is not a JSON object                                                                    | no    |
  | 413    | `too-large`     | the body is over 16 KiB                                                                          | no    |
  | 422    | `invalid-input` | bounds, a type, an unknown field or sample, a sample not for this transport; `fields` names them | no    |
  | 429    | `rate-limited`  | over the per-instance limit; `retryAfterSeconds` from `Retry-After`                              | no    |
  | 500    | `server-error`  | a built message failed HL7v2 validation, or a bug                                                | no    |
  | 504    | `no-answer`     | cut off upstream (Cloud Run's request timeout): it **may** have been sent, unrecorded            | maybe |

  `error.fields` lists the request fields the OperationOutcome names (`systolic`, `diastolic`,
  `sendMalformed`, `transport`, or `body`). cuff-sim's `diagnostics` are fragments with the field
  only in `expression` ("is required"), so `error.message` puts each field before its fragment and
  joins the issues: `systolic: is required; diastolic: is required`. Any other status (for example
  a 502 from the proxy) stays a plain `ApiError`.

- **`Retry-After`** is read only in its delay-seconds form (digits only, RFC 9110 section
  10.2.3). Anything else (negative, decimal, `10abc`, an HTTP-date) gives `retryAfterSeconds: null`,
  for the cuff route and for login alike.

- Never call `fetch` yourself: the page has one backend and one client.

### Using the selection store

```ts
// Set it when the user picks something (T17 chart point, T18 readings row):
selection.set({
  source: 'watch',
  observationId: obs.id,
  summary: 'Heart rate 68 /min, 09:05',
  // Share only `final` readings (T06/T15): give the reason when it may not be shared.
  unshareableReason: obs.status === 'final' ? null : 'this window is still preliminary.',
});
selection.set({
  source: 'cuff',
  attemptId,
  observationId: maybeNull,
  summary: 'BP 121/79',
  unshareableReason: null,
});

// React to it (trace panels, share button, your own highlight):
selection.subscribe((current) => {
  /* current is Selection | null */
}, signal);
```

- There is one selection for the page: picking a cuff reading replaces a heart-rate point. When
  the selection moves to the other side, drop your highlight.
- `subscribe` calls the listener immediately with the current value, then after each change.
- `shareableObservation(selection)` gives the `Observation/<id>` the Share button would send, or
  null: nothing selected, a cuff reading that produced no Observation, or a selection with an
  `unshareableReason`. The button is enabled exactly when it is non-null, and otherwise its
  description (the hint under it) says why.
- **For T18:** a send whose AuditEvent could not even be built has `attemptId: null`. It has no
  trace to look up and is not in the messages log, so show its result as returned and do not put
  it in the selection (which needs an `attemptId`).
- **For T17:** the selection does not carry the Observation's `status`, so set
  `unshareableReason` yourself for any window whose status is not `final` (the newest windows are
  `preliminary` until the next sync, T10). Re-set the selection if a refresh finalises it.

### Telling each other data changed

Features never call each other. A feature that **changes** data on the server tells the others
with `notifyDataChanged(kind)`; a feature that **shows** data listens with
`onDataChanged(listener, signal)` and re-fetches what it shows through the client. The event
carries no data on purpose: the server is the source of truth (`src/state/data-changed.ts`).

| Kind          | Who calls it                                                                                  | Who listens                                   |
| ------------- | --------------------------------------------------------------------------------------------- | --------------------------------------------- |
| `'cuff-send'` | T18, after `submitCuffReading` returns (whatever the ACK)                                     | T19 messages log; the status strip (built in) |
| `'share'`     | T19, after `share` returns                                                                    | T19 if it lists shares; the status strip      |
| `'sync'`      | the status strip (built in), when the newest watch data time changes between two status loads | T17 chart                                     |

A listener is not called at subscribe time; load your data once in the mount function.

**What `lastSyncAt` means.** Despite its name, `watchSync.lastSyncAt` is the time of the **newest
heart-rate write** to the FHIR store, not the time of the last sync run: health-sync writes only
windows that changed, so while the watch is off the wrist the time stays where it was. The strip
labels it "Newest watch data" for that reason. (The field kept its name so T17's code did not
change.)

**How `'sync'` is announced.** The strip compares each status load's `watchSync.lastSyncAt` with
the previous load's (`src/shell/sync-watch.ts`): it announces on a change, but not on the first
load, not when the time is unchanged (compared as instants) and not when it becomes null (a later
real time is announced again). A load that answers after a newer one started is ignored. The strip
does not reload on its own announcement, and a reload that finds the same time announces nothing,
so the two cannot loop; only the strip's own `'sync'` is skipped, and any other change reported
meanwhile still reloads it. With health-sync running every 15 minutes and the strip reloading every
5, new data is noticed within 5 minutes, and a window that was provisional can become final during
a session.

**What the polling costs.** About 12 status loads an hour per visible tab (one every 5 minutes,
plus at most one per minute when the tab becomes visible again), and none while it is hidden. A
session lasts at most 8 hours (the token's lifetime), so one tab left open makes at most about 100
loads. Each is one proxy call; health-sync caches its own answer for 60 s.

**Waiting for "pending".** After a cuff send the stored message is `pending` until `v2-to-fhir`
labels it (D7), usually seconds. The feature that shows a pending item polls for it, with
`pollUntil(check, { signal })` from `src/state/poll.ts`: every 3 s, only while the tab is visible,
stopping when nothing shown is pending, after 2 minutes (leave it shown as "pending", which is a
real state: the mapper may have failed), or when the signal aborts. T18 polls
`getCuffAttemptTrace` for the reading it just sent; T19 polls `listMessages` while a visible row
is pending. Nobody else polls.

### Rendering data

- Build DOM with `el()` and `svg()` from `src/ui/dom.ts`. String children and `props.text` become
  text, never markup.
- `el()` and `svg()` **throw** on an `on…` attribute (use `props.on`), on `srcdoc`, and on an
  `href`, `src`, `xlink:href`, `action`, `formaction` or `poster` value that is not relative, a
  `#fragment` or `https:`.
- **Never use `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`,
  `createContextualFragment`, `setHTMLUnsafe` or `srcdoc`, and do not reach them indirectly**
  (`Object.assign(node, { innerHTML })`, computed keys, `Reflect.set`). ESLint fails on all of
  these (`eslint-plugin-no-unsanitized` plus `no-restricted-syntax` rules), and so does
  `setAttribute('on…')` or a `javascript:` URL.
- Data from other systems is untrusted text: patient names, OperationOutcome text, HL7v2 segments,
  and the raw ACK. The proxy gives the ACK decoded (`commitAck.raw`); inside the AuditEvent
  resource it stays base64 (`ack-message`). Show either with `codeBlock`, never as markup.
- `codeBlock(content, label)` shows JSON or ER7 (HL7v2 segments separated by `\r` are shown one per
  line). `placeholder(text)` shows a muted empty-state line.
- `createTabs(label, tabs, signal)` (`src/ui/tabs.ts`) gives accessible trace-panel tabs with
  arrow-key support; fill `tabs.panel(id)`.
- No inline `style="..."` attributes and no inline scripts: the Content-Security-Policy blocks
  them. Setting `element.style.x` from script is allowed. Charts that draw to `<canvas>` or SVG are fine.

### Element ids

One document holds the shell and four features built in parallel, so an id must say who owns it:

| Owner               | Id prefix | Example                             |
| ------------------- | --------- | ----------------------------------- |
| T17 heart rate      | `hr-`     | `hr-summary`                        |
| T18 cuff            | `cuff-`   | `cuff-systolic`                     |
| T19 share           | `share-`  | `share-hint`                        |
| T19 messages log    | `log-`    | `log-filter`                        |
| the shell           | `shell-`  | `shell-messages-log`                |
| the shell's helpers | `ui-`     | `ui-tabs-3-tab-fhir` (`createTabs`) |

- Every id you create starts with your feature's prefix, and is used once. For ids that repeat
  (one per row), use `uniqueId('cuff-row')` from `src/ui/dom.ts`.
- Ids made by `createTabs` start with `ui-` and are unique by construction; use them as they are.
- `main` (the skip link's target) and the login form's `login-` ids are the only shell exceptions.
- `src/shell/element-ids.test.ts` mounts the real features against the mock and fails on a
  duplicate id, or an id inside a feature's slot without that feature's prefix. Run it after a merge.
- Prefer `aria-labelledby` and `aria-controls` with these ids over global selectors; tests should
  find elements by role and name where they can.

### Styling tokens

All colours, spacing and type are CSS custom properties in `src/styles/tokens.css`, with light and
dark values (`prefers-color-scheme`). Use them, never raw values:

| Token                                                            | Use                                                                                 |
| ---------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| `--color-bg`, `--color-surface`, `--color-surface-sunken`        | page, cards, code blocks                                                            |
| `--color-text`, `--color-text-muted`, `--color-border`           | text and decorative lines                                                           |
| `--color-control-border`                                         | the boundary of an input or control (at least 3:1 against the surface, WCAG 1.4.11) |
| `--color-episodic` / `--color-continuous`                        | the cuff side / the watch side (series colour, accents)                             |
| `--color-ok`, `--color-warn`, `--color-error`, `--color-neutral` | states; always pair with words                                                      |
| `--color-primary`, `--color-primary-text`, `--color-focus`       | primary button, focus ring                                                          |
| `--space-1` … `--space-6`, `--radius`, `--gutter`                | spacing (4 px steps), corners, page gutter                                          |
| `--font-sans`, `--font-mono`, `--text-xs` … `--text-lg`          | system fonts only (no web fonts)                                                    |

Put a feature's CSS in its own directory (for example `src/heart-rate/heart-rate.css`) and import
it from that feature's `index.ts`; prefix class names with the feature (`.hr-…`, `.cuff-…`,
`.share-…`, `.log-…`). Shared classes: `.card`, `.button`, `.button--primary`, `.placeholder`,
`.code-block`, `.visually-hidden`.

### Adding tests

- **Unit tests:** `*.test.ts` next to the code, inside your directory. Vitest with jsdom; build a
  `FeatureContext` by hand (see `src/share/share.test.ts`) or use `new MockClient({ session,
onUnauthorized, latencyMs: 0, now: () => fixedTime })` with `new Session(sessionStorage, () =>
fixedTime)` for deterministic data (see `src/api/mock-client.test.ts`).
- **Browser tests:** `*.e2e.ts` inside your directory (Playwright picks up `src/**/*.e2e.ts`).
  They run against the built mock page; fail on any console error, as `e2e/shell.e2e.ts` does.
  The shell's keyboard test checks only the order of areas, so your controls do not break it.
- Every test is break-checked (CLAUDE.md): break the implementation, see the test fail, restore.
- Use synthetic data only. Never paste real readings into a test.

### How faithful the mock is

- Heart-rate and blood-pressure Observations have the element structure of T06's examples (tests
  compare them).
- "Send malformed" samples, their classes (`structurally_invalid`, `unparseable`,
  `no_acknowledgment`, `parses_but_cannot_map`), payload bytes and expected outcomes are a generated
  copy of T07's `libs/pulsebridge_hl7v2/samples/samples.json`
  (`node scripts/sync-mock-samples.mjs`; a test fails if the copy drifts). A malformed send shows
  the sample's own bytes.
- A built reading is byte for byte what T07's builder makes (a test compares it with T07's
  `valid_bp_reading`).
- Heart-rate and blood-pressure Observations carry `performer`, the same Patient reference as
  `subject` (T06, changed in T33: the readings are self-measured).
- Send-attempt AuditEvents follow T31's final model: they are built from T31's five examples
  (copied into `src/api/t31-examples/`; `src/api/t31-examples.test.ts` fails when a copy differs
  from `libs/pulsebridge_fhir/examples/`) and a test compares them element by element.
- The mock share strips `id`, `meta`, `identifier`, `device` and `performer` from the Observation
  before "posting" it, as T06 says the real share must (`performer` names our Patient; OpenEMR
  does not keep it, spike S1).
- **A cuff send is checked as cuff-sim checks it**: integers from 1 to 400 mm[Hg], systolic above
  diastolic, both required unless `sendMalformed` is set (then still checked if present), a known
  sample that lists the transport; refusals are `CuffSendError` 422 with one OperationOutcome issue
  per wrong field. The **rate limit** is cuff-sim's token bucket (burst 3, 6 a minute, per
  instance), checked after the input, answering 429 with `retryAfterSeconds`; pass
  `rateLimit: null` to `MockClient` in a test that sends many readings.
- **A send result has cuff-sim's fields**: `result`, `framing` (`standard` for a built reading
  over MLLP, the sample's own `mllp_framing` from T07 for a malformed one, null over HTTPS),
  `messageName` with `messageNameSource`: from the "ingest response" over HTTPS, and over MLLP the
  **derived** name `<store>/messages/<urlsafe-base64(sha256(bytes))>`, computed exactly as
  cuff-sim's `derived_message_name` (a test checks fixed vectors cuff-sim computed for T07's sample
  bytes). A real derived name is over the bytes actually sent: on a deployment cuff-sim (T33)
  substitutes the configured patient into every sample except the seven T07's samples README
  lists (`with_patient`), so for those samples the bytes, and the name, differ from the
  manifest's. In the mock the configured patient is T07's own `SYN-000123`, so the bytes are the
  manifest's. As in cuff-sim since T33, the derived name **is** recorded in the AuditEvent, as the
  detail `derived-message-name` and never as `entity.what`; the log row has it as
  `derivedMessageName`, and `messageName` stays the store-returned name only (null over MLLP).
- **Refusals use cuff-sim's exact texts**: the field in `expression`, a fragment in `diagnostics`
  ("is required", "must be from 1 to 400 mm[Hg]", "must be greater than diastolic"), `code`
  `invalid`, every issue collected before refusing; an absent `transport` is `mllp`; a null value
  counts as absent; an unknown field is a `body` issue; a body that is not an object is 400.
  `audit.detail` uses cuff-sim's sentences.
- A reading inside cuff-sim's limits but outside the mapper's (`MAPPER_PLAUSIBLE_RANGE`) is stored
  with `AA` and then mapped to `error` / `out-of-range`, as T12 does.
- `new MockClient({ auditFailure: 'build' | 'write' })` makes every send fail to record its
  AuditEvent: `build` gives `attemptId: null`, `write` an id with nothing stored under it (the trace
  is a 404). The send result still says what the store answered, and the attempt is not in the log.
  (The proxy may later show such a stored message as a `store-only` row; the mock does not.)
- **Heart-rate data arrives at sync runs**: the mock's health-sync runs every 15 minutes (minute 2,
  17, 32, 47). Windows, their `preliminary`/`final` status (T10's 30-minute cutoff at that run) and
  `watchSync.lastSyncAt` change together, once per run, so the status strip's `'sync'`
  announcement and the chart's re-fetch can be seen in mock mode.
- The status is health-sync's shape (`connected`, no notes, `strandedPreliminary` 0); the strip's
  other states and notes are covered by unit tests.
- The trace's `source` is `{kind: 'reconstructed', window}`: what the proxy is expected to send.

## Security basics

- **Token storage.** The bearer token from `POST /auth/login` is kept in `sessionStorage`
  (`src/api/session.ts`): per tab, gone when the tab closes, and never sent automatically. No
  cookie is used, so there is nothing for cross-site request forgery to ride on (D9,
  docs/spikes/s6.md). Logout and any 401 delete it.
- **A late 401 cannot end a newer session.** The client clears the session on a 401 only if the
  stored token is still the one that request sent (or none is stored). Logout also aborts every
  request the dashboard has in flight.
- **Requests** are sent with `credentials: 'omit'`, `cache: 'no-store'` (health data is not kept
  in the HTTP cache) and `redirect: 'error'`. A paging `next` link is followed only when it stays
  inside the proxy's base URL, so the token cannot be sent to another host.
- **`VITE_FHIR_PROXY_URL` is checked at build time** (`validateProxyUrl` in `vite.config.ts`):
  empty, a same-origin path, an `https:` URL, or `http://localhost` / `http://127.0.0.1` for
  development; anything else stops the build with a message. (A production build that points at
  localhost still fails the bundle check.)
- **Content-Security-Policy.** The build adds a `<meta>` policy (`vite.config.ts`):
  `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src <proxy>;
base-uri 'none'; form-action 'none'; object-src 'none'`. A mock build has `connect-src 'none'`,
  so mock mode cannot make a network request at all. `scripts/check-bundle.mjs` fails the build if
  any of those directives is missing or wider, and requires `connect-src` to be exactly `'self'`
  (or `'self'` plus the configured proxy origin). The dev server has no policy (Vite's hot reload
  injects styles). **The hosting layer (Firebase Hosting, T22) must send the real policy as an
  HTTP header**, which can also set `frame-ancestors 'none'` (a `<meta>` policy cannot).
- **No secrets in the bundle.** The page holds no key or password; the only credential it ever
  sees is the reviewer's session token, at run time.

## Dependencies

Every version is pinned exactly in `package.json` (`.npmrc` sets `save-exact`), and
`package-lock.json` is committed.

### Runtime dependencies (shipped in the page)

| Package        | Why                                                                                             | Size in the HTTP build (`npm run build`, Vite's report)                                                          |
| -------------- | ----------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `uplot` 1.6.32 | The heart-rate chart (D5, chosen by T17; see `src/heart-rate/README.md`). MIT, no dependencies. | its own chunk, 50,990 B (22.24 kB gzip), loaded by `import()` on first draw; its stylesheet is in the page's CSS |

For comparison, our own code is the main chunk, 46,619 B (17.25 kB gzip), and the CSS is 13,741 B
(3.52 kB gzip) including uPlot's. **uPlot must stay out of the main chunk**, so the login form
never waits for it: `scripts/check-bundle.mjs` reads Vite's build manifest (`dist/.vite/manifest.json`,
`build.manifest: true`) and fails the build if the entry chunk, or any chunk it imports statically,
is uPlot's or contains uPlot's code (`scripts/bundle-rules.mjs`, tested on hand-made manifests).
The manifest lists only the build's public file names. It is written into `dist/.vite/` with the
build; **for T22:** Firebase Hosting's default `ignore` (`**/.*`) skips dot-directories, so it is
not deployed. Keep that default.

### Development dependencies

| Package                                     | Why                                                                                                                                                                                   |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `vite`                                      | Dev server and production bundler (D5).                                                                                                                                               |
| `typescript`                                | Strict type checking (`tsc --noEmit`). 6.0, because typescript-eslint does not yet support 7.x.                                                                                       |
| `vitest`                                    | Unit tests; shares Vite's config and TypeScript handling.                                                                                                                             |
| `jsdom`                                     | A browser-like DOM for the unit tests.                                                                                                                                                |
| `@playwright/test`                          | Browser test in Chromium. Pinned to 1.63.0 to match the Chromium build preinstalled on the development machine; `npx playwright install chromium` fetches the matching one elsewhere. |
| `eslint`, `@eslint/js`, `typescript-eslint` | Linting, including type-aware rules.                                                                                                                                                  |
| `eslint-plugin-no-unsanitized`              | Mozilla's rules against passing strings to HTML sinks (added after review).                                                                                                           |
| `prettier`                                  | Formatting.                                                                                                                                                                           |

Known: `npm audit` reports a moderate advisory in `minimatch`, pulled in by typescript-eslint
(development only; it affects glob patterns from untrusted input, which lint never sees). It
clears when typescript-eslint updates.

Adding a dependency, as observed on this project: with an existing `package-lock.json`, npm 10.9.9
adds a package fine, but it drops the `libc` fields npm 11 wrote into the lockfile, which makes a
noisy diff. npm 10.9.9 crashes ("Cannot read properties of null (reading 'edgesOut')") only when
resolving the whole tree from scratch with no lockfile. So add packages with
`npx npm@11.21.1 install --save-dev --save-exact <pkg>@<version>`, which keeps the lockfile diff to
the new package. `npm ci` from the lockfile works with npm 10 and 11.

## HTTP contract (proposal for T14 and T15)

`fhir-proxy` does not exist yet. These are the routes the HTTP client (`src/api/http-client.ts`)
calls; the TypeScript types in `src/api/contract.ts` are the normative shapes. Paths are relative
to the base URL (default `/api`). Where the data is FHIR, the body is FHIR (`application/fhir+json`);
the other bodies are views the proxy assembles (`application/json`). T14 and T15 decide finally.

**Authentication.** Every route except `POST /auth/login` requires `Authorization: Bearer <token>`
and answers `401` when it is missing, expired, altered or signed under a previous password. That
includes `GET /fhir/metadata` (the CapabilityStatement): T14's criterion "unauthenticated requests
are rejected" is simplest to state and test with exactly one exception, and the page never reads
the CapabilityStatement. Errors carry a FHIR `OperationOutcome` body where possible; the client
shows its first `issue.diagnostics`.

| Method and path                                                                                          | Client method          | Request                                                                                                                                                                                   | Response                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| -------------------------------------------------------------------------------------------------------- | ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /auth/login`                                                                                       | `login`                | `{"password": "..."}`                                                                                                                                                                     | `200 {"token", "expires_at"}`; `401` wrong password; `429` with `Retry-After`; `503` not configured                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `GET /status`                                                                                            | `getStatus`            |                                                                                                                                                                                           | `StatusSummary`: `watchSync` is health-sync's `/status` (`state`: its `googleHealth`, `connected` \| `reconnect-needed` \| `not-configured` \| `unknown`; `reason`; `notes` (`backfill-needed`, `refresh-token-rotated`); `strandedPreliminary` number \| null; `checkedAt`) plus `lastSyncAt` (ISO \| null, derived by the proxy, open question 9); `observationCounts {watch, cuff}`; `fhirStore` and `openemr` `{reachable, checkedAt, detail}`. Always 200 from health-sync, so `unknown` means "the check could not finish", not "down"                                                                                                                                                                                                                         |
| `GET /fhir/Observation?code=http://loinc.org\|8867-4&date=gt<start>&date=lt<end>&_sort=date&_count=1000` | `searchObservations`   | FHIR search; `code` is a token (`system\|code`); also `85354-9`. Date rule below                                                                                                          | `Bundle` (`searchset`) of Observations, oldest first; `link[next]` to the next page, under the same base URL                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `GET /trace/heart-rate/{observationId}`                                                                  | `getHeartRateTrace`    |                                                                                                                                                                                           | `{"resources": Bundle (collection: Observation, Provenance, Device), "source": {"kind": "reconstructed" \| "original", "window": <Google Health roll-up window>} \| null}`. The client converts it (below)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `GET /cuff/samples`                                                                                      | `listMalformedSamples` |                                                                                                                                                                                           | cuff-sim's list as it returns it: `[{name, label, class, transports}]`, every T07 sample except class `valid`, `label` = the manifest's `description`; bad-framing samples list only `mllp`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `POST /cuff/readings`                                                                                    | `submitCuffReading`    | `{systolic?, diastolic?, transport: "mllp" \| "https", sendMalformed: <sample name> \| null}`: integers 1 to 400 mm[Hg], systolic above diastolic, required unless `sendMalformed` is set | `200 CuffSendResult` (forwarded to cuff-sim, D10, body unchanged): `{attemptId (string \| null), sentAt, transport, controlId, sample, messageSent, framing, messageName, messageNameSource, result, ack, audit}`; `ack` is `{received: true, code, controlId, reason, raw}` or `{received: false, why: timeout \| connection-closed \| api-error \| unreadable-reply, httpStatus, detail, replyPrefixBase64}`. `400` not a JSON object; `413` over 16 KiB; `422` a wrong field (bounds, type, unknown field or sample, sample not for this transport); `429` + `Retry-After`; `500` built message failed validation or a bug; all with an OperationOutcome naming the field in `issue.expression`, and nothing sent. `504` if cut off upstream (may have been sent) |
| `GET /trace/cuff-attempts/{attemptId}`                                                                   | `getCuffAttemptTrace`  |                                                                                                                                                                                           | `{"attempt": SendAttemptEntry, "messageSent": "<ER7>" \| null, "resources": Bundle (collection: AuditEvent as stored, Observation?, Provenance?)}`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `GET /messages?kind=&outcome=&count=&cursor=`                                                            | `listMessages`         | `kind`: `cuff-send` \| `unreadable-send-attempt` \| `fhir-transaction`; `outcome`: `accepted` \| `error` \| `pending`                                                                     | `{"entries": [...], "nextCursor": string \| null, "unreadable": number}`, newest first. Row shapes below                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `POST /share`                                                                                            | `share`                | `{"observations": ["Observation/<id>", ...]}`                                                                                                                                             | `200 {"outcome": shared \| already-shared \| partly-shared \| failed, "steps": [ShareStep], "readBack": Bundle (collection: Patient, Encounter, Observations as OpenEMR stored them)}`. Each step: `{step, label, request {method, url (path only), body}, response {status, body}, result, durationMs}`; never the private key, the signed assertion or the access token. Failures below                                                                                                                                                                                                                                                                                                                                                                            |

Field names in the JSON views are camelCase, except the login response, which follows
docs/spikes/s6.md as written (`token`, `expires_at`).

### Wire shape and parsed shape

The HTTP client does not hand the proxy's JSON (the **wire** shape) straight to the features. It
converts it into the types in `src/api/contract.ts` (the **parsed** shape), in `http-client.ts`
and `src/api/parse.ts`. Features only ever see the parsed shape, from either client.

| Route                           | Wire (what the proxy sends)                           | Parsed (what a feature gets)                                                                                                   |
| ------------------------------- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `GET /trace/heart-rate/{id}`    | `{resources: Bundle, source: {kind, window} \| null}` | `HeartRateTrace {observation, provenance, device, source: {kind: 'reconstructed' \| 'original' \| 'unknown', window} \| null}` |
| `GET /trace/cuff-attempts/{id}` | `{attempt, messageSent, resources: Bundle}`           | `CuffAttemptTrace {attempt, messageSent, auditEvent, observation, provenance}`                                                 |
| `POST /share`                   | `{outcome, steps, readBack: Bundle}`                  | `ShareResult {outcome, steps, readBack: {patient, encounter, observations}}`                                                   |
| `GET /status`                   | `StatusSummary`                                       | the same, with `watchSync` checked                                                                                             |
| `POST /cuff/readings`           | cuff-sim's result                                     | the same, with `attemptId`, `framing`, `messageName` and `messageNameSource` checked                                           |
| `GET /fhir/Observation?...`     | searchset Bundles, paged with `link[next]`            | `Observation[]`, every page followed (inside the proxy's base URL only)                                                        |

The rule for checked values: **an unrecognised value never becomes a stronger claim than the
server made.** An unknown connection state is `unknown` (not `connected`); an unknown trace source
`kind` is `unknown`, which the trace panel shows without saying "original" or "reconstructed"; a
`source` that is not a roll-up window is null; an unknown `messageNameSource` is treated as
`derived-sha256-unverified` (only the store's own answer is presented as confirmed); an unknown
`framing` is null; unknown `notes` are dropped; a `strandedPreliminary` that is not a whole number
is null ("not known"), never zero; an unknown `result` is worked out from the ACK (T31's mapping);
a `/status` body that is not an object (even JSON `null`) does not throw: it reads as `unknown`,
unreachable and zero counts. `googleHealth` is read as the state when `state` is absent.

**Date rule for heart-rate windows.** The page sends `date=gt<start>&date=lt<end>`. On a Period
(`effectivePeriod`), R4 defines `gt` as "the period ends after the value" and `lt` as "the period
starts before the value", so a window matches when it overlaps the open interval (start, end). A
window that ends exactly at `start` does not match, so an aligned 24-hour range returns 288
windows. (With `ge` it would match, giving 289.) The mock applies the same rule, and T14 should
confirm the Cloud Healthcare store does.

**Heart-rate trace: custom route or FHIR search.** The FHIR-native alternative is
`GET /fhir/Observation?_id=<id>&_include=Observation:device&_revinclude=Provenance:target`, which
returns the Observation, its Device and its Provenance in one standard searchset. I propose the
custom route for now, because the trace also needs the Google Health source JSON, which is not a
FHIR resource today (open question 2), and because it keeps the proxy's search surface, and so its
CapabilityStatement, small. If the source JSON ends up stored as a FHIR resource (for example a
`Binary` referenced from the Provenance), switch to the search and drop the route.

**How a failed share is reported.** Consistent with the rest of the API:

- refused before anything is sent to OpenEMR (nothing selected, an Observation that is not
  `final`, or an incomplete Patient, for example "Patient incomplete: missing birthDate"): `422`
  with an OperationOutcome (`issue.code` `required` or `invalid`, `diagnostics` naming what is
  missing). The client throws `ApiError`; there are no steps;
- OpenEMR unreachable before the first step: `502` with an OperationOutcome;
- failed during the OpenEMR sequence: `200` with `outcome: failed` or `partly-shared` and the steps
  so far, because the trace of what happened is the point of the panel; a repeated reading
  (OpenEMR's "already exists" 400) is a step with `result: already-shared`, not a failure.

**Messages log rows.** The proxy reads send-attempt AuditEvents from the **audit FHIR store**
(D15, separate from the health-data store) and turns each into a row with T31's
`parse_send_attempt`:

- `cuff-send`: the fields of T31's `SendAttemptRecord` (`sentAt` = period start, `recordedAt`,
  `transport`, `controlId` = MSH-10, `result` accepted / rejected / no-acknowledgment, `outcome`
  0 / 4 / 8, `commitAck` with the MSA-1 code and cleaned MSA-3 reason or the no-acknowledgment
  reason, `messageName`, `sample`), plus `application {outcome: accepted | error | pending,
reason}` joined from the stored message's labels where the send was accepted, and `observation`.
  `commitAck.raw` is the `ack-message` detail **decoded** to text as a convenience; the AuditEvent
  in the trace stays exactly as stored (base64). A stored message with no AuditEvent is a row with
  `source: store-only`.
- `unreadable-send-attempt`: an AuditEvent that is ours but unreadable (T31's
  `UnreadableSendAttempt`): `{id, recordedAt, problem}`. T31 tells the proxy to surface these, never
  hide them, so the page shows them as error rows, and `unreadable` counts them.
- `fhir-transaction`: `{id, recordedAt, writer, httpStatus, resources}`.

Two terms used in the shapes (D7): the **commit-level acknowledgment** is the HL7v2 ACK the store
returned (MSA-1 `AA` stored, `AE` rejected, with MSA-3 as the reason), or no ACK at all; the
**application-level outcome** is what `v2-to-fhir` made of a stored message, from its
`application_outcome` and `reason` labels, or `pending` when it has no label yet.

### What T14, T18 and T19 must know (contract changes made by T34)

T34 brought the contract in line with the services T13 (cuff-sim) and T10 (health-sync) built.
Code written against the old shapes no longer type-checks, on purpose.

| Type or value                               | Before (T16)                                                                    | After (T34)                                                                                                                                                                                                                                                                                            | Who must follow |
| ------------------------------------------- | ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------- |
| `CuffReadingRequest`                        | `{systolic, diastolic, transport, sendMalformed: string \| null}`, all required | a union: built reading `{systolic, diastolic, transport, sendMalformed: null}`, or malformed `{systolic?, diastolic?, transport, sendMalformed: string}`                                                                                                                                               | T18, T14        |
| `CUFF_BOUNDS` (60-250 / 30-150)             | presented as the bounds                                                         | **removed**. `CUFF_SERVICE_LIMITS` = cuff-sim's 1 to 400 mm[Hg], systolic above diastolic; `MAPPER_PLAUSIBLE_RANGE` = T12's 50-300 / 20-200 (information only)                                                                                                                                         | T18             |
| `CuffSendResult.attemptId`                  | `string`                                                                        | `string \| null` (null: the AuditEvent could not be built; no trace, not in the log)                                                                                                                                                                                                                   | T18             |
| `CuffSendResult` new fields                 |                                                                                 | `result` (`accepted` \| `rejected` \| `no-acknowledgment`), `framing` (`MllpFraming \| null`), `messageNameSource` (`ingest-response` \| `derived-sha256-unverified` \| null)                                                                                                                          | T18, T14        |
| `CuffSendResult.messageName`                | HTTPS only; null over MLLP                                                      | also set over MLLP for an accepted send, but **derived**: show it labelled as derived and unconfirmed (`isDerivedMessageName(result)`), never as the stored message's name                                                                                                                             | T18             |
| Cuff refusals                               | `ApiError` 422 / 429                                                            | `CuffSendError` with `reason` for 400, 413, 422, 429, 500, 504; `fields`; `retryAfterSeconds`                                                                                                                                                                                                          | T18             |
| `MalformedSample`                           | `{name, label, class, transports}`                                              | unchanged, now documented as cuff-sim's list (`label` = the manifest's description)                                                                                                                                                                                                                    | T18             |
| `StatusSummary.watchSync.state`             | `ok` \| `reconnect-needed` \| `never-synced` \| `error`                         | health-sync's `connected` \| `reconnect-needed` \| `not-configured` \| `unknown`; "never synced" is `lastSyncAt === null`                                                                                                                                                                              | T14, T17 (done) |
| `StatusSummary.watchSync` other fields      | `lastSyncAt`, `detail`                                                          | `reason`, `notes`, `strandedPreliminary` (number \| null), `checkedAt`, `lastSyncAt` (`detail` removed)                                                                                                                                                                                                | T14             |
| `StatusSummary.watchSync.lastSyncAt`        | "last sync"                                                                     | the time of the newest heart-rate write (does not move while the watch is off); the strip says "Newest watch data"                                                                                                                                                                                     | T14             |
| `HeartRateTrace.source`                     | `GoogleHealthRollUpWindow \| null`                                              | `{kind: 'reconstructed' \| 'original' \| 'unknown', window} \| null`; on the wire `{kind, window}`                                                                                                                                                                                                     | T14, T17 (done) |
| `SendAttemptEntry.derivedMessageName` (T33) |                                                                                 | new: the name cuff-sim derived for an accepted MLLP send (the AuditEvent's `derived-message-name` detail). `messageName` stays the store-returned name only. The proxy joins the attempt to its stored message by the derived name and confirms it with a `get`; the page shows it labelled as derived | T14, T19        |
| Sample bytes (T33)                          | T07's patient `SYN-000123`                                                      | cuff-sim substitutes the configured patient (`with_patient`) except in the seven samples T07's samples README lists; a derived name is over the bytes actually sent                                                                                                                                    | T14, T18        |
| `Observation.performer` (T33)               |                                                                                 | the Patient, as `subject`; a share drops it                                                                                                                                                                                                                                                            | T15, T19        |
| `OperationOutcome.issue`                    |                                                                                 | `expression?: string[]` (cuff-sim names the field there)                                                                                                                                                                                                                                               | T18             |
| `'sync'` data change                        | "anyone who learns of a new sync (none today)"                                  | announced by the status strip; features only listen                                                                                                                                                                                                                                                    | T17 (done), T19 |
| Shell element ids                           | `cuff-heading`, `share-heading`, `messages-log`, `tabs-N`, ...                  | `shell-...` and `ui-...`; features use their own prefix (see "Element ids")                                                                                                                                                                                                                            | T18, T19        |

For T18 in short: offer inputs inside `CUFF_SERVICE_LIMITS` (a narrower form range is fine, but
do not call it the service's); let a malformed send leave the values out; show `result`, the
commit-level ACK and, once mapped, the application-level outcome; label a derived message name as
derived; handle `attemptId: null`; on `CuffSendError` mark `error.fields` and, for
`rate-limited`, say when to retry from `retryAfterSeconds`; and expect 429 after three quick sends
in mock mode too.

**Changes made in the T18 review round** (orchestrator-approved edits to shell files):

| What                                                                                                                                                                                                                                                                                                                                                 | Where                 | For                                     |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------- | --------------------------------------- |
| The mapper's reason sentences are one shared module, `reasonText(code)` and `REASON_SENTENCES` (moved from `src/cuff/`). Its test checks every code and sentence against `services/v2-to-fhir/v2_to_fhir/reasons.py`, and `MAPPER_PLAUSIBLE_RANGE` against `SYSTOLIC_RANGE`/`DIASTOLIC_RANGE` in `libs/pulsebridge_fhir/pulsebridge_fhir/inputs.py`. | `src/api/reasons.ts`  | T19 (show reason codes as text with it) |
| A 200 from `POST /cuff/readings` whose `ack`, `audit`, `transport`, `sentAt` or `messageSent` cannot be read throws `ApiError(502)` instead of returning a half-read result.                                                                                                                                                                         | `src/api/parse.ts`    | T14                                     |
| The share hint shows a selection's `unshareableReason` before the generic "no Observation was stored".                                                                                                                                                                                                                                               | `src/share/index.ts`  | T19                                     |
| The tab list wraps instead of scrolling, so no tab is cut off on a phone.                                                                                                                                                                                                                                                                            | `src/styles/base.css` | all                                     |
| `?mock-data=hostile` and `?mock-audit=build\|write` (above).                                                                                                                                                                                                                                                                                         | `src/api/`            | T19 browser tests                       |

For T19, from the cuff side:

- **Selecting a cuff row from the messages log works.** If T19 sets the shared selection to an
  attempt that is in T18's readings list (this session's sends), the cuff panel highlights it,
  loads its trace, marks the selection "being checked against its stored trace." while that
  loads, and then re-sets the selection from its own data (summary, Observation, reason). An
  attempt not sent in this session has no row there, so the cuff trace stays empty.
- **The two acknowledgment levels need the same explanation in the log.** The store answers in
  HL7's original acknowledgment mode (AA or AE in MSA-1); "commit-level acknowledgment" and
  "application-level outcome" are PulseBridge's names, after HL7's enhanced mode (MSH-15/16),
  where those would be CA/CE/CR and AA/AE/AR. Without that sentence a reader who knows HL7 sees AA
  under a "commit-level" heading and thinks the levels are confused. The cuff side shows it in
  the ACK tab and in short above its readings list (`ACK_MODE_NOTE`, `ACK_MODE_SHORT` in
  `src/cuff/readings.ts`); T19 should do the same in the log, and must not call AA "application
  accept" under the commit-level label.

For T19 in short: an attempt whose AuditEvent was not recorded is not in the log; the log row's
`messageName` is only ever a name the store returned (null over MLLP), and an accepted MLLP row
has `derivedMessageName` instead: show it labelled as derived and unconfirmed; prefix ids with
`share-` and `log-`.

The minimal edits T34 made in `src/heart-rate/` to keep T17's code compiling and honest against
the new types: `trace.ts` takes the source tab's note and code-block label from `source.kind`
(and shows `source.window`); `trace.test.ts` uses the new shape and adds a test for `original` and
`unknown`; `panel.ts` reads "has not synced yet" from `lastSyncAt === null` instead of the removed
`never-synced` state; `panel.test.ts`'s status helper builds the new `watchSync`.

### Open questions for T14, T15 and the orchestrator

1. **Patient parameter.** The page sends no `patient` search parameter: this is a single-patient
   PHR, and the proposal is that the proxy pins searches to the configured patient (T14's AC says
   "search by patient"). Alternative: `/status` returns the Patient reference and the page passes it.
2. **Source JSON for the heart-rate trace.** Settled in shape: the proxy sends
   `source: {kind, window}`. T10 does not store the raw roll-up, so today the proxy rebuilds the
   window from the stored Observation (period, average, minimum, maximum) and sends
   `kind: "reconstructed"`. If a later task stores the original (for example as a `Binary`
   referenced from the Provenance), the proxy sends `kind: "original"` and the page needs no change.
3. **"HL7v2 as sent".** T31 never stores the message. For a malformed send the proxy can take the
   bytes from the T07 sample named in `malformed-sample`. For a valid send it must fetch the stored
   message (`view=RAW_ONLY`) by name. Over MLLP the AuditEvent carries cuff-sim's derived name
   (`derived-message-name`, T33: `<store>/messages/<urlsafe-base64(sha256(bytes sent))>`), which
   is unverified, so the proxy joins by it, confirms it with a `get`, and sends it as
   `derivedMessageName`, never as `messageName`. A control ID is not a key (samples share them).
4. **FHIR transaction rows.** The brief's log lists one row per FHIR transaction (Bundle, HTTP
   status, resource ids), but no task records them. Proposal: derive them from the `Provenance`
   each ingest Bundle writes (`recorded`, `target`); the HTTP status would then be unknown (`null`).
5. **Date rule.** Confirm that the Cloud Healthcare FHIR store applies `gt`/`lt` to
   `effectivePeriod` as R4 defines (overlap), so the proxy can pass the parameters through.
6. **Share failures and the CapabilityStatement route** (above) are proposals for T15 and T14.
7. **Forwarding cuff-sim's refusals.** The page maps 400, 413, 422, 429 (with `Retry-After`) and
   500 from cuff-sim, and 504 for a cut-off request. T14 should forward cuff-sim's status, body and
   `Retry-After` unchanged, keep its own forward timeout at least 90 s (cuff-sim's README), and
   answer 502 (not 500) when cuff-sim cannot be reached, so the page does not report a cuff-sim bug.
   Hosting ends a rewritten request at 60 s (documented); the proxy answers within 55 s.
8. **`/status` from health-sync.** health-sync answers 200 in every state. T14 should send
   `googleHealth` as `state` (the client also accepts `googleHealth` itself when `state` is absent),
   and `reason`, `notes`, `strandedPreliminary` and `checkedAt` unchanged,
   and report health-sync itself not answering (any non-200 or no answer) as `state: "unknown"`
   with a `reason` such as `health-sync-unreachable`, not as a disconnection.
9. **`lastSyncAt`.** health-sync's `/status` is stateless and has no last sync time. The page
   needs the time of the newest heart-rate write (the strip shows it as "Newest watch data", and a
   change announces `'sync'`). Proposal: the proxy reads it from the FHIR store, for example the
   newest `Provenance.recorded` whose agent is health-sync, or the newest heart-rate Observation's
   `meta.lastUpdated`, with a short cache. It moves only when data was written, which is what the
   page wants: otherwise the chart would re-fetch for nothing.
10. **Store-only rows.** A send whose AuditEvent was not recorded leaves a stored message without
    an AuditEvent. The contract has `source: "store-only"` rows for these; the mock does not make
    any. T14 decides whether to list them.
