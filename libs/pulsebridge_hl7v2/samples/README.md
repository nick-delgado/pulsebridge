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

`evidence` on each sample names the spike row the store behaviour was observed in, or says
"inferred" where no spike sent that exact message.

## The samples

| Name | Class | Validation | Tolerant parse | Store ACK (MSA-1, MSA-3) | Mapper outcome | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| `valid_bp_reading` | valid | pass | ok | AA | accepted | S4 row v and S5 'valid' (same structure; S4 had no UTC offset) |
| `valid_bp_reading_offset` | valid | pass | ok | AA | accepted | S5 stored an OBX-14 with offset -0500 and accepted it |
| `invalid_missing_pid3` | structurally_invalid | fail | ok | AA | error `missing-patient-identifier` | inferred: S4 found the store does not check required fields |
| `invalid_obx14_not_hl7_date` | structurally_invalid | fail | ok | AA | error `invalid-obx-14` | inferred: S4 found the store does not check data types |
| `unparseable_no_msh` | unparseable | fail | error | AE: Invalid MSH header | not reached | S4 row b3, S5 'starts with PID, no MSH' |
| `unparseable_lf_terminators` | unparseable | fail | error | AE: invalid segment terminator, store expects: "\r" | not reached | S4 row a6, S5 'line feeds between segments' |
| `unparseable_crlf_terminators` | unparseable | fail | error | AE: Invalid segment name "\nPI" | not reached | S5 '\r\n between segments' (HTTPS ingest; MLLP not tested) |
| `unparseable_separator_mismatch` | unparseable | fail | error | AE: Invalid segment name "PID" | not reached | S4 row b2, S5 'MSH-1 says \|, fields use #' |
| `noack_missing_end_block` | no_acknowledgment | pass | ok | none (timeout) | not reached | S4 row c1 (no reply for the full 75 s wait) |
| `noack_missing_final_cr` | no_acknowledgment | pass | ok | none (timeout) | not reached | S4 row c2 |
| `unmappable_unknown_obx3_code` | parses_but_cannot_map | pass | ok | AA | error `unknown-obx-3-code` | S4 row a3, S5 |
| `unmappable_missing_obx14` | parses_but_cannot_map | pass | ok | AA | error `missing-obx-14` | S4 row a1, S5 |
| `unmappable_non_numeric_obx5` | parses_but_cannot_map | fail | ok | AA | error `non-numeric-obx-5` | S4 row a2, S5 |
| `unmappable_msh_only` | parses_but_cannot_map | fail | ok | AA | error `no-observations` | S4 row a4, S5 |
| `unmappable_empty_msh9` | parses_but_cannot_map | fail | ok | AA | error `unsupported-message-type` | S4 row a5, S5 (stored with msgType "") |
| `unmappable_bare_msh` | parses_but_cannot_map | fail | ok | AA | error `unsupported-message-type` | S4 row b4, S5 'MSH\|^~\& followed by \r' (without the \r S5 got AE) |
| `unmappable_unknown_version` | parses_but_cannot_map | fail | error | AA | error `unparseable-by-mapper` | S4 row b8; S5 confirmed hl7apy 1.3.5 raises UnsupportedVersion |

Notes:

- The class is the **first** layer meant to catch the message when it is sent on purpose with
  "send malformed". Several `parses_but_cannot_map` samples also fail validation
  (non-numeric OBX-5, MSH only, empty MSH-9): the simulator would normally stop them, and the
  "send malformed" option exists to push them past that check so the mapper's error path shows.
- `unmappable_missing_obx14` and `unmappable_unknown_obx3_code` pass validation: OBX-14 is
  optional in 2.5.1 and validation does not check LOINC codes. Only the mapper can catch them.
- The `noack_*` samples are MLLP only. Over HTTPS there is no framing to break (S5).
- `unmappable_unknown_version` is stored by the store but hl7apy cannot parse it at all; the
  mapper must turn `HL7ParseError` into an `error` outcome rather than crash.
