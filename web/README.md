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

T19 added four more (all parsed by `mockSwitchesFromSearch` in `src/api/mock-data.ts`; each switch
name is a marker in `scripts/bundle-rules.mjs` `MOCK_MARKERS`, so an HTTP build that contains one
fails the bundle check):

- `?mock-lookup=not-found|failed`: the HL7v2 store does not have (or cannot be asked for) the
  name of any accepted send (`messageLookup`); with `not-found` an MLLP message nobody claims is
  a store-only row with a `possibleDuplicateOf` hint.
- `?mock-log=partial`: every messages-log page says `partial: true`.
- `?mock-status=openemr-not-checked|counts-unknown|health-sync-timeout|not-configured`: the
  status (or, for `not-configured`, a 503 `login-not-configured` on every route but login) the
  proxy sends in that state.
- `?mock-share=patient-incomplete|not-configured|openemr-unreachable|encounter-refused|observation-timeout|read-back|hosting-timeout`:
  where a share is refused or stops.

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

| Path                                                                                                                                                                 | Owner                                                                                                                                     | Rule for the others                                                                               |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `src/heart-rate/`                                                                                                                                                    | T17 (T34 made the edits a contract change needed, listed below)                                                                           | do not edit                                                                                       |
| `src/cuff/`                                                                                                                                                          | T18                                                                                                                                       | do not edit                                                                                       |
| `src/share/`, `src/messages/`                                                                                                                                        | T19 (which, to follow T14's proxy, also edited `src/api/`, this README and parts of `src/cuff/` and `src/shell/`: see "What T19 changed") | do not edit                                                                                       |
| `package.json`, `package-lock.json`                                                                                                                                  | T17, **only** to add the chart library (D5)                                                                                               | T18 and T19 add no dependencies                                                                   |
| `src/api/` (contract, wire parsing `parse.ts`, errors, HTTP client, mock client, mock data, T31 example copies)                                                      | the shell (T16, then T34)                                                                                                                 | a task that needs a contract or mock change **reports it to the orchestrator** instead of editing |
| `src/shell/`, `src/ui/`, `src/state/`, `src/styles/`, `e2e/` (shell and mock-data browser tests), configs, `scripts/` (bundle check and its rules, mock-sample sync) | the shell (T16, then T34)                                                                                                                 | same: report, do not edit                                                                         |

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

- **The share's failures are typed.** `share` rejects with `ShareError` (`src/api/errors.ts`):
  `reason` is the proxy's code (`not-found`, `observation-not-final`, `patient-incomplete`,
  `openemr-unreachable`, `openemr-refused`, `openemr-timeout`, `share-not-configured`, ...),
  and `steps` the trace steps completed before it stopped (empty when refused before OpenEMR).
- **A 503 on every route** (`isServiceNotConfigured(error)`): the proxy's password secret is
  missing, so no token can be checked (amendment 11). It is not a 401: the session stays, and
  the page says the service is not configured.
- **Final trace answers** (`isFinalTraceAnswer(error)`): 404, 410 and 422 (an unreadable
  AuditEvent, amendment 12). Retrying cannot change them.
- `proxyReason(outcome)` reads the proxy's reason code (`issue[0].details.coding`, system
  `urn:pulsebridge:fhir-proxy:reason`).

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

| Kind          | Who calls it                                                                                  | Who listens                                                       |
| ------------- | --------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| `'cuff-send'` | T18, after `submitCuffReading` returns (whatever the ACK)                                     | T19 messages log; the status strip (built in)                     |
| `'share'`     | T19, after `share` returns, and after a failure that reached OpenEMR                          | T19 messages log; T18 (re-checks its selection); the status strip |
| `'sync'`      | the status strip (built in), when the newest watch data time changes between two status loads | T17 chart; T19 messages log                                       |

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
`getCuffAttemptTrace` for the reading it just sent; T19 polls `listMessages` while a row it shows
is pending and the strip is open. Nobody else polls. A row whose stored message the proxy could
not find or check (`messageLookup` `not-found` or `failed`) is not pending: both stop polling
for it and say so.

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
  is a 404). The send result still says what the store answered, and the attempt is not in the log
  as an attempt; its stored message is listed as a `store-only` row, as the proxy lists it.
- **The messages log is assembled as the proxy assembles it** (T19; services/fhir-proxy
  `messages.py`): every attempt row has `messageLookup` (one seeded accepted MLLP attempt has a
  wrong derived name: `not-found`, no outcome, no Observation); store-only rows (that attempt's
  real message, with `possibleDuplicateOf` pointing at it, and a message sent straight to the
  adapter, with no hint and transport `null`); two unreadable rows (one with id `""` and no
  time, sorted last); transaction rows from health-sync, v2-to-fhir (one per mapped cuff
  message), the seeder and an unknown writer, `httpStatus` always null, resources only
  Observations and Provenances. Order is newest first by send time (store-only rows by
  createTime), ties by id. Pages are 25 rows by default, at most 50, cut BEFORE the kind and
  outcome filters, so a filtered page can be short or empty while `nextCursor` is set; the
  outcome filter matches only rows with that outcome. The cursor is opaque and a foreign one is
  a 400 `invalid-cursor`. A page holding a `failed` lookup is `partial`.
- **Refusals are the proxy's**: a refused search is 400 `not-supported`; the trace of an
  unreadable AuditEvent is 422; a store-only name as a trace id is 404 `unknown-route` (its slashes
  match no route), another id that is not a FHIR id 400 `invalid-id`; with
  `?mock-status=not-configured` every route but login is 503 `login-not-configured`. Each error
  carries an OperationOutcome with the proxy's reason code.
- **Status**: `lastSyncAt` in UTC with milliseconds; `observationCounts` null when not known
  (switch); OpenEMR reachable (as with T15's probe) or `not-checked` (switch, the proxy before T15).
- **The mock reuses its access token** for 240 s, as the proxy does: a second share in that
  time shows a `token` step with `result: "reused"`. A heart-rate window is sent at its start
  (`effectiveDateTime`), as the proxy sends it.
- **The share walks T15's specified sequence** (see "For T15: the share route"): token, Patient
  search then create only if absent, Encounter search for the reading's UTC day then create only
  if none, the Observation POST (a repeat in the same Encounter gets OpenEMR's "already exists"
  400 and is reported as `already-shared`), then the three reads by id. Search parameters are in
  `request.query`. Trace steps are redacted with the final rule (the token step's credentials
  hidden, FHIR codes visible). `?mock-share=hosting-timeout` does the whole share and then answers
  as the HTTP client reads Firebase Hosting's HTML 504 (no steps, no OperationOutcome). The mock OpenEMR's read-back differs from what was sent the way
  spike S1 saw (its own `id` and `meta.profile`, `type` on references, rewritten displays and
  unit texts, times in `+00:00`), and for a heart rate it keeps no components or period: that last
  part is this mock's assumption, not measured.
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

For comparison, our own code is the main chunk, 128,483 B (41.80 kB gzip) after T19, and the CSS
is 24,678 B (5.02 kB gzip) including uPlot's. **uPlot must stay out of the main chunk**, so the login form
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

## HTTP contract (as built by T14; the share route specified for T15)

`fhir-proxy` (T14) is the authority: its README's "Routes as built" and "Contract amendments"
(services/fhir-proxy/README.md). T19 brought the contract, the client, the parser and the mock
in line with it (table "What T19 changed" below). The TypeScript types in `src/api/contract.ts`
are the parsed shapes. Paths are relative to the base URL (default `/api`).

**Authentication.** Every route except `POST /auth/login` requires `Authorization: Bearer <token>`
and answers `401` when it is missing, expired, altered or signed under a previous password,
including `GET /fhir/metadata` and unknown paths. When the proxy's password secret is missing,
EVERY route answers `503` `login-not-configured`. Every error body is an `OperationOutcome` with
a fixed `diagnostics` sentence and a reason code in `issue[0].details.coding[0]` (system
`urn:pulsebridge:fhir-proxy:reason`); the client shows the sentence.

| Method and path                                    | Client method          | Answer (as built)                                                                                                                                                                                                                                                                                                                                                                               |
| -------------------------------------------------- | ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /auth/login`                                 | `login`                | `{"password"}` → `200 {"token", "expires_at"}`; `401`; `429` + `Retry-After`; `503` secret missing; `400`/`413`/`415` a bad body                                                                                                                                                                                                                                                                |
| `GET /status`                                      | `getStatus`            | always `200`: `watchSync` (`state`, `reason` (health-sync's, or the proxy's `health-sync-*` codes), `notes`, `strandedPreliminary`, `checkedAt`, `lastSyncAt` in UTC with milliseconds), `observationCounts {watch, cuff}` each a number or `null` ("not known", never 0), `fhirStore` and `openemr` `{reachable, checkedAt, detail}` (`detail` a short code; OpenEMR `not-checked` before T15) |
| `GET /fhir/Observation?code=&date=&_sort=&_count=` | `searchObservations`   | `searchset` Bundle pinned to the configured patient; `link` `self` and `next` path-absolute; every entry has `fullUrl` (`PUBLIC_BASE_URL/fhir/Observation/<id>`); no `total`; `400` (`not-supported`/`invalid`) for anything unsupported                                                                                                                                                        |
| `GET /fhir/Observation/{id}`                       | `getObservation`       | the Observation if it is the configured patient's, else `404`; any query parameter `400`; `ETag`, `Last-Modified`. The share area reads the selected reading with it                                                                                                                                                                                                                            |
| `GET /trace/heart-rate/{observationId}`            | `getHeartRateTrace`    | `{"resources": Bundle (Observation, Provenance?, Device?), "source": {"kind": "reconstructed", "window"} \| null}`; `404`                                                                                                                                                                                                                                                                       |
| `GET /trace/cuff-attempts/{attemptId}`             | `getCuffAttemptTrace`  | `{"attempt": SendAttemptEntry (with messageLookup), "messageSent": ER7 \| null, "resources": Bundle (AuditEvent as stored, Observation?, Provenance?)}`; `404`; `422` the AuditEvent is unreadable (final); a store-only message name (it has slashes) matches no route: `404` `unknown-route`; another id that is not a FHIR id `400` `invalid-id`                                             |
| `GET /messages?kind=&outcome=&count=&cursor=`      | `listMessages`         | `{"entries", "nextCursor", "unreadable", "partial"}`, newest first by send time; `count` default 25, cap 50; filters applied after the join (short or empty pages while `nextCursor` is set); `400` an unknown parameter or value, or `invalid-cursor`                                                                                                                                          |
| `GET /cuff/samples`                                | `listMalformedSamples` | cuff-sim's list, forwarded                                                                                                                                                                                                                                                                                                                                                                      |
| `POST /cuff/readings`                              | `submitCuffReading`    | forwarded to cuff-sim; its `200/400/413/422/429/500`, body and `Retry-After` unchanged; `502` cuff-sim unreachable or unusable; `504` timed out (may have been sent)                                                                                                                                                                                                                            |
| `POST /share`                                      | `share`                | **not built yet (T15)**: specified below                                                                                                                                                                                                                                                                                                                                                        |

Field names in the JSON views are camelCase, except the login response (`token`, `expires_at`).

### What T19 changed to follow the proxy (the 21 amendments)

| #   | Amendment                    | Dashboard before                               | After (T19)                                                                                                                                                                         | Files                                                                     |
| --- | ---------------------------- | ---------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| 1   | `messageLookup`              | absent; an unconfirmed row waited as "pending" | on every row; only `confirmed` carries an outcome and an Observation (the parser enforces it); the cuff panel and the log say "not found" / "could not be checked" and stop polling | contract, parse, mock, cuff `readings.ts`/`panel.ts`/`trace.ts`, messages |
| 2   | `writer` seed/unknown        | two writers                                    | four, worded in the log                                                                                                                                                             | contract, parse, mock, messages                                           |
| 3   | count 25 / cap 50            | mock 50 / 100                                  | `MESSAGES_DEFAULT_COUNT`, `MESSAGES_MAX_COUNT`; mock follows; the log asks for 25                                                                                                   | contract, mock, messages                                                  |
| 4   | short filtered pages         | mock filtered a full list                      | mock cuts the page before filtering; the log follows `nextCursor` (at most 4 pages a load), then offers "Load more"                                                                 | mock, messages `paging.ts`                                                |
| 5   | store-only rows              | not produced                                   | produced by the mock; shown as "Stored message, no send record", never selectable                                                                                                   | contract, parse, mock, messages                                           |
| 6   | unreadable id `""`           | string                                         | `""` allowed; never used as an element id (generated ids only)                                                                                                                      | contract, parse, mock, messages                                           |
| 7   | search refusal 400           | mock 422                                       | mock 400 `not-supported`                                                                                                                                                            | mock                                                                      |
| 8   | Bundle links and `fullUrl`   | relative `next`                                | unchanged client (it resolves path-absolute links inside the base); a test with the proxy's literal links                                                                           | tests                                                                     |
| 9   | `watchSync.reason` codes     | shown as the code                              | the strip words the proxy's `health-sync-*` codes, with the code                                                                                                                    | contract `HEALTH_SYNC_FAILURE_REASONS`, shell `status-strip.ts`           |
| 10  | OpenEMR `not-checked`        | shown as "Unreachable"                         | "Not checked", neutral                                                                                                                                                              | contract `REACHABILITY_NOT_CHECKED`, shell `status-strip.ts`              |
| 11  | 503 on every route           | a generic error                                | `isServiceNotConfigured`; the strip says "Service: Not configured", the log and share say so; the session stays                                                                     | errors, shell `app.ts`/`status-strip.ts`, messages, share                 |
| 12  | trace 422 final              | only 404/410 final                             | `isFinalTraceAnswer` (404, 410, 422); the cuff trace says the record could not be read                                                                                              | errors, cuff `panel.ts`/`trace.ts`, mock                                  |
| 13  | `lastSyncAt` milliseconds    | ISO                                            | unchanged code (compared as instants); tests with milliseconds                                                                                                                      | tests                                                                     |
| 14  | `possibleDuplicateOf`        | absent                                         | parsed (store-only rows only); a hint linking to the attempt row, never a confirmed link                                                                                            | contract, parse, mock, messages                                           |
| 15  | nullable counts              | `count()` made null 0                          | null kept; the strip says "unknown"                                                                                                                                                 | contract, parse, shell `status-strip.ts`                                  |
| 16  | `partial`                    | absent                                         | parsed (anything but `false` is true; a dropped row makes it true); the log says the list may be incomplete and offers a reload                                                     | contract, parse, mock, messages                                           |
| 17  | order by send time           | mock by `recordedAt`                           | mock by send time (store-only by createTime), ties by id; `sortTime` in the log                                                                                                     | mock, messages                                                            |
| 18  | transactions pinned          | mock listed Patient/Device                     | mock lists only Observations and Provenances, `httpStatus` null                                                                                                                     | mock                                                                      |
| 19  | `outcome` filter             | matched `application.outcome`                  | only rows WITH that outcome; the log's filter note says rows with none match no filter                                                                                              | contract, mock, messages                                                  |
| 20  | `GET /fhir/Observation/{id}` | absent                                         | `getObservation`; the share's check before confirming                                                                                                                               | contract, http-client, mock, share                                        |
| 21  | cursor 400                   | opaque                                         | the mock's cursor is opaque too and a foreign one is 400 `invalid-cursor`                                                                                                           | mock                                                                      |

### For T15: the share route

This is the FINAL contract for `POST /share` (agreed after the T19 review; T15 implements it and
adjusts T14's trace helper to the redaction rule below). `src/api/contract.ts` (`ShareRequest`,
`ShareStep`, `ShareQuery`, `ShareResult`, `ShareFailureReason`, `SHARE_TOKEN_STEP_SECRETS`,
`SHARE_FHIR_STEP_SECRETS`) and `src/api/parse.ts` (`parseShareResult`) are the normative shapes;
`src/api/http-client-proxy.test.ts` holds literal bodies, and the mock implements it. It follows
the proxy's conventions: the route is registered through `extensions.add_route` (token-gated, in
the CapabilityStatement), refusals are `Problem`s (OperationOutcome with a reason code), every
step is built with `extensions.trace_step`, and nothing secret or host-shaped reaches the page.

**Request.** `{"observationId": "<FHIR id>"}`, exactly that one member (a FHIR id, the proxy's
`SAFE_ID`), at most a few hundred bytes (read with `bodies.read_body`). Never a patient, a
reference to another type or a URL: the proxy pins the Observation to the configured patient
(`stores.read`, `stores.is_ours`) and builds everything it sends from its own store. There is no
idempotency key: shares are serialised per proxy instance, and a repeat is handled by the
sequence itself (below).

**Steps.** Each step is `{step, label, request: {method, url, query, body}, response: {status,
body}, result, durationMs}`:

- `request.url` is a PATH ONLY: no scheme, host or query string;
- `request.query` holds a search's parameters as an object, parameter name to one value or a list
  of values (`{"identifier": "<value>"}`, `{"patient": "<uuid>", "date": "2026-03-10"}`), or
  `null`; the page shows it as "Search parameters: …";
- `request.body` is only a real request body (`null` for a GET).

| `step`                                               | Request                                                                                                                       | `result`                                                                                                      |
| ---------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `token`                                              | `POST /oauth2/default/token` (client credentials, signed assertion)                                                           | `ok`, `reused` (a token still valid in the proxy's memory: no request sent, status 0, no bodies), or `failed` |
| `patient-search`                                     | `GET /apis/default/fhir/Patient`, query `{"identifier": ...}`                                                                 | `found` or `not-found`                                                                                        |
| `patient-create`                                     | only after `not-found`: `POST /apis/default/fhir/Patient` with `patient_for_sharing(stored)`                                  | `ok` or `failed`                                                                                              |
| `encounter-search`                                   | `GET /apis/default/fhir/Encounter`, query `{"patient": <uuid>, "date": "<YYYY-MM-DD>"}`                                       | `found` (reused) or `not-found`                                                                               |
| `encounter-create`                                   | only after `not-found`: `POST /apis/default/fhir/Encounter`                                                                   | `ok` or `failed`                                                                                              |
| `observation-create`                                 | `POST /apis/default/fhir/Observation` (subject and encounter OpenEMR's; no `performer`, `device`, `meta`, `identifier`, `id`) | `ok` (201), `already-shared` (OpenEMR's "already exists" 400), or `failed`                                    |
| `patient-read`, `encounter-read`, `observation-read` | `GET /apis/default/fhir/<Type>/<id>`                                                                                          | `ok` or `failed`                                                                                              |

**Redaction (final rule).** Values are replaced by `"[not shown]"`, at any depth, comparing key
names without regard to case:

- in the `token` step's request and response bodies: every OAuth 2.0 / JWT-bearer credential
  parameter, `authorization`, `client_secret`, `client_assertion`, `assertion`, `password`,
  `token`, `access_token`, `refresh_token`, `id_token`, `code`, `code_verifier`, `private_key`,
  `registration_access_token`;
- in every other step (FHIR bodies): only names that cannot collide with a FHIR element,
  `access_token`, `refresh_token`, `id_token`, `client_assertion`, `client_secret`, `assertion`,
  `private_key`, `password`, `authorization`, `registration_access_token`.

So FHIR codes (`Observation.code`, `Coding.code`, `issue.code`) stay visible in the trace. (Today
T14's helper hides every `code` and `token` key; T15 changes it to this rule.) The page's note
under the trace says only "Credentials are replaced by "[not shown]"."

**As built by T15 (accepted differences from the first version of this contract).**

- A cached token is a `token` step with `result: "reused"` (no request, no bodies, status 0);
  the page says "Token reused from the server's memory: no request sent".
- A heart-rate window is sent with `effectiveDateTime` = its `effectivePeriod.start` (offset
  kept) and no `effectivePeriod`: OpenEMR 8.5.0's vitals write reads only `effectiveDateTime`.
  OpenEMR keeps the average, not the min and max components. From the 8.5.0 source; not verified
  live.
- A create counts only as a 201 with a valid id. Any other 2xx is a `failed` step with that
  status and a 502 envelope ("not known whether it was created"); for `observation-create` the
  page then says it is not known whether the reading was stored. A 4xx there means refused, not
  stored; anything else (5xx, 2xx) means not known.
- `already-shared` only for OpenEMR's own phrase with the code that was sent; an id is read back
  only when exactly one is named, and only shown if it is this reading (our Patient, Encounter
  and code).
- A request already sent when the proxy failed is still a step (status 0, `failed`, "whether it
  took effect is not known"); a request never sent (the budget ran out) is not a step.
- A shared reading that could not be read back is worded "OpenEMR answered that it stored the
  reading (HTTP 201), but it could not be read back to confirm", never "stored" as confirmed.
- New reasons: `share-busy` (503), `share-internal-error` (502), `share-blocked` (500).
- Credentials OpenEMR echoes inside a message (the assertion in an OAuth error, the access token
  in a FHIR error) are replaced by `[not shown]` too, not only values under credential key names.

**Encounter day rule.** The Encounter is reused per patient and UTC calendar day of the reading
(`effectiveDateTime`, or `effectivePeriod.start` for a heart-rate window), found with
`Encounter?patient=<id>&date=<YYYY-MM-DD>` (the `encounter-search` step's query). If several are
found, the oldest is reused and the step's label says so; if none, one is created for that day.

**Repeats.** A repeat never creates a second Patient (the Patient is searched for before it is
created) and reuses that day's Encounter. OpenEMR refuses a repeated reading within the same
Encounter (same code, encounter and time) with its "already exists" 400, which names the
existing Observation; the step's `result` is then `already-shared`, `observation-read` reads
that Observation, and the answer is a 200 `already-shared`. Spike S1 saw that refusal for a
blood-pressure reading only; for a heart rate (sent at its window's start) it is not verified, and
the page says so. T15 handles `already-shared` for both.

**200** (the reading is in OpenEMR):

```json
{
  "outcome": "shared",
  "steps": [
    {
      "step": "patient-search",
      "label": "Search Patient by identifier",
      "request": {
        "method": "GET",
        "url": "/apis/default/fhir/Patient",
        "query": { "identifier": "<PT identifier>" },
        "body": null
      },
      "response": {
        "status": 200,
        "body": { "resourceType": "Bundle", "type": "searchset", "total": 1 }
      },
      "result": "found",
      "durationMs": 120
    }
  ],
  "readBack": {
    "patient": { "sent": null, "stored": { "resourceType": "Patient", "id": "..." } },
    "encounter": {
      "sent": { "resourceType": "Encounter" },
      "stored": { "resourceType": "Encounter", "id": "..." }
    },
    "observation": {
      "sent": { "resourceType": "Observation" },
      "stored": { "resourceType": "Observation", "id": "..." }
    }
  }
}
```

- `outcome`: `shared` or `already-shared`; anything else (and a 200 body the page cannot read) is
  read as an unreadable answer, so never send a 200 for a share that did not end with the reading
  in OpenEMR.
- `readBack.<x>.sent`: the resource exactly as sent (no credential in it, not redacted), or
  `null` when nothing was sent (the Patient was found; the Encounter reused). `stored`: the
  resource read back by id, or `null` when that read failed (its step is `failed`; the outcome is
  still `shared`). The page shows the two side by side with the differences in words (D13).

**Errors.** Every error body is an OperationOutcome built by `Problem` (reason code in
`issue[0].details.coding[0]`), with ONE documented exception, the 502/504 envelope:

| Status | Reason                                                                     | When                                                                                                                                                                         | Body                                                                                                                                                        |
| ------ | -------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 400    | `invalid-body`                                                             | not exactly `{"observationId": <FHIR id>}`                                                                                                                                   | OperationOutcome                                                                                                                                            |
| 404    | `not-found`                                                                | no such Observation, or another patient's (answered alike)                                                                                                                   | OperationOutcome                                                                                                                                            |
| 422    | `observation-not-final`, `observation-not-shareable`, `patient-incomplete` | before OpenEMR is called: not `final` (for example `amended`); not a heart rate or blood-pressure panel; the stored Patient incomplete (`diagnostics` names what is missing) | OperationOutcome                                                                                                                                            |
| 503    | `share-not-configured`                                                     | T15's variables or key are missing                                                                                                                                           | OperationOutcome                                                                                                                                            |
| 503    | `share-busy`                                                               | another share was in progress on this proxy instance and did not finish in time (`Retry-After: 5`); nothing was sent                                                         | OperationOutcome                                                                                                                                            |
| 500    | `share-blocked`                                                            | the proxy's leak guard found a PulseBridge-internal value in what it was about to send; nothing was sent (a server fault)                                                    | OperationOutcome                                                                                                                                            |
| 502    | `openemr-unreachable`, `openemr-refused`, `share-internal-error`           | OpenEMR could not be reached, refused a step or answered in a way the proxy could not understand; or the proxy itself failed after OpenEMR was called                        | envelope, `Content-Type: application/json`: `{"operationOutcome": <exactly as Problem builds it>, "steps": [steps completed so far, the failing one last]}` |
| 504    | `openemr-timeout`                                                          | OpenEMR did not answer within the request budget                                                                                                                             | the same envelope; a step that got no answer has `response.status` 0                                                                                        |

Why the envelope: an OperationOutcome cannot carry the steps without members FHIR does not
define, and the steps are what tells the user what did and did not happen.

**What the page concludes from a failure.** Only a 400, 404, 422 or 503 whose body is the proxy's
OperationOutcome, with no steps, means the share was refused before OpenEMR was called, and only
then does the page say "Nothing was sent to the clinic" (`ShareError.nothingSent`). Everything
else may come after the proxy had already created the Patient, the Encounter or the reading at
the clinic, so the page says it is not known what reached the clinic, that sharing again is safe
(it finds and reuses what exists), and announces `'share'` so the others re-check: no answer at
all (status 0), any 5xx without the envelope (a bare OperationOutcome when the proxy's budget
runs out, a 500), a non-JSON body (Firebase Hosting's own HTML 504 at its 60 s limit), and a 200
the page cannot read. With the envelope it says, step by step, what was done.

### Wire shape and parsed shape

The HTTP client does not hand the proxy's JSON (the **wire** shape) straight to the features. It
converts it into the types in `src/api/contract.ts` (the **parsed** shape), in `http-client.ts`
and `src/api/parse.ts`. Features only ever see the parsed shape, from either client.

| Route                           | Wire (what the proxy sends)                                                                      | Parsed (what a feature gets)                                                                                                   |
| ------------------------------- | ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------ |
| `GET /trace/heart-rate/{id}`    | `{resources: Bundle, source: {kind, window} \| null}`                                            | `HeartRateTrace {observation, provenance, device, source: {kind: 'reconstructed' \| 'original' \| 'unknown', window} \| null}` |
| `GET /trace/cuff-attempts/{id}` | `{attempt, messageSent, resources: Bundle}`                                                      | `CuffAttemptTrace {attempt, messageSent, auditEvent, observation, provenance}`                                                 |
| `POST /share`                   | `{outcome, steps, readBack: {patient, encounter, observation: {sent, stored}}}`; errors as above | `ShareResult` (same shape, checked); failures `ShareError {reason, steps}`                                                     |
| `GET /messages`                 | `{entries, nextCursor, unreadable, partial}`                                                     | the same, each row checked (`parseMessagesPage`): only a `confirmed` lookup carries an outcome; `partial` true unless `false`  |
| `GET /status`                   | `StatusSummary`                                                                                  | the same, with `watchSync` checked                                                                                             |
| `POST /cuff/readings`           | cuff-sim's result                                                                                | the same, with `attemptId`, `framing`, `messageName` and `messageNameSource` checked                                           |
| `GET /fhir/Observation?...`     | searchset Bundles, paged with `link[next]`                                                       | `Observation[]`, every page followed (inside the proxy's base URL only)                                                        |

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

**Messages log rows.** The proxy reads send-attempt AuditEvents from the **audit FHIR store**
(D15, separate from the health-data store) and turns each into a row with T31's
`parse_send_attempt`:

- `cuff-send`: the fields of T31's `SendAttemptRecord` (`sentAt` = period start, `recordedAt`,
  `transport`, `controlId` = MSH-10, `result` accepted / rejected / no-acknowledgment, `outcome`
  0 / 4 / 8, `commitAck` with the MSA-1 code and cleaned MSA-3 reason or the no-acknowledgment
  reason, `messageName`, `sample`), plus `application {outcome: accepted | error | pending,
reason}` joined from the stored message's labels where the send was accepted, and `observation`.
  `commitAck.raw` is the `ack-message` detail **decoded** to text as a convenience; the AuditEvent
  in the trace stays exactly as stored (base64). `messageLookup` says whether the store confirmed
  the attempt's message name. A stored message that no attempt anywhere claims is a row with
  `source: store-only` (id = the message name, `possibleDuplicateOf` a hint or null).
- `unreadable-send-attempt`: an AuditEvent that is ours but unreadable (T31's
  `UnreadableSendAttempt`): `{id, recordedAt, problem}`. T31 tells the proxy to surface these, never
  hide them, so the page shows them as error rows, and `unreadable` counts them.
- `fhir-transaction`: `{id, recordedAt, writer, httpStatus (always null), resources}`, from the
  Provenances one ingest Bundle wrote, pinned to the configured patient.

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
  attempt not sent in this session has no row there; since T19's review the cuff panel then loads
  its trace from the proxy and shows it from the trace alone (framing and MSA-2 shown as unknown),
  re-setting the selection from it.
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

### Open questions (answered by T14)

The ten open questions T16 listed here for T14, T15 and the orchestrator are answered in
services/fhir-proxy/README.md, "Answers to the dashboard's open questions" (patient pinning,
the reconstructed source window, "HL7v2 as sent", transaction rows from Provenances, the date
rule, share failures, forwarding cuff-sim's refusals and Firebase Hosting's 60 s limit,
`/status` from health-sync, `lastSyncAt` as the newest heart-rate `meta.lastUpdated`, and
store-only rows). The page follows those answers (the amendments table above). Question 6, how a
failed share is reported, is now specified for T15 in "For T15: the share route".
Firebase Hosting ends a rewritten request at 60 s (documented, T22); the proxy answers within 55 s.
