# cuff-sim

The cuff simulator backend (task T13). It takes one blood-pressure reading, builds an HL7v2
ORU^R01 with T07's builder, validates it, sends it to the HL7v2 store over MLLP (default) or
HTTPS ingest, and returns the store's acknowledgment. Every send attempt, whatever happened, is
recorded as a FHIR `AuditEvent` in the audit FHIR store (D12, D15).

```
dashboard ──> fhir-proxy (login, D9/D10) ──OIDC──> cuff-sim
                                                      │ build ORU^R01 (T07) + validate
                                                      ├── MLLP frame ──> edge VM adapter ──┐
                                                      └── HTTPS messages:ingest ───────────┴─> HL7v2 store
                                                      │ ACK (AA / AE) or no acknowledgment
                                                      └── PUT AuditEvent/<id> ──> audit FHIR store
```

It follows `v2-to-fhir` (T12): Flask and gunicorn, configuration validated at start-up, a thin
route, JSON log lines, tests on `FakeTransport` and a loopback socket.

## Layout

```text
services/cuff-sim/
  pyproject.toml       the three libs, Flask, gunicorn (exact pins)
  cuff_sim/
    request.py         the request body: bounded integers, transport, a sample NAME
    message.py         build + validate a reading (T07), pick a malformed sample, derived name
    mllp_client.py     MLLP: connect, write, read one frame, all on one deadline; close
    https_client.py    HTTPS: messages:ingest and the mapping of every response
    outcome.py         the one result shape: AckReceived or NoAck(why)
    audit.py           the AuditEvent (T31) and its single PUT to the audit store
    ratelimit.py       per-instance token bucket
    service.py         one send, start to finish; the JSON result
    config.py          environment variables
    app.py             Flask app: two routes, start-up checks, JSON logs
  tests/               pytest; no network beyond 127.0.0.1, no credentials
```

## API

Only `fhir-proxy` calls this service (D10). The paths and bodies are the dashboard's proposed
contract (`web/src/api/contract.ts`), so the proxy can forward the body unchanged.

### `POST /cuff/readings`

```json
{"systolic": 120, "diastolic": 80, "transport": "mllp", "sendMalformed": null}
```

| Field | Rule |
| --- | --- |
| `systolic`, `diastolic` | JSON integers (not `120.0`, `"120"` or `true`), each 1 to 400 mm[Hg], systolic above diastolic: T07's builder limits. Required unless `sendMalformed` is set; then they may be left out, and are ignored, but are still checked if present. |
| `transport` | `"mllp"` (default when absent) or `"https"` |
| `sendMalformed` | `null`, or the exact name (case-sensitive) of a sample from `GET /cuff/samples` that lists the chosen transport |
| anything else | refused |

The ranges are T07's, as the brief asks. The dashboard proposes narrower ones (`CUFF_BOUNDS`,
60–250 / 30–150) and the mapper (T12) accepts systolic 50–300 and diastolic 20–200: a reading
between the mapper's range and T07's is stored with `AA` and then gets the application-level
outcome `out-of-range`, which is one of the D7 layers the demo can show.

**No caller text reaches a message.** The only strings accepted select fixed content (a
transport, a sample name from T07's manifest). The patient, the names and MSH-3/MSH-4 come from
configuration.

Responses. Every error body is a FHIR `OperationOutcome` with one issue per problem; the issue's
`expression` names the field and its `diagnostics` is a fixed sentence that never repeats what
the caller sent.

| Status | When | Issue `code` | Sent? Recorded? |
| --- | --- | --- | --- |
| 200 | a send happened, whatever the store said (AA, AE, or no acknowledgment) | | sent; recorded (or `audit.recorded: false`) |
| 400 | the body is not a JSON object (not JSON, an array, a string) | `invalid` | no; no |
| 413 | the body is over 16 KiB (`MAX_BODY_BYTES`; a real body is under 100 bytes) | `too-long` | no; no |
| 422 | a field is wrong: bounds, type, unknown field, unknown sample, sample not for this transport | `invalid` | no; no |
| 429 | rate-limited; `Retry-After: <seconds>` | `throttled` | no; no |
| 500 | a built message failed HL7v2 validation (a bug: inputs and configuration were checked) | `structure` | no; no |
| 500 | any other bug in the handler, logged by exception type only | `exception` | depends where it failed; no 500 can happen once the send has started (every reply and recording error is a result) |

The 200 result (`CuffSendResult` in the contract, plus `result`, `framing` and
`messageNameSource`):

```json
{
  "attemptId": "<AuditEvent id>",
  "sentAt": "2026-11-15T10:00:00.123Z",
  "transport": "mllp",
  "controlId": "<MSH-10>",
  "sample": null,
  "messageSent": "MSH|^~\\&|CUFFSIM|...\rPID|...\r...",
  "framing": "standard",
  "messageName": "projects/.../hl7V2Stores/<s>/messages/<id>",
  "messageNameSource": "derived-sha256-unverified",
  "result": "accepted",
  "ack": {"received": true, "code": "AA", "controlId": "<MSA-2>", "reason": null,
          "raw": "MSH|^~\\&|PHR|...\rMSA|AA|<id>"},
  "audit": {"recorded": true}
}
```

`ack` is either `{received: true, code, controlId, reason, raw}` (MSA-1, MSA-2, MSA-3 decoded,
the ACK text) or `{received: false, why, httpStatus, detail, replyPrefixBase64}`; `audit` is
`{recorded: true}` or `{recorded: false, detail}`. `result` is `accepted` (AA/CA), `rejected`
(AE/AR/CE/CR) or `no-acknowledgment`. `framing` is the MLLP framing used (`standard`, or a
sample's `missing_end_block` / `missing_final_cr`), `null` over HTTPS. `attemptId` is `null`
only if the AuditEvent could not even be built.

Differences from the contract, for T14 and T18: the three extra keys above; `attemptId` can be
`null`; 413 is added; and the field is `sendMalformed` (the contract's spelling), where the work
plan says `send_malformed`.

### `GET /cuff/samples`

`[{"name", "label", "class", "transports"}]`: every T07 sample except the `valid` class, with
the manifest's description as `label`. Bad-framing samples list only `mllp`.

## Sending

### A reading

1. A **new control ID** (MSH-10, `new_control_id()`, 20 random hex characters) for every
   reading. The mapper keys the Observation on it (T12), so a reused ID would overwrite an
   earlier reading.
2. `build_bp_oru_r01` with the configured patient and sender, MSH-7/OBR-7/OBX-14 = now (UTC,
   whole seconds), then `to_er7_bytes` (every segment ends with `\r`, the last one too).
3. `validate_oru_r01` on those exact bytes. If it fails, nothing is sent, no connection is
   opened and no ingest call is made.

### A malformed sample

The sample's bytes from T07's loader, **not validated**, with its PID segment rebuilt for the
configured patient by T07's `Sample.with_patient` (T33): PID-3.1 `PATIENT_ID`, PID-3.4
`PATIENT_ASSIGNING_AUTHORITY`, PID-5 the configured names, exactly as in a built reading.
Every other byte, and so the sample's defect, is T07's. Over MLLP the sample's own framing is
kept (`wire_bytes`), so `noack_missing_end_block` really is sent without its end bytes. Over HTTPS only samples whose manifest lists `https` are accepted. The control ID is the
sample's own MSH-10 read with hl7apy, or `null` when hl7apy cannot read the sample (no MSH,
line-feed terminators).

What to expect from samples, because their bytes are fixed:

- **The bytes name the configured patient, as the AuditEvent does**, for every sample T07 can
  substitute. So with any `PATIENT_ID` (set the mapper's to the same value), a "parses but
  cannot be mapped" sample ends in its own reason, not `unknown-patient`:
  `unmappable_unknown_obx3_code` (`unknown-obx-3-code`), `unmappable_missing_obx14`
  (`missing-obx-14`), `unmappable_non_numeric_obx5` (`non-numeric-obx-5`) and
  `invalid_obx14_not_hl7_date` (`invalid-obx-14`) among them. `tests/test_sample_patient.py`
  runs the bytes `prepare` returns through `v2-to-fhir`'s mapper configured for another patient.
  T07 leaves seven samples unchanged, so they still carry `SYN-000123`:
  `invalid_missing_pid3` (its empty PID-3 is the defect; the mapper's reason,
  `missing-patient-identifier`, does not depend on the patient), `unmappable_msh_only` and
  `unmappable_bare_msh` (no PID; their reasons come before the patient check), and the four
  `unparseable_*` samples, which the store rejects with `AE` before any mapper sees them. For
  those seven the AuditEvent's patient and the bytes still differ.
- **Sending the same sample twice stores one message.** The store deduplicates on exact bytes
  (spike S5): the second send gets `AA` with the same message name and publishes no second
  notification, so the mapper runs once. (With the patient substituted, "the same bytes" means the
  same sample and the same configured patient.) Two AuditEvents are recorded, differing only in their
  send time (and their id, which includes it).
- **No malformed sample is mappable.** Each is rejected by the store (`AE`), gets no
  acknowledgment, or is stored and then ends with the mapper's application-level outcome
  `error` (T12 tests every sample's reason). None produces an Observation.
- **Samples share control IDs.** For example `invalid_missing_pid3` and
  `invalid_obx14_not_hl7_date` both carry `SYN-BAD-0001`, and every `unparseable` sample has
  none. A sample's control ID is not a key for anything.
- **Same-millisecond collision.** T31's AuditEvent id is derived from the sender, the
  transport, MSH-10 and the send time in milliseconds. Two sends of samples with the same
  control ID (or none) on the same transport in the same millisecond, for example from two
  instances, get one id, and the second write replaces the first (T31: "last write wins"; the
  store's history keeps both versions). Built readings cannot collide: each has a new control
  ID.

### MLLP

A connection to `MLLP_HOST:MLLP_PORT` (private VPC path, D14; no TLS at this layer), a write of
the frame, then T07's `MllpReader.read_frame` for one reply frame, capped at 4096 bytes. A new
connection per send, always closed.

**One deadline.** `MLLP_TIMEOUT_SECONDS` is turned into a deadline once, and every step gets
only what is left of it: each address tried by connect (the service connects address by
address itself; `socket.create_connection` would give every address the full timeout), the
write, and the read of the whole frame. So a send waits at most `MLLP_TIMEOUT_SECONDS` on the
socket, plus name resolution, which the standard library cannot time-limit: set `MLLP_HOST` to
the VM's IP address (T22).

| On the socket | `ack` |
| --- | --- |
| an ACK frame | `received: true`, parsed by `parse_ack` (AA, or AE with MSA-3) |
| two frames at once | the first is the reply; a different MSA-2 shows in `ack.controlId` |
| closed with no data (S4: the adapter's API call failed) | `why: connection-closed` |
| closed after part of a frame, or reset | `why: connection-closed` |
| nothing before the deadline (S4: bad framing), or bytes trickling | `why: timeout` |
| a frame that `parse_ack` refuses or cannot read (it can raise more than `AckParseError`), over 4096 bytes, or holding an MLLP control byte | `why: unreadable-reply` (first bytes kept when there are any) |
| connection refused on every address, host not resolvable | `why: connection-closed` |
| connect or write not done before the deadline | `why: timeout` |

MSA-2 is not required to match on a NACK (the store leaves it empty when it could not read the
header), and the reply type `ACK` (not `ACK^R01`) is what the store sends. On an accepting ACK a
different MSA-2 is logged (`ack_matches_control_id: false`), not treated as a failure.

**The stored message's name over MLLP is DERIVED.** The ACK does not carry it. Spike S5 observed,
on one message ingested over HTTPS, that the id after `/messages/` is the URL-safe base64 of the
SHA-256 of the message bytes. For an accepted (AA) MLLP send the service returns
`<store>/messages/<urlsafe-b64(sha256(bytes inside the frame))>` with
`messageNameSource: "derived-sha256-unverified"`. It is **unverified** beyond that one message
and for the MLLP path; confirm on the first deployment (T25) by listing the store. It is not
written into the AuditEvent until T31 has a field for a derived name; that is then a one-line
change, marked `FOLLOW-UP` in `CuffSimulator.send` (`service.py`).

### HTTPS

`pulsebridge_gcp.HealthcareClient.ingest(store, bytes, labels={"transport": "https"})`. The
label lets the messages log tell the transports apart (spike S5; the adapter cannot set labels).

| Response | `ack` |
| --- | --- |
| 200 with `hl7Ack` | `received: true`, parsed by the same `parse_ack` (the store's AA); `messageName` from the response, `messageNameSource: "ingest-response"` |
| 400 or 409 with `error.details[].hl7Nack` | `received: true`, the NACK (AE) with MSA-3: an answer, not a transport failure |
| 400/409 without `hl7Nack`, 401, 403, 404, 408, 429, 5xx | `why: api-error`, `httpStatus` |
| no HTTP response, caused by a timeout | `why: timeout` |
| no HTTP response, any other cause | `why: connection-closed` |
| 200 whose `hl7Ack` is missing, not base64, or not a readable ACK; a 3xx | `why: unreadable-reply` |

The ACK bytes over HTTPS have no MLLP frame and no trailing `\r`; `parse_ack` does not need
one.

**A failure with no HTTP status** has no dedicated reason in T31's closed set (`api-error`
requires 400–599). `pulsebridge_gcp`'s `RequestsTransport` raises `RetryableError(status=None)`
with the original exception chained as `__cause__`, and the service reads it:

- a `requests.Timeout` (connect or read) is `timeout`. After a read timeout the request was
  sent, so the message **may have been stored**: that is exactly what "no acknowledgment" means.
- anything else is `connection-closed`: refused or reset, DNS, and a failure to refresh the
  access token. The exchange ended with no reply of any kind, as for a refused MLLP connection.
  A token refresh failure means nothing was sent at all; T31's set has no exact reason for
  "not sent", and `connection-closed` is the nearest.

## The AuditEvent

Every attempt (accepted, rejected, no acknowledgment, malformed sample) is recorded once, after
its final outcome, built by T31's `build_send_attempt_audit_event` from values the service
produced or validated and written with `put_resource(AUDIT_FHIR_STORE_ID, "AuditEvent", id, ...)`:
one `PUT .../fhirStores/<audit>/fhir/AuditEvent/<id>`, nothing to any other FHIR store. The
service never accepts or forwards FHIR from a caller.

| Attempt | `outcome` | Details on `entity[1]` |
| --- | --- | --- |
| AA | `0` | `transport`, `MSH-10`, `MSA-1` AA, `ack-message` (base64), `what` = stored message name (HTTPS only) |
| AE | `4` | the same, plus `MSA-3` with the store's reason; no message name |
| no acknowledgment | `8` | `no-ack-reason`, `http-status` for `api-error`, `unreadable-reply-bytes` for `unreadable-reply` |
| any malformed sample | as above | plus `malformed-sample` = the sample name |

If recording fails, the response says `audit: {recorded: false, detail}` and an
`audit-write-failed` line is logged with a short code: `build-failed` (T31 refused a value),
`build-error` (anything else raised while building), `write-failed` (the store answered an error
or did not answer; with its HTTP status), `write-error` (anything else raised while writing).
The send's own result is never changed or hidden, and the request does not fail. A failed
record is not retried: T31's "last write wins" makes a retry safe, but the proxy would have to
resend the request, which would be a new attempt.

## Rate limit

An in-process token bucket: `RATE_LIMIT_BURST` sends back to back, refilled at
`RATE_LIMIT_PER_MINUTE`. Malformed sends count too. Over the limit: 429 with `Retry-After`
(whole seconds until a token is back, rounded up). Checked after input validation (a typo does
not use a token) and before anything is built or sent. The bucket is locked, so gunicorn's
threads share it safely.

**It is per instance.** Each Cloud Run instance has its own bucket, so the service-wide ceiling
is the limit times the instance count; T22 caps instances (`max_instance_count`). A shared limit
would need a shared store that bills while idle.

## Time limits: what T14 and T22 must allow

One request makes one send and one audit write. Each `HealthcareClient` call goes through
`requests`, which applies `HTTPS_TIMEOUT_SECONDS` to the connect and separately to each wait for
response data, so one call normally ends within 2 × `HTTPS_TIMEOUT_SECONDS`. `google-auth` may
first refresh the access token (from the metadata server) and retries a call once after a 401,
which can double that. Neither is a hard bound: a server trickling bytes could stretch a read.

| Transport | Worst case (formula) | With the defaults |
| --- | --- | --- |
| MLLP | `MLLP_TIMEOUT_SECONDS` + audit write (2 × `HTTPS_TIMEOUT_SECONDS`, doubled on a token refresh and 401 retry) | 5 + 20 = 25 s; 45 s with a refresh and retry |
| HTTPS | ingest (2 × `HTTPS_TIMEOUT_SECONDS`, doubled likewise) + audit write (the same) | 20 + 20 = 40 s; 80 s with a refresh and retry on both |

**Numbers to set:** the `cuff-sim` Cloud Run request timeout (T22) at **90 s**, the hard stop
for anything above, and `fhir-proxy`'s forward timeout to this service (T14) at **at least
90 s**, with the proxy's own Cloud Run timeout above it. If Cloud Run does cut a request off,
the caller gets 504 and the attempt may have been sent but is not recorded.

## Who may call it

There is no authentication code here. The Cloud Run service does not allow unauthenticated
calls, and only `fhir-proxy`'s service account has `roles/run.invoker` on it (D10, T22); Cloud
Run checks the proxy's OIDC token before a request reaches the container. There is no health or
info route, so nothing reveals the configuration.

Credentials: Application Default Credentials, on Cloud Run the `cuff-sim` service account:
`roles/healthcare.hl7V2Ingest` on the HL7v2 store (HTTPS transport) and
`roles/healthcare.fhirResourceEditor` on the audit FHIR store only. It has no access to the main
FHIR store and reads no secrets. MLLP needs no IAM, only the network path (D14).

## Configuration

Environment variables, set by Terraform (T22). All are checked at start-up: `ServiceConfig`
checks each value, then `create_app` builds the store names, a synthetic reading message
(validated) and a synthetic AuditEvent from them, offline, so a value that cannot work stops the
service from starting. Every start-up error names the variables and never their values, and is
raised without a chained cause (`from None`): the libraries' own errors quote the value, and a
chained exception would put it into the traceback Cloud Run logs.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `GCP_PROJECT_ID` | yes | | project of the Healthcare dataset (6–30 characters, starting with a letter, for T31) |
| `HEALTHCARE_LOCATION` | yes | | dataset region, e.g. `us-central1` |
| `HEALTHCARE_DATASET_ID` | yes | | dataset id |
| `HL7V2_STORE_ID` | yes | | HL7v2 store id (both transports deliver here) |
| `AUDIT_FHIR_STORE_ID` | yes | | audit FHIR store id, same dataset (D15); must differ from `HL7V2_STORE_ID` |
| `MLLP_HOST` | yes | | the edge VM's private IP address (a host name works, but its lookup has no time limit) |
| `MLLP_PORT` | no | `2575` | adapter port, 1–65535 |
| `MLLP_TIMEOUT_SECONDS` | no | `5` | one deadline for connect, write and the whole ACK frame; above 0, at most 60 (S4 worst case 111 ms) |
| `HTTPS_TIMEOUT_SECONDS` | no | `10` | per `requests` phase of one ingest or audit write; above 0, at most 60 |
| `PATIENT_ID` | yes | | PID-3.1, at most 64 characters; the setup step's patient id (the same value as `v2-to-fhir`'s) |
| `PATIENT_ASSIGNING_AUTHORITY` | yes | | PID-3.4, at most 20 |
| `PATIENT_FAMILY_NAME` | yes | | PID-5.1, at most 80 |
| `PATIENT_GIVEN_NAME` | yes | | PID-5.2, at most 80 |
| `SENDING_APPLICATION` | no | `CUFFSIM` | MSH-3, at most 20 (the mapper's Device name and key include it) |
| `SENDING_FACILITY` | no | `PULSEBRIDGE` | MSH-4, at most 20 |
| `RATE_LIMIT_PER_MINUTE` | no | `6` | refill rate per instance, 1–600 |
| `RATE_LIMIT_BURST` | no | `3` | bucket size per instance, 1–100 |
| `PORT` | no | `8080` | listening port (Cloud Run sets it) |

Patient and sender values must be printable ASCII without a backslash (T07: MSH-18 is empty,
and `\` is HL7's escape character). The length limits are ours, below what hl7apy enforces
(199 characters for PID-3.1 and PID-5, 20 for PID-3.4 and MSH-3/MSH-4), so a long value is
refused by name before hl7apy can raise an error that prints it. Timeouts are plain decimals
(`1e1`, `1_0`, `inf` and `nan` are refused).

## Running

```bash
gunicorn --bind ":${PORT:-8080}" --workers 1 --threads 4 --timeout 0 "cuff_sim.app:create_app()"
```

- For T24's container: install `libs/pulsebridge_fhir`, `libs/pulsebridge_hl7v2` and
  `libs/pulsebridge_gcp` before this package. They are declared by name only and are not on
  PyPI. T07's samples are package data of `pulsebridge_hl7v2` and must be installed with it.
- One process, a few threads: the token bucket is per process, so `--workers 1` keeps "per
  instance" true.
- The client is shared by the threads, as in `v2-to-fhir` (see its README on session sharing).

## Logging

One JSON object per line on stdout.

| Event | Fields |
| --- | --- |
| `cuff-send` (INFO; WARNING if not recorded) | `transport`, `result`, `sample` (a T07 sample name), `framing`, `control_id` (random hex, or a sample's fixed id), `ack_code` (MSA-1), `no_ack_reason`, `http_status`, `message_name_source`, `attempt_id` (the AuditEvent id, a hash), `audit_recorded`, `audit_failure` (code), `audit_http_status`, `duration_ms`, `ack_matches_control_id` (on an accept), `send_error_type` (exception class, only when a send failed unexpectedly) |
| `audit-write-failed` (ERROR) | `failure`, `http_status`, `attempt_id` |
| `request-refused` (WARNING) | `http_status` (400, 413, 422), `fields` (names of the wrong fields) |
| `rate-limited` (WARNING) | `http_status`, `retry_after` |
| `validation-failed` (ERROR) | `problems` (a count) |
| `bug` (ERROR) | `error_type` (the exception class name) |

Never logged: blood-pressure values, patient identifiers or names, message or ACK text, MSA-3
reasons, the stored message name, request bodies, exception messages, server error text. Flask's
own exception logging never runs: the route catches everything and logs the type only.

## Tests

`make test` runs `tests/`. A fake MLLP listener on `127.0.0.1` (in `tests/conftest.py`) replies
with the exact ACK and NACK bytes recorded in spikes S4 and S5, or closes, stays silent,
trickles, resets, or sends oversized, garbage or doubled replies, and records the bytes it
received. A listener with a full backlog gives a real hanging connect for the deadline tests.
HTTPS ingest and the audit write use the real `HealthcareClient` over `FakeTransport`. A seeded
fuzz test feeds thousands of reply shapes through `read_ack` and 150 through the whole app, and
checks that none gives a 5xx or escapes the AuditEvent. All data is synthetic.

## Not verified

This task made no network call beyond the loopback interface and no GCP call.

- **The derived MLLP message name** (above): one observation over HTTPS only.
- **The path from Cloud Run to the adapter** (D14, T21, T22) and the real adapter's behaviour
  under a connection that closes right after the reply.
- **Least-privilege roles**: that `hl7V2Ingest` alone allows `messages:ingest` with labels
  (spike S5 used a broad role).
- **Timing under load**: the worst-case figures above are derived from the libraries'
  documented behaviour, not measured.
