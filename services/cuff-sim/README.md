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
    mllp_client.py     MLLP: connect, write, read one frame with one deadline, close
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
| `systolic`, `diastolic` | JSON integers (not `120.0`, `"120"` or `true`), each 1 to 400 mm[Hg], systolic above diastolic: T07's builder limits. Required unless `sendMalformed` is set (then ignored, but checked if present). |
| `transport` | `"mllp"` (default when absent) or `"https"` |
| `sendMalformed` | `null`, or the name of a sample from `GET /cuff/samples` that lists the chosen transport |
| anything else | refused |

The ranges are T07's, as the brief asks. The dashboard proposes narrower ones (`CUFF_BOUNDS`,
60–250 / 30–150) and the mapper (T12) accepts systolic 50–300 and diastolic 20–200: a reading
between the mapper's range and T07's is stored with `AA` and then gets the application-level
outcome `out-of-range`, which is one of the D7 layers the demo can show.

**No caller text reaches a message.** The only strings accepted select fixed content (a
transport, a sample name from T07's manifest). The patient, the names and MSH-3/MSH-4 come from
configuration.

Responses:

| Status | When | Body |
| --- | --- | --- |
| 200 | a send happened, whatever the store said | the result below |
| 400 | body is not a JSON object | OperationOutcome |
| 422 | a field is wrong (bounds, type, unknown sample, sample not for this transport) | OperationOutcome; `expression` names the field; nothing sent |
| 429 | rate-limited | OperationOutcome, `Retry-After: <seconds>`; nothing sent |
| 500 | a bug, including a built message failing validation (nothing sent) | OperationOutcome with a fixed text |

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
`null`; and the field is `sendMalformed` (the contract's spelling), where the work plan says
`send_malformed`.

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

The sample's exact bytes from T07's loader, **not validated**. Over MLLP the sample's own
framing is kept (`wire_bytes`), so `noack_missing_end_block` really is sent without its end
bytes. Over HTTPS only samples whose manifest lists `https` are accepted. The control ID is the
sample's own MSH-10 read with hl7apy, or `null` when hl7apy cannot read the sample (no MSH,
line-feed terminators). The samples carry T07's synthetic patient `SYN-000123`, not the
configured one (see "Not verified").

### MLLP

`socket.create_connection` to `MLLP_HOST:MLLP_PORT` (private VPC path, D14; no TLS at this
layer), `sendall` of the frame, then T07's `MllpReader.read_frame` with one deadline
(`MLLP_ACK_TIMEOUT_SECONDS`) for the whole reply and a 4096-byte cap. A new connection per
send, always closed.

| On the socket | `ack` |
| --- | --- |
| an ACK frame | `received: true`, parsed by `parse_ack` (AA, or AE with MSA-3) |
| closed with no data (S4: the adapter's API call failed) | `why: connection-closed` |
| closed after part of a frame, or reset | `why: connection-closed` |
| nothing before the deadline (S4: bad framing), or bytes trickling | `why: timeout` |
| a frame that `parse_ack` refuses, or over 4096 bytes | `why: unreadable-reply` (first bytes kept when there are any) |
| connection refused, host not reachable or not resolvable | `why: connection-closed` |
| connecting or writing does not finish in `MLLP_CONNECT_TIMEOUT_SECONDS` | `why: timeout` |

MSA-2 is not required to match on a NACK (the store leaves it empty when it could not read the
header), and the reply type `ACK` (not `ACK^R01`) is what the store sends. On an accepting ACK a
different MSA-2 is logged (`ack_matches_control_id: false`), not treated as a failure.

**The stored message's name over MLLP is DERIVED.** The ACK does not carry it. Spike S5 observed,
on one message ingested over HTTPS, that the id after `/messages/` is the URL-safe base64 of the
SHA-256 of the message bytes. For an accepted (AA) MLLP send the service returns
`<store>/messages/<urlsafe-b64(sha256(bytes inside the frame))>` with
`messageNameSource: "derived-sha256-unverified"`. It is **unverified** beyond that one message
and for the MLLP path; confirm on the first deployment (T25) by listing the store. It is not
written into the AuditEvent (T31 takes the name from the ingest response only).

### HTTPS

`pulsebridge_gcp.HealthcareClient.ingest(store, bytes, labels={"transport": "https"})`. The
label lets the messages log tell the transports apart (spike S5; the adapter cannot set labels).

| Response | `ack` |
| --- | --- |
| 200 with `hl7Ack` | `received: true`, parsed by the same `parse_ack` (the store's AA); `messageName` from the response, `messageNameSource: "ingest-response"` |
| 400 or 409 with `error.details[].hl7Nack` | `received: true`, the NACK (AE) with MSA-3: an answer, not a transport failure |
| 400/409 without `hl7Nack`, 401, 403, 404, 408, 429, 5xx | `why: api-error`, `httpStatus` |
| no HTTP response (network failure, token refresh failure) | `why: connection-closed` (below) |
| 200 whose `hl7Ack` is missing, not base64, or not an ACK | `why: unreadable-reply` |

The ACK bytes over HTTPS have no MLLP frame and no trailing `\r`; `parse_ack` does not need
one.

**A network failure has no HTTP status**, and T31's closed set has no reason for it
(`api-error` requires 400–599). It is mapped to `connection-closed`: the exchange ended with no
reply of any kind, which is how a refused or reset MLLP connection is reported too. `timeout`
was rejected because `pulsebridge_gcp` reports every failure with no response as one
`RetryableError` without saying whether it was a timeout, and reading that from the exception
text would tie this mapping to the transport's internals.

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

If recording fails (a FHIR input refusal, or the write returns an error or no response), the
response says `audit: {recorded: false, detail}` and an `audit-write-failed` line is logged with
a short code and HTTP status. The send's own result is never changed or hidden, and the request
does not fail. A failed record is not retried: T31's "last write wins" makes a retry safe, but
the proxy would have to resend the request, which would be a new attempt.

## Rate limit

An in-process token bucket: `RATE_LIMIT_BURST` sends back to back, refilled at
`RATE_LIMIT_PER_MINUTE`. Malformed sends count too. Over the limit: 429 with `Retry-After`
(whole seconds until a token is back). Checked after input validation (a typo does not use a
token) and before anything is built or sent.

**It is per instance.** Each Cloud Run instance has its own bucket, so the service-wide ceiling
is the limit times the instance count; T22 caps instances (`max_instance_count`). A shared limit
would need a shared store that bills while idle.

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

Environment variables, set by Terraform (T22). All are checked at start-up: `create_app`
builds the store names, a synthetic reading message (validated) and a synthetic AuditEvent from
them, offline, so a value that cannot work stops the service from starting. Errors name the
variable, never its value.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `GCP_PROJECT_ID` | yes | | project of the Healthcare dataset |
| `HEALTHCARE_LOCATION` | yes | | dataset region, e.g. `us-central1` |
| `HEALTHCARE_DATASET_ID` | yes | | dataset id |
| `HL7V2_STORE_ID` | yes | | HL7v2 store id (both transports deliver here) |
| `AUDIT_FHIR_STORE_ID` | yes | | audit FHIR store id, same dataset (D15); must differ from `HL7V2_STORE_ID` |
| `MLLP_HOST` | yes | | the edge VM's private address or host name |
| `MLLP_PORT` | no | `2575` | adapter port, 1–65535 |
| `MLLP_CONNECT_TIMEOUT_SECONDS` | no | `3` | connect and write, above 0 and at most 60 |
| `MLLP_ACK_TIMEOUT_SECONDS` | no | `5` | the whole ACK frame (S4 worst case 111 ms) |
| `HTTPS_TIMEOUT_SECONDS` | no | `10` | one ingest or audit write |
| `PATIENT_ID` | yes | | PID-3.1; the setup step's patient id (the same value as `v2-to-fhir`'s) |
| `PATIENT_ASSIGNING_AUTHORITY` | yes | | PID-3.4 |
| `PATIENT_FAMILY_NAME` | yes | | PID-5.1 |
| `PATIENT_GIVEN_NAME` | yes | | PID-5.2 |
| `SENDING_APPLICATION` | no | `CUFFSIM` | MSH-3 (the mapper's Device name and key include it) |
| `SENDING_FACILITY` | no | `PULSEBRIDGE` | MSH-4 |
| `RATE_LIMIT_PER_MINUTE` | no | `6` | refill rate per instance, 1–600 |
| `RATE_LIMIT_BURST` | no | `3` | bucket size per instance, 1–100 |
| `PORT` | no | `8080` | listening port (Cloud Run sets it) |

Patient and sender values must be printable ASCII without a backslash (T07). `GCP_PROJECT_ID`
must be a real-shaped project id (6–30 characters, starting with a letter) for T31.

## Running

```bash
gunicorn --bind ":${PORT:-8080}" --workers 1 --threads 4 --timeout 0 "cuff_sim.app:create_app()"
```

- For T24's container: install `libs/pulsebridge_fhir`, `libs/pulsebridge_hl7v2` and
  `libs/pulsebridge_gcp` before this package. They are declared by name only and are not on
  PyPI. T07's samples are package data of `pulsebridge_hl7v2` and must be installed with it.
- One process, a few threads: the token bucket is per process, so `--workers 1` keeps "per
  instance" true. Each thread waits at most the MLLP timeouts or the HTTPS timeout per send.
- The client is shared by the threads, as in `v2-to-fhir` (see its README on session sharing).

## Logging

One JSON object per line on stdout.

| Event | Fields |
| --- | --- |
| `cuff-send` (INFO; WARNING if not recorded) | `transport`, `result`, `sample` (a T07 sample name), `framing`, `control_id` (random hex, or a sample's fixed id), `ack_code` (MSA-1), `no_ack_reason`, `http_status`, `message_name_source`, `attempt_id` (the AuditEvent id, a hash), `audit_recorded`, `audit_failure` (code), `audit_http_status`, `duration_ms`, `ack_matches_control_id` (on an accept) |
| `audit-write-failed` (ERROR) | `failure`, `http_status`, `attempt_id` |
| `request-refused` (WARNING) | `http_status`, `fields` (names of the wrong fields) |
| `rate-limited` (WARNING) | `http_status`, `retry_after` |
| `validation-failed` (ERROR) | `problems` (a count) |
| `bug` (ERROR) | `error_type` (the exception class name) |

Never logged: blood-pressure values, patient identifiers or names, message or ACK text, MSA-3
reasons, the stored message name, request bodies, exception messages, server error text. Flask's
own exception logging never runs: the route catches everything and logs the type only.

## Tests

`make test` runs `tests/`. A fake MLLP listener on `127.0.0.1` (in `tests/conftest.py`) replies
with the exact ACK and NACK bytes recorded in spikes S4 and S5, or closes, stays silent,
trickles, resets, or sends oversized or garbage replies, and records the bytes it received.
HTTPS ingest and the audit write use the real `HealthcareClient` over `FakeTransport`. All data
is synthetic.

## Not verified

This task made no network call beyond the loopback interface and no GCP call.

- **The derived MLLP message name** (above): one observation over HTTPS only.
- **The sample patient.** T07's malformed samples carry `SYN-000123`; a deployment whose
  `PATIENT_ID` differs will see the mapper end parseable samples with `unknown-patient` (if it
  checks the patient first) rather than the sample's own reason, and the AuditEvent names the
  configured patient. A per-deployment sample patient would need T07 to build samples from a
  template.
- **The path from Cloud Run to the adapter** (D14, T21, T22) and the real adapter's behaviour
  under a connection that closes right after the reply.
- **Least-privilege roles**: that `hl7V2Ingest` alone allows `messages:ingest` with labels
  (spike S5 used a broad role).
