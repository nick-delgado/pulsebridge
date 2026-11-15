# Share with clinic (T19)

Under the figure: the "Share with clinic" button and its confirm step (`share-action`), the
step-by-step trace of the share (`share-trace`) and the "As stored in OpenEMR" read-back
(`share-readback`, D13). It talks to `getObservation` and `share`.

| File        | What it holds                                                              |
| ----------- | -------------------------------------------------------------------------- |
| `index.ts`  | `mountShare`, the shell's entry point                                      |
| `panel.ts`  | the flow: check, confirm, send, trace, read-back, selection changes        |
| `words.ts`  | pure: confirm text, step results, what did and did not happen on a failure |
| `diff.ts`   | pure: sent versus stored, as sentences                                     |
| `share.css` | `.share-…` styles, tokens only                                             |

## Design

- **Acts on the selection.** Enabled exactly when `shareableObservation(selection)` is not null;
  the hint (the button's description) gives the feature's own reason first. After a share, the
  hint says when this reading was shared in this session.
- **Check first.** "Share with clinic" reads the Observation (`GET /fhir/Observation/{id}`); one
  that is not `final` is refused there and nothing is sent.
- **Confirm.** Says what goes to the clinic (the reading; the Patient's PT identifier, official
  name, gender and birth date only if the clinic lacks it) and what is left out (device,
  performer, identifiers, this store's id and metadata, the Google Health user id), with the
  Observation as it is expected to be sent. A repeat says it creates no duplicate.
- **Send once.** Both buttons are disabled while the request runs; a second click does nothing.
  A selection change while checking or confirming closes the confirm and sends nothing; while
  sending it does not cancel (the write is under way), and the result names the reading it was
  for. Logout aborts everything and a late answer changes nothing.
- **Trace.** An ordered list: each step's title, its result in words ("Patient already existed:
  not created again", "Encounter found: reused", "Already in OpenEMR: refused as a repeat"),
  method, path and status, and the redacted bodies.
- **Read-back.** For the reading, the Patient and the Encounter: what was sent beside what OpenEMR
  stored, and the differences in words (dropped, added, changed, by path).
- **Failures** say what did and did not happen, from the steps completed so far (for example
  "a Patient was created in OpenEMR; the reading was not sent"). Only the proxy's own refusal
  before OpenEMR (a 400, 404, 422 or 503 with its OperationOutcome, no steps) may say "Nothing
  was sent to the clinic" (`ShareError.nothingSent`). No connection, a 5xx without the steps
  envelope, the hosting layer's HTML 504 or a 200 that cannot be read say it is not known what
  reached the clinic and that sharing again is safe.
- `notifyDataChanged('share')` after a share, and after every failure except a refusal before
  OpenEMR.
- **Repeats.** The confirm says a repeat creates no second Patient and reuses the day's
  Encounter; OpenEMR's refusal of a repeated reading was seen for blood pressure only, and the
  note says that for heart rate it is not verified.

The route's contract, which T15 implements, is in `web/README.md`, "For T15: the share route".
