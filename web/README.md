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

Put a local value in `web/.env.local` (git-ignored); `.env.example` shows the format. Vite copies
every `VITE_` variable into the public bundle, so **never put a secret in one**. The mode (mock or
HTTP) is not a variable: it is `vite --mode mock`, fixed at build time, and an HTTP build does not
contain the mock client at all (`scripts/check-bundle.mjs` fails the build if it does).

## Page layout and who fills what

```
+--------------------------------------- header -----------------------------------------+
| PulseBridge [Mock data]  status strip: last sync, counts, FHIR store, OpenEMR  Log out |
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

### Directory ownership and mounting

Each feature owns one directory and exports **one** mount function from its `index.ts`. The shell
(`src/shell/app.ts`) calls it once after login. A feature only touches the elements in its
`slots`; it never edits files outside its directory.

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
  Pass it to `addEventListener(..., { signal })`, `selection.subscribe(fn, signal)` and `el(...,
{ signal })` so nothing outlives the dashboard.
- `refreshStatus()`: reload the header's status strip (for example after a cuff send).

If a mount function throws, the shell logs it and shows "This area failed to load" in that
feature's first slot; the rest of the page keeps working. Split your code into more files inside
your directory as you like.

### Using the client

```ts
import { LOINC } from '../api/fhir';

const observations = await client.searchObservations({
  code: LOINC.heartRate,
  range: { start: new Date(Date.now() - 24 * 3600_000), end: new Date() },
});
```

- Every method is in `PulseBridgeApi` (`src/api/contract.ts`); both the mock and the HTTP client
  implement it, so code written against the mock works unchanged against the proxy.
- **401 is handled for you.** The client clears the token and the shell shows the login form; the
  call rejects with `UnauthorizedError`, which you can ignore (the dashboard is about to go).
- Other failures reject with `ApiError` (`status`, `message`, and `outcome`, the FHIR
  `OperationOutcome` when the proxy sent one). Status 0 means the network failed. Show
  `error.message` to the user.
- Never call `fetch` yourself: the page has one backend and one client.

### Using the selection store

```ts
// Set it when the user picks something (T17 chart point, T18 readings row):
selection.set({ source: 'watch', observationId: obs.id, summary: 'Heart rate 68 /min, 09:05' });
selection.set({ source: 'cuff', attemptId, observationId: maybeNull, summary: 'BP 121/79' });

// React to it (trace panels, share button, your own highlight):
selection.subscribe((current) => {
  /* current is Selection | null */
}, signal);
```

- There is one selection for the page: picking a cuff reading replaces a heart-rate point. When
  the selection moves to the other side, drop your highlight.
- `subscribe` calls the listener immediately with the current value, then after each change.
- `shareableObservation(selection)` gives the `Observation/<id>` the Share button would send, or
  null (nothing selected, or a cuff reading that produced no Observation). The button is enabled
  exactly when it is non-null.

### Rendering data

- Build DOM with `el()` and `svg()` from `src/ui/dom.ts`. String children become text nodes.
- **Never use `innerHTML`, `outerHTML`, `insertAdjacentHTML` or `document.write`.** ESLint fails
  on them. Data from the server (patient names, OperationOutcome text, HL7v2 segments) must never
  be parsed as markup.
- `codeBlock(content, label)` shows JSON or ER7 (HL7v2 segments separated by `\r` are shown one per
  line). `placeholder(text)` shows a muted empty-state line.
- `createTabs(label, tabs, signal)` (`src/ui/tabs.ts`) gives accessible trace-panel tabs with
  arrow-key support; fill `tabs.panel(id)`.
- No inline `style="..."` attributes and no inline scripts: the Content-Security-Policy blocks
  them. Setting `element.style.x` from script is allowed. Charts that draw to `<canvas>` or SVG are fine.

### Styling tokens

All colours, spacing and type are CSS custom properties in `src/styles/tokens.css`, with light and
dark values (`prefers-color-scheme`). Use them, never raw values:

| Token                                                            | Use                                                     |
| ---------------------------------------------------------------- | ------------------------------------------------------- |
| `--color-bg`, `--color-surface`, `--color-surface-sunken`        | page, cards, code blocks                                |
| `--color-text`, `--color-text-muted`, `--color-border`           | text and lines                                          |
| `--color-episodic` / `--color-continuous`                        | the cuff side / the watch side (series colour, accents) |
| `--color-ok`, `--color-warn`, `--color-error`, `--color-neutral` | states; always pair with words                          |
| `--color-primary`, `--color-primary-text`, `--color-focus`       | primary button, focus ring                              |
| `--space-1` … `--space-6`, `--radius`, `--gutter`                | spacing (4 px steps), corners, page gutter              |
| `--font-sans`, `--font-mono`, `--text-xs` … `--text-lg`          | system fonts only (no web fonts)                        |

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
- Every test is break-checked (CLAUDE.md): break the implementation, see the test fail, restore.
- Use synthetic data only. The mock's data is generated (`src/api/mock-data.ts`); never paste real
  readings into a test.

## Security basics

- **Token storage.** The bearer token from `POST /auth/login` is kept in `sessionStorage`
  (`src/api/session.ts`): per tab, gone when the tab closes, and never sent automatically. No
  cookie is used, so there is nothing for cross-site request forgery to ride on (D9,
  docs/spikes/s6.md). Logout and any 401 delete it.
- **Requests** are sent with `credentials: 'omit'`, `cache: 'no-store'` (health data is not kept
  in the HTTP cache) and `redirect: 'error'`. A paging `next` link is followed only when it stays
  inside the proxy's base URL, so the token cannot be sent to another host.
- **Content-Security-Policy.** The build adds a `<meta>` policy (`vite.config.ts`):
  `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src <proxy>;
base-uri 'none'; form-action 'none'; object-src 'none'`. A mock build has `connect-src 'none'`,
  so mock mode cannot make a network request at all. The dev server has no policy (Vite's hot
  reload injects styles). **The hosting layer (Firebase Hosting, T22) must send the real policy as
  an HTTP header**, which can also set `frame-ancestors 'none'` (a `<meta>` policy cannot).
- **No secrets in the bundle.** The page holds no key or password; the only credential it ever
  sees is the reviewer's session token, at run time.

## Dependencies

Every version is pinned exactly in `package.json` (`.npmrc` sets `save-exact`), and
`package-lock.json` is committed. All are development dependencies; the shipped page has **no**
runtime dependencies (about 18 kB of our own JavaScript before gzip).

| Package                                     | Why                                                                                                                                                                                   |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `vite`                                      | Dev server and production bundler (D5).                                                                                                                                               |
| `typescript`                                | Strict type checking (`tsc --noEmit`). 6.0, because typescript-eslint does not yet support 7.x.                                                                                       |
| `vitest`                                    | Unit tests; shares Vite's config and TypeScript handling.                                                                                                                             |
| `jsdom`                                     | A browser-like DOM for the unit tests.                                                                                                                                                |
| `@playwright/test`                          | Browser test in Chromium. Pinned to 1.63.0 to match the Chromium build preinstalled on the development machine; `npx playwright install chromium` fetches the matching one elsewhere. |
| `eslint`, `@eslint/js`, `typescript-eslint` | Linting, including type-aware rules and the `innerHTML` ban.                                                                                                                          |
| `prettier`                                  | Formatting.                                                                                                                                                                           |

The chart library (D5) is T17's choice and will be the first runtime dependency.

Known: `npm audit` reports a moderate advisory in `minimatch`, pulled in by typescript-eslint
(development only; it affects glob patterns from untrusted input, which lint never sees). It
clears when typescript-eslint updates.

Adding a dependency: npm 10.9 crashes while resolving this tree ("Cannot read properties of null
(reading 'edgesOut')", an npm arborist bug); use `npx npm@11.21.1 install --save-dev <pkg>@<exact>`.
`npm ci` from the lockfile works with npm 10 and 11.

## HTTP contract (proposal for T14 and T15)

`fhir-proxy` does not exist yet. These are the routes the HTTP client (`src/api/http-client.ts`)
calls; the TypeScript types in `src/api/contract.ts` are the normative shapes. Paths are relative
to the base URL (default `/api`). Where the data is FHIR, the body is FHIR (`application/fhir+json`);
the other bodies are views the proxy assembles (`application/json`).

All routes except `POST /auth/login` (and, by FHIR convention, `GET /fhir/metadata`) require
`Authorization: Bearer <token>` and answer `401` when it is missing, expired, altered or signed
under a previous password. Errors carry a FHIR `OperationOutcome` body where possible; the client
shows its first `issue.diagnostics`.

| Method and path                                                                                          | Client method          | Request                                                                                     | Response                                                                                                                                                                                                                                                                                                                                                                                                             |
| -------------------------------------------------------------------------------------------------------- | ---------------------- | ------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /auth/login`                                                                                       | `login`                | `{"password": "..."}`                                                                       | `200 {"token", "expires_at"}`; `401` wrong password; `429` with `Retry-After`; `503` not configured                                                                                                                                                                                                                                                                                                                  |
| `GET /status`                                                                                            | `getStatus`            |                                                                                             | `StatusSummary`: `watchSync {state: ok \| reconnect-needed \| never-synced \| error, lastSyncAt, detail}`, `observationCounts {watch, cuff}`, `fhirStore` and `openemr` `{reachable, checkedAt, detail}`                                                                                                                                                                                                             |
| `GET /fhir/Observation?code=http://loinc.org\|8867-4&date=ge<start>&date=lt<end>&_sort=date&_count=1000` | `searchObservations`   | FHIR search; `code` is a token (`system\|code`); also `85354-9`                             | `Bundle` (`searchset`) of Observations, oldest first; `link[next]` to the next page, under the same base URL                                                                                                                                                                                                                                                                                                         |
| `GET /trace/heart-rate/{observationId}`                                                                  | `getHeartRateTrace`    |                                                                                             | `{"resources": Bundle (collection: Observation, Provenance, Device), "source": <Google Health roll-up window> \| null}`                                                                                                                                                                                                                                                                                              |
| `GET /cuff/samples`                                                                                      | `listMalformedSamples` |                                                                                             | `[{name, label, class, transports}]`, sample names as in `libs/pulsebridge_hl7v2/samples/samples.json`                                                                                                                                                                                                                                                                                                               |
| `POST /cuff/readings`                                                                                    | `submitCuffReading`    | `{systolic, diastolic, transport: "mllp" \| "https", sendMalformed: <sample name> \| null}` | `200 CuffSendResult` (forwarded to the cuff simulator, D10): `{attemptId, sentAt, transport, controlId, sample, messageSent, messageName, ack, audit}` where `ack` is `{received: true, code: AA \| AE \| AR, controlId, reason, raw}` or `{received: false, why: timeout \| connection-closed \| http-error \| network-error, httpStatus, detail}`. `422` + OperationOutcome for out-of-bounds input (nothing sent) |
| `GET /trace/cuff-attempts/{attemptId}`                                                                   | `getCuffAttemptTrace`  |                                                                                             | `{"attempt": SendAttemptEntry, "messageSent": "<ER7>" \| null, "resources": Bundle (collection: AuditEvent, Observation?, Provenance?)}`                                                                                                                                                                                                                                                                             |
| `GET /messages?kind=&outcome=&count=&cursor=`                                                            | `listMessages`         | `kind`: `cuff-send` \| `fhir-transaction`; `outcome`: `accepted` \| `error` \| `pending`    | `{"entries": [...], "nextCursor": string \| null}`, newest first. A `cuff-send` entry: `{id, recordedAt, transport, controlId, sample, commitAck, application: {outcome, reason} \| null, messageName, observation, source: audit-event \| store-only}`. A `fhir-transaction` entry: `{id, recordedAt, writer, httpStatus, resources}`                                                                               |
| `POST /share`                                                                                            | `share`                | `{"observations": ["Observation/<id>", ...]}`                                               | `{"outcome": shared \| already-shared \| partly-shared \| failed, "steps": [ShareStep], "readBack": Bundle (collection: Patient, Encounter, Observations as OpenEMR stored them)}`. Each step: `{step, label, request {method, url (path only), body}, response {status, body}, result, durationMs}`; never the private key, the signed assertion or the access token                                                |

Field names in the JSON views are camelCase, except the login response, which follows
docs/spikes/s6.md as written (`token`, `expires_at`).

Two terms used in the shapes (D7): the **commit-level acknowledgment** is the HL7v2 ACK the store
returned (MSA-1 `AA` stored, `AE` rejected, with MSA-3 as the reason), or no ACK at all; the
**application-level outcome** is what `v2-to-fhir` made of a stored message, from its
`application_outcome` and `reason` labels, or `pending` when it has no label yet.

### Open questions for T14, T15 and the orchestrator

1. **Patient parameter.** The page sends no `patient` search parameter: this is a single-patient
   PHR, and the proposal is that the proxy pins searches to the configured patient (T14's AC says
   "search by patient"). Alternative: `/status` returns the Patient reference and the page passes it.
2. **Source JSON for the heart-rate trace.** The right trace panel shows the Google Health roll-up
   window an Observation came from, but T10 does not store it. Options: T10 keeps it (for example
   as a `Binary` or a Provenance entity), or the proxy rebuilds it from the Observation (min, mean,
   max and the period), labelled as reconstructed. Until decided, `source` may be null.
3. **"HL7v2 as sent" for a valid reading.** T31 keeps the raw message only for malformed samples.
   For a valid send the proxy must fetch the stored message (`view=RAW_ONLY`) by name; over MLLP
   the ACK does not carry the name, so the proxy needs a way to find it (for example by the control
   ID in MSH-10, if the store can filter on it).
4. **FHIR transaction rows.** The brief's log lists one row per FHIR transaction (Bundle, HTTP
   status, resource ids), but no task records them. Proposal: derive them from the `Provenance`
   each ingest Bundle writes (`recorded`, `target`); the HTTP status would then be unknown (`null`).
5. **Cuff bounds.** `CUFF_BOUNDS` (systolic 60–250, diastolic 30–150, systolic above diastolic) is
   a proposal; T13 owns the real limits, and `/cuff/samples` could return them too.
6. **AuditEvent codes.** The mock's AuditEvent is an approximation (DICOM 110106 "Export"); T31
   decides the real codes, and the page only displays the resource.
