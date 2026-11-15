# pulsebridge_gcp

A thin client for the parts of the Cloud Healthcare REST API (v1) PulseBridge uses, shared by
`v2-to-fhir` (T12) and later by `health-sync`, the cuff simulator, `fhir-proxy` and the seeder.
It is plumbing: one method per REST call, typed results, typed errors, no hidden retries.

Unlike the other libraries in `libs/`, this one talks to GCP, but only through an injected
**transport**, so its tests (and every service's tests) run with a fake one: no network, no
credentials.

## Use

```python
from pulsebridge_gcp import HealthcareClient, default_transport

client = HealthcareClient(
    project=os.environ["GCP_PROJECT_ID"],      # from configuration, never from code
    location=os.environ["HEALTHCARE_LOCATION"],
    dataset=os.environ["HEALTHCARE_DATASET_ID"],
    transport=default_transport(),             # Application Default Credentials
)
message = client.get_message(name, view="RAW_ONLY")   # name: from the Pub/Sub notification
client.execute_bundle("fhir-store-id", bundle)
client.patch_labels(name, {"application_outcome": "accepted", "reason": "mapped"})
```

| Method | REST call | Returns |
| --- | --- | --- |
| `execute_bundle(fhir_store, bundle)` | `POST …/fhirStores/<s>/fhir` (`application/fhir+json`) | response Bundle (dict) |
| `read_resource(fhir_store, type, id)` | `GET …/fhir/<type>/<id>` | resource (dict) |
| `search(fhir_store, type, params)` | `GET …/fhir/<type>?<params>`; `params` a mapping, or `(name, value)` pairs for repeats such as `[("date", "ge2026-11-01"), ("date", "lt2026-11-08")]` | one `searchset` page (dict) |
| `capabilities(fhir_store)` | `GET …/fhir/metadata` | the store's CapabilityStatement (dict) |
| `next_page(fhir_store, bundle)` | the Bundle's `link[relation=next]` | next page, or `None` |
| `put_resource(fhir_store, type, id, resource)` | `PUT …/fhir/<type>/<id>` | stored resource (dict) |
| `get_message(name, view="RAW_ONLY")` | `GET <name>?view=…` | `Hl7v2Message` |
| `list_messages(store, filter=, order_by=, page_size=, page_token=, view="BASIC")` | `GET …/hl7V2Stores/<s>/messages` | `MessagePage` |
| `iter_messages(store, max_pages=100, **same)` | the above, following `nextPageToken` | iterator of `Hl7v2Message` |
| `patch_labels(name, labels)` | `PATCH <name>?updateMask=labels` | `Hl7v2Message` |
| `ingest(store, data, labels=None)` | `POST …/hl7V2Stores/<s>/messages:ingest` | `IngestResult(hl7_ack, message)` |
| `is_message_in_store(name, store)` | none | whether `name` is a message of that store |

- **Several FHIR stores, one client.** Every FHIR method takes the store id, so one client
  serves every store in its dataset: for example the health-data store and the separate audit
  store for send-attempt AuditEvents (decision D15), `client.put_resource("audit", "AuditEvent",
  id, event)` and `client.search("audit", "AuditEvent", {...})` (tested).
- `Hl7v2Message`: `name`, `data` (the exact bytes, base64-decoded; `None` in views without it),
  `message_type` (MSH-9.1), `send_facility` (MSH-4), `send_time` (MSH-7), `create_time`,
  `patient_ids` (each PID-3 as `PatientId(value, type)`), `labels`, `raw`.
- `IngestResult.hl7_ack` is the commit-level ACK in ER7: no MLLP frame, no trailing `\r`
  (spike S5). Parse it with `pulsebridge_hl7v2.parse_ack`.
- Everything that ends up in a URL path (store ids, FHIR types and ids, message names) is
  checked against a pattern before a request is built, so a value from outside cannot address
  another resource. `next_page` only follows links inside the same FHIR store, so a response
  cannot send the access token elsewhere.
- `patch_labels` and `ingest` check labels against the documented limits (at most 64; keys
  1-63 of `[a-z0-9_-]` starting with a letter; values 0-63 of `[a-z0-9_-]`). Spike S5 found
  the store does not enforce them today; we keep to them anyway.

## Errors

| Error | When | Usual response |
| --- | --- | --- |
| `NotFoundError` | 404 | the thing is gone; stop |
| `PermissionDeniedError` | 401, 403 | IAM or configuration, often still propagating; retry later |
| `ConflictError` (a `ClientError`) | 409 | FHIR: concurrent update, retry. HL7v2 ingest: duplicate, with `hl7_nack` |
| `ClientError` | other 4xx | refused as sent; do not resend unchanged. Carries `operation_outcome` (FHIR) or `hl7_nack` (HL7v2 ingest NACK, decoded bytes) |
| `RetryableError` | 408, 429, 5xx, no response at all, token refresh failure | retry later |
| `TimedOutError` (a `RetryableError`, `status` `None`) | no response within the timeout (connect or read), T33 | retry later; after a read timeout the request may have been processed. Tell it from other network failures with `isinstance`, not by reading `__cause__` |
| `MalformedResponseError` | 2xx with a body that is not what the API documents | bug or contract change |

**For callers mapping results to "no acknowledgment, with the HTTP status" (T13's HTTPS
ingest):** 401, 403 and 404 raise `PermissionDeniedError` / `NotFoundError`, not `ClientError`,
so they carry no `hl7_nack`; handle them (and `RetryableError`) alongside a `ClientError` whose
`hl7_nack` is `None`. Only a `ClientError`/`ConflictError` with `hl7_nack` set is a NACK.

All are `HealthcareApiError` with `operation` and `status`. `str(error)` names only the
operation and status: the server's own text can quote the request, which here is health data,
so it is kept on the exception (`server_message`, `operation_outcome`) for the caller to log
selectively. `operation_outcome_summary(outcome)` keeps each issue's `severity`, `code` and
`expression`/`location` and drops `diagnostics` and `details.text`.

## Transports and tests

```python
class Transport(Protocol):
    def request(self, method, url, *, params, body, headers, timeout) -> HttpResponse: ...
```

- `RequestsTransport(session)` wraps a `requests.Session`; `default_transport()` builds one from
  `google.auth.default()` and `google.auth.transport.requests.AuthorizedSession`, which adds the
  access token and refreshes it. On Cloud Run the credentials are the service's own service
  account from the metadata server; no key file exists.
- `pulsebridge_gcp.testing.FakeTransport` scripts responses by method and path suffix and
  records every request. An unscripted request fails the test.

```python
transport = FakeTransport()
transport.add("GET", "/messages/abc=", json_response(200, {"name": name, "data": "..."}))
client = HealthcareClient(project="demo", location="l", dataset="d", transport=transport)
```

## Dependency choices

| Package | Pin | Why |
| --- | --- | --- |
| `google-auth` | `2.62.0` | Application Default Credentials and token refresh. The official library; nothing else should touch tokens. |
| `requests` | `2.34.2` | `google-auth` ships its authorised session (`AuthorizedSession`) for `requests`, including the refresh-and-retry on 401. Using another HTTP client would mean writing that again. Synchronous, which suits a handler that makes three calls in a row. |

Not used: the generated `google-cloud-healthcare` / `googleapiclient` discovery clients. They are
large, hide the REST shapes spikes S4 and S5 recorded, and would still need wrapping to give
typed errors with the OperationOutcome and NACK.

Timeouts: 15 s per request by default. `v2-to-fhir` makes three requests per push and Pub/Sub
waits 60 s for the answer (T05 `ack_deadline_seconds`).

## Not verified against the real API

Nothing in this task called GCP. Shapes come from spike S5 (HL7v2) and the FHIR REST
specification. Unconfirmed: the `messages.list` response key `hl7V2Messages`; that FHIR errors on
`executeBundle` carry an `OperationOutcome` body (handled either way: a Google `{"error": …}`
envelope also parses).
