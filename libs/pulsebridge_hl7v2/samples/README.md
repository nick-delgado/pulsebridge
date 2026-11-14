# HL7v2 sample messages

Synthetic messages shared by the `v2-to-fhir` mapper tests (T12) and the cuff simulator's
"send malformed" option (T13). Every patient in them is the made-up `TESTPATIENT^SYNTHIA`,
identifier `SYN-000123`. No real data.

## How to use them

```python
from pulsebridge_hl7v2.samples import load, by_class, CLASSES

sample = load("unparseable_no_msh")
sample.payload  # exact message bytes (HTTPS ingest sends these, base64-encoded)
sample.wire_bytes  # exact bytes for the MLLP socket, framing included (possibly broken on purpose)
sample.transports  # ("mllp", "https") or ("mllp",) for framing samples
sample.expected  # outcome at each layer, see below

for s in by_class("parses_but_cannot_map"):
    ...
```

To get raw files for sending by hand (for example with `nc`), export them to a directory outside
the repository:

```bash
python -m pulsebridge_hl7v2.samples export /tmp/hl7-samples
```

## Why JSON and not raw `.hl7` files

HL7v2 segments end with a carriage return (`\r`), and these samples must keep exactly the bytes
they declare: a CR, an LF, a CR LF, or an MLLP control byte. The repository's pre-commit hooks,
which CI runs on every file, rewrite plain-text files: `trailing-whitespace` strips a final `\r`,
and `end-of-file-fixer` appends `\n` to a file that does not end in one. Tried on throwaway
files, no raw CR-terminated message survived both hooks unchanged.

So each sample is an entry in `samples.json` listing its segments, its segment terminator, whether
the last segment is terminated, and its MLLP framing. The loader rebuilds the exact bytes. The
JSON escapes `\r` and `\n`, so no git line-ending setting or text hook can change them, and
`tests/test_samples.py` checks the bytes. The alternative, if raw files are preferred, is to add
`exclude: ^libs/pulsebridge_hl7v2/samples/` to the two text hooks in `.pre-commit-config.yaml`.

## Expected outcome fields

| Field | Meaning | Checked by this package's tests? |
| --- | --- | --- |
| `hl7apy_validation` | `pass` / `fail` for `validate_oru_r01` (the simulator's check before sending) | yes |
| `hl7apy_parse` | `ok` / `error` for `parse_er7`, the tolerant parse the mapper will use | yes |
| `commit_ack` | MSA-1 the HL7v2 store replies with: `AA`, `AE`, or `null` for no reply | framing only; the store codes come from spikes S4/S5 |
| `commit_ack_reason` | MSA-3 the store sent, as recorded in the spike notes (escapes decoded) | no (needs the store) |
| `no_ack` | why there is no ACK: `timeout` (the adapter waits for end bytes forever) | yes (the frame never completes) |
| `application_outcome` | the mapper's result, `accepted` / `error`; `null` when the message is never stored | no (T12) |
| `mapping_reason` | a **suggested** reason code in the style of spike S5; T12 owns the final list | no (T12) |

`evidence` gives, for each transport a sample can be sent on, where its expected store outcome
comes from. Every entry starts with one of three kinds, and the loader refuses any other:

- **observed in S4** or **observed in S5**: that spike sent a message with the same defect over
  that transport and recorded the reply. The spike's message was not byte-identical to the sample
  (control IDs, times and some field text differ), and the entry says so.
- **inferred:** no spike sent that defect over that transport. The entry says what the
  expectation rests on, usually S4's finding that the store checks only the header and segment
  structure, or S5's finding that ACK content is the same on both transports.

## The samples

| Name | Class | Validation | Tolerant parse | Store ACK (MSA-1, MSA-3) | Mapper outcome | Evidence, MLLP | Evidence, HTTPS |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `valid_bp_reading` | valid | pass | ok | AA | accepted | **observed in S4** (row v): a valid ORU^R01 got AA; same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent. Differences: OBR-3 and the UTC offset in MSH-7 and OBX-14 are new here | **observed in S5** ('valid'): a valid ORU^R01 got AA; same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent. Differences as for MLLP |
| `valid_bp_reading_offset` | valid | pass | ok | AA | accepted | **inferred:** S4 found the store does not check field content, so an OBX-14 offset should not matter | **inferred:** S5 stored a made-up message with OBX-14 offset -0500, but did not record its ACK |
| `invalid_missing_pid3` | structurally_invalid | fail | ok | AA | error `missing-patient-identifier` | **inferred:** S4 found the store does not check required fields | **inferred:** S5 found the same ACK behaviour as S4 on HTTPS |
| `invalid_obx14_not_hl7_date` | structurally_invalid | fail | ok | AA | error `invalid-obx-14` | **inferred:** S4 found the store does not check data types | **inferred:** S5 found the same ACK behaviour as S4 on HTTPS |
| `unparseable_no_msh` | unparseable | fail | error | AE: Invalid MSH header | not reached | **observed in S4** (row b3); same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent | **observed in S5** ('starts with PID, no MSH'); same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent |
| `unparseable_lf_terminators` | unparseable | fail | error | AE: invalid segment terminator, store expects: "\r" | not reached | **observed in S4** (row a6); same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent | **observed in S5** ('line feeds between segments'); same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent |
| `unparseable_crlf_terminators` | unparseable | fail | error | AE: Invalid segment name "\nPI" | not reached | **inferred:** S4 did not send CR LF; S5 observed AE for it over HTTPS and found NACK content identical on both transports | **observed in S5** ('\r\n between segments'); same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent |
| `unparseable_separator_mismatch` | unparseable | fail | error | AE: Invalid segment name "PID" | not reached | **observed in S4** (row b2), rebuilt from S4's description of the message; same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent | **observed in S5** ('MSH-1 says \|, fields use #'), rebuilt from the description; same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent |
| `noack_missing_end_block` | no_acknowledgment | pass | ok | none (timeout) | not reached | **observed in S4** (row c1: no reply for the full 75 s wait); same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent | not applicable |
| `noack_missing_final_cr` | no_acknowledgment | pass | ok | none (timeout) | not reached | **observed in S4** (row c2); same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent | not applicable |
| `unmappable_unknown_obx3_code` | parses_but_cannot_map | pass | ok | AA | error `unknown-obx-3-code` | **observed in S4** (row a3); same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent | **observed in S5**; same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent |
| `unmappable_missing_obx14` | parses_but_cannot_map | pass | ok | AA | error `missing-obx-14` | **observed in S4** (row a1); same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent | **observed in S5**; same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent |
| `unmappable_non_numeric_obx5` | parses_but_cannot_map | fail | ok | AA | error `non-numeric-obx-5` | **observed in S4** (row a2); same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent | **observed in S5**; same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent |
| `unmappable_msh_only` | parses_but_cannot_map | fail | ok | AA | error `no-observations` | **observed in S4** (row a4); same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent | **observed in S5**; same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent |
| `unmappable_empty_msh9` | parses_but_cannot_map | fail | ok | AA | error `unsupported-message-type` | **observed in S4** (row a5); same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent | **observed in S5** (stored with msgType ""); same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent |
| `unmappable_bare_msh` | parses_but_cannot_map | fail | ok | AA | error `unsupported-message-type` | **inferred:** S4 row b4 got AA for 'MSH\|^~\&' but did not record whether the payload ended in \r; without it S5 got AE over HTTPS | **observed in S5** ('MSH\|^~\& followed by \r'): AA |
| `unmappable_unknown_version` | parses_but_cannot_map | fail | error | AA | error `unparseable-by-mapper` | **observed in S4** (row b8: MSH-12 9.9 got AA); same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent | **inferred:** S5 did not send MSH-12 9.9; it found ACK content identical on both transports |
| `unmappable_unknown_msh9` | parses_but_cannot_map | fail | ok | AA | error `unsupported-message-type` | **inferred:** S4 found the store does not check MSH-9 against the body; S5 found ACK content identical on both transports | **observed in S5**: an ADT message was stored and notified with msgType ADT; that message's body was not an ORU reading |
| `unmappable_empty_msh12` | parses_but_cannot_map | fail | error | AA | error `unparseable-by-mapper` | **observed in S4** (row b9: AA, and the ACK carried no version); same kind of message; this sample's exact bytes (control ID, times, some field text) were not sent | **inferred:** S5 did not send an empty MSH-12; it found ACK content identical on both transports |

Notes:

- The class is the **first** layer meant to catch the message when it is sent on purpose with
  "send malformed". Several `parses_but_cannot_map` samples also fail validation
  (non-numeric OBX-5, MSH only, empty or wrong MSH-9, empty or unknown MSH-12): the simulator would normally stop them, and the
  "send malformed" option exists to push them past that check so the mapper's error path shows.
- `unmappable_missing_obx14` and `unmappable_unknown_obx3_code` pass validation: OBX-14 is
  optional in 2.5.1 and validation does not check LOINC codes. Only the mapper can catch them.
- The `noack_*` samples are MLLP only. Over HTTPS there is no framing to break (S5).
- `unmappable_unknown_version` (MSH-12 `9.9`) and `unmappable_empty_msh12` are stored by the
  store, but hl7apy cannot parse them at all; the mapper must turn `HL7ParseError` into an
  `error` outcome rather than crash.
- `unmappable_unknown_msh9` says `ADT^A01` over an ORU body. hl7apy's tolerant parse reads it;
  the mapper should refuse any message type other than `ORU^R01`.
