# Cuff area (T18)

The left side of the dashboard: the blood-pressure cuff simulator form, this session's readings
list, and the left trace panel. It talks only to `submitCuffReading`, `getCuffAttemptTrace` and
`listMalformedSamples` on the shared client (D10: the proxy forwards cuff requests to cuff-sim).

| File          | What it holds                                                                          |
| ------------- | -------------------------------------------------------------------------------------- |
| `index.ts`    | `mountCuff`, the shell's entry point                                                   |
| `panel.ts`    | the form, the readings list, polling, selection and data-change handling (the flow)    |
| `form.ts`     | pure: input rules at cuff-sim's limits, the exact request body, samples by class       |
| `readings.ts` | pure: one row per send, the words for both acknowledgment levels, shareability, errors |
| `trace.ts`    | the three trace tabs, rendered as text only                                            |
| `cuff.css`    | `.cuff-…` styles, tokens only                                                          |

## Design

- **Limits.** The inputs accept what cuff-sim accepts (`CUFF_SERVICE_LIMITS`: whole numbers 1 to
  400 mm[Hg], systolic above diastolic). The mapper's range (`MAPPER_PLAUSIBLE_RANGE`) is shown as
  information: a value between the two is sent, stored with `AA`, then mapped to `error` /
  `out-of-range`. The form says so, next to the field, while you type.
- **Two acknowledgment levels (D7)**, always two separately labelled items, in the list and in
  the trace: "Commit-level acknowledgment (from the store)" (the ACK in the send result) and
  "Application-level outcome (from the mapper)" (`pending` until the stored message is labelled).
  The store answers in HL7's original acknowledgment mode, so the ACK tab explains that the two
  names come from the enhanced mode (`ACK_MODE_NOTE`), and the list says it in short. Reason codes
  become sentences through the shared `src/api/reasons.ts`.
- **A malformed send** carries only `transport` and `sendMalformed`; the pressure inputs are
  disabled. Samples are listed by class with the layer that catches each class; a sample not
  offered over the chosen transport is disabled (and unchosen, with a reason, when you switch).
- **Readings list** = this session's sends, newest first, at most 20 (`MAX_READINGS`). The
  messages log (T19) is where every attempt, from any session, is listed.
- **Not recorded.** A send whose AuditEvent was not built (`attemptId: null`) or not written
  (`audit.recorded: false`, so nothing is stored under the id) is listed and marked "Not
  recorded", has no "Show trace" button, is never put in the selection and is not polled.
- **Polling.** A stored, recorded send is polled with `pollUntil` (every 3 s while visible, at
  most 2 minutes). It stops when the outcome settles, the trace answers 404, the row is dropped,
  or the dashboard is torn down. After the bound the row says it stopped checking and offers
  "Check again". A passing failure is retried at the next tick.
- **Selection.** After a send the new row is selected, unless the selection changed while the
  send was in flight. A recorded row sets the shared selection with `unshareableReason` unless
  the reading was accepted and its Observation exists and is `final`. On a `sync` or `share`
  data change the selected reading is first marked "being checked again" (not shareable), then
  re-checked through its trace; a 404 clears the selection. A poll answer that settles the
  selected row re-sets the selection.
- **Quiet polling.** A trace re-render patches only the parts whose content changed, so a poll
  tick that changes nothing touches no DOM (focus and scroll inside a code block survive) and
  the live summary is written only when its text changes.
- **Late answers.** Every request has its own `AbortController` chained to the dashboard's
  signal and is checked before rendering. A trace answer that still says `pending` never moves a
  settled row back (`withTrace`).
- **The trace frame** has a fixed height in every state, so selecting or switching tabs does not
  move the page.

## For T19 and T14

See "What T14, T18 and T19 must know" in `web/README.md`. In short:

- The selection a cuff row sets: `{source: 'cuff', attemptId, observationId, summary,
unshareableReason}`. `observationId` is null until the mapper has accepted the message.
  `unshareableReason` is set for pending, rejected, no acknowledgment, mapper error, an
  Observation that is not `final` or not found, and while a check runs.
- A cuff selection set by another feature for an attempt in this panel's list loads its trace
  and is re-set from the row. An attempt not sent in this session (selected in the messages log)
  has no row: its trace is fetched and shown from the trace alone (TraceState `attempt`, built
  with `readingFromAttempt`: framing and MSA-2 are said to be unknown, the bytes shown only when
  the proxy returned them), 404/410/422 are final, it is not polled, and it is re-checked on
  `'sync'` and `'share'`.
- The panel announces `'cuff-send'` after every send that returned a result, and after a failure
  that may have sent one (504, a lost connection, an unreadable 200). It re-checks its selection
  on `'sync'` and `'share'`.
