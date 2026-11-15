# Module `healthcare`

The Cloud Healthcare API side of PulseBridge: one **dataset** holding a **FHIR R4 store** for
health data, a second **audit FHIR R4 store** for the cuff simulator's send-attempt records
(D15), and an **HL7v2 store**, plus the Pub/Sub path that tells `v2-to-fhir` when an HL7v2
message has arrived.

```
cuff-sim ──MLLP──> edge VM adapter ──┐
cuff-sim ──HTTPS messages:ingest─────┴─> HL7v2 store ──notification──> ingest topic
                                                                          │
                       dead-letter topic <──after N failed deliveries── ingest subscription
                              │                                           │ push + OIDC token
                     dead-letter pull subscription                        v
                                                                      v2-to-fhir ──> FHIR R4 store
health-sync ─────────────────────────────────────────────────────────────────────> FHIR R4 store
cuff-sim ──PUT AuditEvent/<id> (one per send attempt, D12, D15)──────────> audit FHIR R4 store
```

- A **dataset** is a regional container in the Healthcare API. It holds stores and decides where
  the data physically lives (`location`). Deleting it deletes everything inside.
- A **FHIR store** is a managed FHIR server: it speaks the FHIR REST API
  (`GET Observation?patient=...`, `PUT`, transaction `Bundle`s) and is the system of record.
- An **HL7v2 store** receives HL7v2 messages (over the MLLP adapter or the REST `messages:ingest`
  call), replies with the HL7 ACK, keeps each accepted message, and publishes a notification.
  Our own `v2-to-fhir` code turns those messages into FHIR resources in the FHIR store.
- The **audit FHIR store** is a second FHIR server in the same dataset that holds only
  `AuditEvent`s: one per cuff send attempt, recording what was sent and which acknowledgment
  came back (D12). It is separate so that the cuff simulator, which writes those records, has
  no access at all to the store with health data (D15; see "IAM").

## Usage

```hcl
module "healthcare" {
  source     = "../../modules/healthcare"
  project_id = var.project_id
  location   = var.region

  # Dev is destroyed nightly; prod keeps its data.
  deletion_policy = "DELETE"

  # Switch push on once v2-to-fhir exists (module ingestion, T20). Using its URL here does not
  # create a cycle: only the subscription depends on it, not the stores ingestion reads.
  push_enabled               = true
  push_endpoint              = "${module.ingestion.v2_to_fhir_url}/pubsub/hl7v2"
  # Referencing module identity's output makes push wait for identity, which also owns the
  # Pub/Sub agent's token-creator grant on this account (see "IAM").
  push_service_account_email = module.identity.service_account_emails["pubsub-push"]

  # Module identity's map as it is: this module uses the keys it knows and ignores the rest.
  store_iam_service_accounts = module.identity.service_account_emails
}
```

`module.identity.service_account_emails` is module identity's output (T04). The
`module.ingestion` output name above is illustrative; T20 and T25 settle it.

## FHIR store settings (health data)

Several of these cannot be changed after the store is created; changing them here would replace
the store and **delete its data**.

| Setting | Value | Why | What the other value would do |
| --- | --- | --- | --- |
| `version` | `R4` | Every writer, the validator (T08) and OpenEMR use R4. | Immutable. |
| `enable_update_create` | `true` | Writers are idempotent on client-derived keys (D8, T06). Update-create lets a `PUT Observation/<id>`, or a conditional update whose body carries an `id`, create the resource when it does not exist yet. A plain conditional update (`PUT Observation?identifier=...` with no `id` in the body) works either way. | With `false`, the first `PUT` to an ID the store has not seen fails, for example the first sync after a nightly dev rebuild. **Rule for writers:** client-chosen IDs appear in audit logs and Pub/Sub, so they must be opaque (a hash), never a patient identifier. |
| `disable_referential_integrity` | `false` (integrity on) | The store refuses a write that references a resource that does not exist, so an Observation can never point at a missing Patient or Device. | With integrity off, a typo in a reference is stored silently and `Patient/$everything` misses resources. **Consequence for writers:** inside a transaction Bundle all entries are resolved together, so entry order does not matter. A reference to a resource created in the same Bundle may use the entry's `fullUrl` (`urn:uuid:...`) or a plain `Patient/<id>` when that entry is a `PUT` to a client-chosen id (from knowledge of the API; confirm on first apply). Outside a transaction, write Patient and Device before the Observation, and the Observation before its Provenance. **Deletes go referrers first:** deleting an Observation fails while a Provenance targets it, and deleting a Patient fails while Observations reference it, so cleanup or regeneration (T09, manual fixes) deletes Provenance, then Observation, then Patient. `$purge` only removes old versions and does not get around this. Destroying the whole store or dataset is unaffected. Immutable. |
| `disable_resource_versioning` | `false` (versioning on) | Each update keeps the old version (`_history`). `health-sync` rewrites overlapping windows every run; history shows how a partly synced window changed. About 1 KB per version. | No history; `_history` reads return errors. Immutable. |
| `enable_history_import` | `false` | Only for bulk-importing past versions, which nothing here does. | An import could create versions that appear to have happened in the past. |
| `complex_data_type_reference_parsing` | `ENABLED` | References inside complex types (for example in extensions) are checked by referential integrity too. Set explicitly because Google plans to change the default for new stores. | `DISABLED`: such references are not checked. Turning it on later can fail on existing data. |
| `default_search_handling_strict` | `true` | An unknown search parameter is an error. | Lenient (the FHIR default) ignores it: `Observation?dat=2026-11-01` (a typo) would return every Observation. |
| `notification_configs` | none | Nothing consumes FHIR change events; the dashboard reads through `fhir-proxy`. | About 300 notifications a day to a topic nobody reads. |
| `validation_config` | defaults | Required-field, reference-type, FHIRPath and base-profile validation all stay on. | |

## Audit FHIR store settings

The audit store (`google_healthcare_fhir_store.audit`, named `<name_prefix>-fhir-audit` unless
`audit_fhir_store_name` is set) holds only the `AuditEvent`s that T31 builds and the cuff
simulator writes. Its settings are chosen for that job; where they match the main store, the
reason is the same.

| Setting | Value | Why |
| --- | --- | --- |
| `version` | `R4` | T31's builder and the validator (T08) are R4. Immutable. |
| `enable_update_create` | `true` | The simulator writes `PUT AuditEvent/<id>` with an ID derived from the attempt (T31), so writing the same attempt twice leaves one resource. With `false`, every first `PUT` to a new ID would fail. |
| `disable_referential_integrity` | `false` (integrity on) | **Referential integrity** means the store refuses a resource whose reference points at something that does not exist. The AuditEvent names the patient and the services with **identifier-only logical references**: `Reference.identifier` (a system and value), with no `Reference.reference` such as `Patient/<id>`. The store does not try to resolve those, so integrity costs nothing. It stays on as a guard: if a literal reference to a health-data resource were ever added, the write would be refused, because that resource lives in the other store. Immutable. |
| `disable_resource_versioning` | `false` (versioning on) | An attempt may first be recorded as "no acknowledgment" and later overwritten with the same ID once the outcome is known. Versioning keeps the earlier record readable with `_history`, so the overwrite does not erase what was first recorded. Immutable. |
| `enable_history_import` | `false` | Nothing imports past versions, and none may be fabricated. |
| `complex_data_type_reference_parsing` | `ENABLED` | Same as the main store: set explicitly ahead of Google's planned default change. |
| `default_search_handling_strict` | `true` | Same as the main store: a mistyped search parameter in the messages log query (T14) is an error instead of returning every AuditEvent. |
| `notification_configs` | none | Nothing reacts to new AuditEvents; `fhir-proxy` reads them on request. |
| `validation_config` | defaults | All validation on. |
| `deletion_policy`, `labels` | the module's variables | Same lifecycle as the main store: destroyed with the dataset under `"DELETE"`, protected under `"PREVENT"`. |

## HL7v2 store settings

| Setting | Value | Why |
| --- | --- | --- |
| `parser_config.version` | `V1` (variable `hl7v2_parser_version`) | Chooses the parser that fills a stored message's `parsedData`. **Immutable after creation** (spike S4: the API refuses the change). `v2-to-fhir` parses the raw bytes with `hl7apy` (spike S5), so `parsedData` is not used. V1 is the parser whose ingest and NACK behaviour spikes S4 and S5 recorded and that the D7 malformed-message samples rely on. V2 and V3 mainly change `parsedData` (V1 numbers MSH fields one lower than HL7); they were not tested. |
| `parser_config.allow_null_header` | `false` (default) | A message without MSH is rejected with `AE` "Invalid MSH header", one of the D7 demo cases. |
| `parser_config.segment_terminator` | unset (`\r`) | HL7v2 segments end in carriage return; `\n` is rejected with `AE` (D7 "unparseable"). |
| `parser_config.schema` | unset | No schematized parsing; nothing would read it. |
| `reject_duplicate_message` | `false` (default) | Resending identical bytes returns `AA`, stores nothing new and publishes no second notification (S4, S5). `true` would turn a harmless resend into `AE` "message already exists". |
| `notification_configs` | one, to the ingest topic, **no filter** | Spike S5: one notification per newly stored message from `ingest` or `create`; `data` is the stored message's resource name, the only attribute is `msgType`. A `message_type = "ORU"` filter would stop messages with an empty MSH-9 from reaching the mapper, and D7 wants those to end as an application-level `error`. |

## Notification delivery

1. The HL7v2 store stores a message and publishes its resource name to the **ingest topic**, as
   the Cloud Healthcare service agent.
2. The **ingest subscription** delivers it. With `push_enabled = true` Pub/Sub `POST`s it to
   `push_endpoint` with `Authorization: Bearer <OIDC token>` signed as
   `push_service_account_email`; the audience is the endpoint's origin (the Cloud Run service URL)
   unless `push_audience` is set. Cloud Run accepts the call only if that account has
   `roles/run.invoker` on `v2-to-fhir` (granted by module ingestion).
3. `v2-to-fhir` answers 2xx to acknowledge. It acknowledges mapping errors too (D7), recording
   them as labels on the stored message (S5), so only infrastructure failures cause a retry.
4. Retries back off exponentially from `retry_minimum_backoff` (10 s) to `retry_maximum_backoff`
   (600 s). After `max_delivery_attempts` (10, roughly 40 minutes) the Pub/Sub service agent moves
   the notification to the **dead-letter topic**, whose **pull subscription** keeps it for
   inspection. Without a dead-letter topic a poison notification would be retried for the whole
   7-day retention, and Pub/Sub slows push delivery to the whole subscription while it sees
   failures.

**Breaking the ordering with module ingestion.** The `v2-to-fhir` service is created later (T20)
and needs this module's store IDs, while the subscription needs the service's URL. The
subscription therefore always exists, and `push_config` is added only when `push_enabled` is
true. Before that it is a pull subscription nobody reads, so notifications wait in it for up to 7
days and are pushed once push is switched on; changing pull to push is an in-place update. The
switch is a plain bool rather than "is `push_endpoint` set", because Terraform must know at plan
time whether to add the push config, and a Cloud Run URL is unknown until that service exists. In one env root the whole stack applies in one run: only the subscription depends
on the URL, not the stores.

Both subscriptions have `expiration_policy { ttl = "" }`: by default Pub/Sub deletes a
subscription after 31 days without activity, which would silently break an idle demo.

## IAM

All bindings are resource-level and additive (`*_iam_member`); none is project-level.

**Service agents** (Google-managed accounts, named from the project number):

| Account | Role | Resource | Why |
| --- | --- | --- | --- |
| Cloud Healthcare service agent `service-<number>@gcp-sa-healthcare` | `roles/pubsub.publisher` | ingest topic | Publishes the HL7v2 store's notifications. The store depends on this binding: creating a store whose topic it cannot publish to fails with 403 (S5). |
| Pub/Sub service agent `service-<number>@gcp-sa-pubsub` | `roles/pubsub.publisher` | dead-letter topic | Forwards failed notifications. |
| Pub/Sub service agent | `roles/pubsub.subscriber` | ingest subscription | Acknowledges the original once it is dead-lettered. |

**Not in this module: the push token grant.** To sign the push request's OIDC token, the
Pub/Sub service agent needs `roles/iam.serviceAccountTokenCreator` on the push service account.
Module identity (T04) is the single owner of that grant: if two modules managed the same IAM
member, destroying either would delete the other's grant. In the root, passing
`module.identity.service_account_emails["pubsub-push"]` as `push_service_account_email` makes
the push switch depend on module identity, so the grant is applied first. IAM changes take a
little while to propagate, so the first pushes after switching on may fail and be retried;
with the retry policy above that costs a few delivery attempts, not messages.
`tests/check_ordering.sh` fails if a token-creator grant appears in this module.

**Application service accounts** (module identity's map, passed as it is to
`store_iam_service_accounts`). Every key is optional, and keys not listed here (for example
`scheduler-invoker`, `pubsub-push`) are ignored. One role table in `iam.tf` (`store_role_matrix`)
drives every binding below; each is the narrowest predefined Healthcare role for the job, and
each column says plainly what the role allows, reading included.

*Main FHIR store (health data):*

| Key (account) | Role | Allows | Needed for |
| --- | --- | --- | --- |
| `health-sync` | `roles/healthcare.fhirResourceEditor` | Create, read, search, update and delete any resource in the store | Conditional updates in a transaction Bundle |
| `seeder` | `roles/healthcare.fhirResourceEditor` | Same as above | Synthetic-data seeder (T09), same write pattern as health-sync. Nothing on the other stores. Present only when module identity creates that account. |
| `v2-to-fhir` | `roles/healthcare.fhirResourceEditor` | Same as above | Writing Patient, Device, Observation, Provenance |
| `fhir-proxy` | `roles/healthcare.fhirResourceReader` | Read and search any resource; no writes | Search and read for the dashboard and share |

`cuff-sim`, `edge-vm` and every other account have **nothing** on this store.

*Audit FHIR store (send-attempt AuditEvents, D15):*

| Key (account) | Role | Allows | Needed for |
| --- | --- | --- | --- |
| `cuff-sim` | `roles/healthcare.fhirResourceEditor` | Create, read, search, update and delete any resource **in the audit store** | Writing one `AuditEvent` per send attempt with `PUT AuditEvent/<id>` (D12, T13, T31). It can also read, search and delete the audit records, which it does not need; Google's role list shows no predefined FHIR role that writes without reading. |
| `fhir-proxy` | `roles/healthcare.fhirResourceReader` | Read and search any resource in the audit store; no writes | The messages log (T14, T19) |

No other account has anything on this store.

*HL7v2 store:*

| Key (account) | Role | Allows | Needed for |
| --- | --- | --- | --- |
| `v2-to-fhir` | `roles/healthcare.hl7V2Consumer` | List and read messages, update their labels, create messages | `messages.get` and `messages.patch` with `updateMask=labels` (outcome labels, S5). Google's IAM reference describes Consumer as "list and read HL7v2 messages, update message labels, and publish new messages", which covers `healthcare.hl7V2Messages.update` (the permission `messages.patch` needs). Found through search-result summaries of that page, not by reading the role directly; the first apply must confirm the label patch. Editor would also allow delete and ingest. |
| `fhir-proxy` | `roles/healthcare.hl7V2Consumer` | Same as above | `messages.list` filtered by label, `messages.get` for the trace. Consumer also lets it patch labels and create messages, which the proxy does not need; no narrower predefined role covers list + get, so a custom role would be the next step. |
| `edge-vm` | `roles/healthcare.hl7V2Ingest` | Ingest messages only | MLLP adapter `messages:ingest` (S4) |
| `cuff-sim` | `roles/healthcare.hl7V2Ingest` | Ingest messages only | HTTPS ingest transport |

**Why a separate audit store (D15).** The cuff simulator must write an `AuditEvent` for every
send attempt (D12). It writes, rather than `fhir-proxy`, because it holds the raw ACK and because
the proxy is the one public service (D10) and should stay read-only. The simple option was to give
the simulator `fhirResourceEditor` on the main store. Healthcare IAM roles apply to a whole store
(from Google's IAM documentation, not tested here), so that would have let the simulator read,
search and delete every resource there, including the author's real heart-rate data. That is far
more than recording a send attempt needs. A custom role would not solve it either: it could
leave out read and delete, but it still could not limit writes to one resource type, so the
simulator could still create or overwrite Observations and Patients. A second store makes the
store itself the boundary. The simulator's editor role reaches only audit records, and it has no
binding on the health-data store.

What remains: on the audit store the simulator can also read, search and delete audit records,
so a compromised simulator could alter the messages log. It cannot touch health data. Two further
mitigations are meant to apply: the simulator will not be publicly invocable, and only
`fhir-proxy` may call it (enforced in T22, D10); and it builds each AuditEvent itself from
validated values and never forwards caller-supplied FHIR (T13). A custom role on the audit store
without read and delete would be the next tightening.

Which permissions a `PUT` that creates a resource (update-create) checks is **unconfirmed**:
it is expected to be covered by `fhirResourceEditor` (create and update), but not tested
against the API. Confirm on the first apply (T25/T29).

**What the tests prove, and what they do not.** `tests/iam.tftest.hcl` compares the whole table
of bindings on each of the three stores, store IDs included. It checks that `cuff-sim` alone gets
exactly the audit-store editor and HL7v2 ingest bindings, that `fhir-proxy` is exactly reader on
both FHIR stores, and that no other workload appears on the audit store. Those tests only see the
role-table-driven resources. So `tests/check_store_iam.sh` reads the source and fails if the
module declares any other Healthcare store or dataset IAM resource (including `_iam_binding` and
`_iam_policy`), any project-level IAM, or a child module. Together they cover this module's role
table and its declared resources. They say nothing about grants made outside this module, such
as a project-level role given by hand or by another module.

None of these least-privilege roles has been exercised against the API yet: spikes S4 and S5 used
broadly privileged accounts. Confirm each on the first apply (T25/T29).

The account that runs `terraform apply` needs `iam.serviceAccounts.actAs` on the push service
account to create a push subscription with its OIDC token (T02's deployer account).

## Cost and destroy

- The Healthcare API bills per request and per GB stored; Pub/Sub bills per message volume.
  **Nothing here is billed by the hour.** Topic retention and retained acknowledged messages
  are not used. Unacknowledged messages older than a day can be billed as storage; at a few
  hundred small notifications this is negligible, and retention is capped at 7 days (Pub/Sub
  allows 31) to keep it that way.
- The audit FHIR store adds no hourly charge either: it is billed only for the AuditEvents it
  stores (a few KB each) and the requests made to it.
- `deletion_policy = "DELETE"` (default) lets `terraform destroy` delete the dataset and all
  three stores (the audit store included) together with every stored resource and message, so a destroyed dev stack leaves nothing
  billable. Topics and subscriptions are always deleted.
- Use `"PREVENT"` in prod: destroy then fails instead of deleting real data. The value is read
  from state, so going back to `"DELETE"` needs an apply before the destroy. `"ABANDON"` is
  rejected by validation, because it would leave the stores and their storage bill behind.

## Assumptions not yet verified

From spike S5's "What was not verified" and this module's own choices:

- **Topic-level binding needed?** S5 could not show it is necessary (the project already grants
  the Healthcare agent `roles/healthcare.serviceAgent`). It is declared anyway so the module does
  not rely on that default.
- **Push delivery, OIDC, retry and dead-letter behaviour** are from Pub/Sub documentation; S5 had
  no endpoint. First confirmation comes with T20/T29.
- **Service agents exist** once the APIs are enabled (module project-base). If an apply runs
  before the agent is created, the IAM binding fails; re-running the apply fixes it.
- **Least-privilege roles** in the IAM tables above are untested, and so is which permission an update-create `PUT` checks.
- **Parser V1 vs V2/V3**: only V1 was exercised.

## Tests

`terraform test` in this directory runs the files under `tests/` against a mocked Google provider:
no credentials, nothing created.

`terraform test` cannot see `depends_on`, which module owns a grant, or IAM resources outside
the role table, so two static scripts check those. `tests/check_store_iam.sh` reads the `.tf`
files only (no init needed) and fails if the module declares any Healthcare store or dataset IAM
resource other than the three role-table-driven ones (`google_healthcare_fhir_store_iam_member`
`app` and `audit`, `google_healthcare_hl7_v2_store_iam_member` `app`), if one of those uses a
different `for_each` or store, or if the module declares project-level IAM or a child module.
`tests/check_ordering.sh` checks the rest. It needs no credentials after `terraform init -backend=false`, and fails if:

- any `.tf` file here manages a service account's IAM or grants
  `roles/iam.serviceAccountTokenCreator` (module identity owns that grant);
- `terraform graph` lacks either ordering edge: HL7v2 store -> the Healthcare service agent's
  publisher binding on the ingest topic, and ingest subscription -> the Pub/Sub service
  agent's publisher binding on the dead-letter topic.

Run every Terraform command for this module with Google credentials removed from the
environment (no `GOOGLE_APPLICATION_CREDENTIALS`, no project variables, no gcloud config), so a
mock that fails to apply can never reach a real project.

```bash
terraform init -backend=false   # once
tests/check_ordering.sh         # from this directory, or pass the module path as $1
tests/check_store_iam.sh        # same
```

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.11 |
| <a name="requirement_google"></a> [google](#requirement\_google) | ~> 8.11 |

## Providers

| Name | Version |
|------|---------|
| <a name="provider_google"></a> [google](#provider\_google) | 8.11.0 |

## Modules

No modules.

## Resources

| Name | Type |
|------|------|
| [google_healthcare_dataset.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/healthcare_dataset) | resource |
| [google_healthcare_fhir_store.audit](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/healthcare_fhir_store) | resource |
| [google_healthcare_fhir_store.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/healthcare_fhir_store) | resource |
| [google_healthcare_fhir_store_iam_member.app](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/healthcare_fhir_store_iam_member) | resource |
| [google_healthcare_fhir_store_iam_member.audit](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/healthcare_fhir_store_iam_member) | resource |
| [google_healthcare_hl7_v2_store.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/healthcare_hl7_v2_store) | resource |
| [google_healthcare_hl7_v2_store_iam_member.app](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/healthcare_hl7_v2_store_iam_member) | resource |
| [google_pubsub_subscription.dead_letter](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/pubsub_subscription) | resource |
| [google_pubsub_subscription.ingest](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/pubsub_subscription) | resource |
| [google_pubsub_subscription_iam_member.pubsub_agent_ingest_subscriber](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/pubsub_subscription_iam_member) | resource |
| [google_pubsub_topic.dead_letter](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/pubsub_topic) | resource |
| [google_pubsub_topic.ingest](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/pubsub_topic) | resource |
| [google_pubsub_topic_iam_member.healthcare_agent_publisher](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/pubsub_topic_iam_member) | resource |
| [google_pubsub_topic_iam_member.pubsub_agent_dead_letter_publisher](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/pubsub_topic_iam_member) | resource |
| [google_project.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/data-sources/project) | data source |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_ack_deadline_seconds"></a> [ack\_deadline\_seconds](#input\_ack\_deadline\_seconds) | How long Pub/Sub waits for v2-to-fhir to answer a push before treating it as failed and retrying. 60 s covers a Cloud Run cold start plus a FHIR write; the Pub/Sub default of 10 s does not. | `number` | `60` | no |
| <a name="input_audit_fhir_store_name"></a> [audit\_fhir\_store\_name](#input\_audit\_fhir\_store\_name) | Name (which is also the ID) of the second FHIR R4 store, which holds only the AuditEvents<br/>that record cuff send attempts (D15). null (default) gives "<name\_prefix>-fhir-audit".<br/>Must differ from the main FHIR store's name, "<name\_prefix>-fhir". Changing it replaces<br/>the store and deletes its records. | `string` | `null` | no |
| <a name="input_deletion_policy"></a> [deletion\_policy](#input\_deletion\_policy) | What `terraform destroy` (or a replacing apply) may do to the dataset and its three stores<br/>(FHIR, audit FHIR, HL7v2).<br/>"DELETE" (default) deletes them together with every stored FHIR resource and HL7v2 message,<br/>so a destroyed dev stack leaves nothing billable. "PREVENT" makes destroy fail instead, which<br/>suits prod where the data is real. The setting is read from state, so switching from PREVENT<br/>to DELETE needs an apply before the destroy. "ABANDON" is not offered: it would leave the<br/>stores, and their storage bill, behind without Terraform knowing. | `string` | `"DELETE"` | no |
| <a name="input_hl7v2_parser_version"></a> [hl7v2\_parser\_version](#input\_hl7v2\_parser\_version) | Version of the HL7v2 store's built-in (unschematized) parser, which fills the stored message's parsedData. Cannot be changed after the store is created (spike S4). V1 is the version spikes S4 and S5 tested; see the README before changing it. | `string` | `"V1"` | no |
| <a name="input_labels"></a> [labels](#input\_labels) | Labels added to the stores, topics and subscriptions (the dataset resource has no labels). | `map(string)` | `{}` | no |
| <a name="input_location"></a> [location](#input\_location) | Cloud Healthcare API location for the dataset and all its stores (a region such as "us-central1", or a multi-region such as "us"). Stored health data stays in this location. | `string` | `"us-central1"` | no |
| <a name="input_max_delivery_attempts"></a> [max\_delivery\_attempts](#input\_max\_delivery\_attempts) | Delivery attempts before a notification is moved to the dead-letter topic. v2-to-fhir acknowledges mapping errors itself (D7), so only infrastructure failures retry; 10 attempts with the default backoff span roughly 40 minutes, enough to ride out a cold start or a short API outage. | `number` | `10` | no |
| <a name="input_message_retention_duration"></a> [message\_retention\_duration](#input\_message\_retention\_duration) | How long the ingest and dead-letter subscriptions keep unacknowledged notifications (a duration such as "604800s" = 7 days, Pub/Sub's default). Pub/Sub allows up to 31 days; this module caps it at 7 days as a cost choice, since unacknowledged messages older than a day can be billed as storage (negligible at this volume, but not zero). | `string` | `"604800s"` | no |
| <a name="input_name_prefix"></a> [name\_prefix](#input\_name\_prefix) | Prefix for every resource name in this module (dataset, stores, topics, subscriptions). Lets two stacks share one project without colliding. | `string` | `"pulsebridge"` | no |
| <a name="input_project_id"></a> [project\_id](#input\_project\_id) | ID of the GCP project that holds the Healthcare dataset and the Pub/Sub topics. Comes from the env root; never hardcoded. | `string` | n/a | yes |
| <a name="input_push_audience"></a> [push\_audience](#input\_push\_audience) | Audience (aud claim) of the OIDC token. null (default) uses the endpoint's origin, scheme and host only, which is the Cloud Run service URL that Cloud Run checks tokens against. | `string` | `null` | no |
| <a name="input_push_enabled"></a> [push\_enabled](#input\_push\_enabled) | Turn the ingest subscription into an OIDC-authenticated push subscription to v2-to-fhir.<br/>false (default): the subscription exists as a pull subscription and simply holds<br/>notifications (up to the retention period) until push is switched on, so nothing is lost<br/>while the v2-to-fhir service does not exist yet. true: push\_endpoint and<br/>push\_service\_account\_email are required. This is a plain bool, not derived from<br/>push\_endpoint, so Terraform knows at plan time which resources to create even when the<br/>endpoint is a Cloud Run URL that is only known after apply. | `bool` | `false` | no |
| <a name="input_push_endpoint"></a> [push\_endpoint](#input\_push\_endpoint) | HTTPS URL Pub/Sub posts each notification to: the v2-to-fhir Cloud Run service URL plus its handler path (from module ingestion, T20). Required when push\_enabled is true. | `string` | `null` | no |
| <a name="input_push_service_account_email"></a> [push\_service\_account\_email](#input\_push\_service\_account\_email) | Service account Pub/Sub signs the push request's OIDC token as: module identity's "pubsub-push" account. It needs roles/run.invoker on v2-to-fhir (module ingestion), and the Pub/Sub service agent needs roles/iam.serviceAccountTokenCreator on it, which module identity grants. Required when push\_enabled is true. | `string` | `null` | no |
| <a name="input_retry_maximum_backoff"></a> [retry\_maximum\_backoff](#input\_retry\_maximum\_backoff) | Longest wait between redeliveries of one notification (a duration such as "600s"). | `string` | `"600s"` | no |
| <a name="input_retry_minimum_backoff"></a> [retry\_minimum\_backoff](#input\_retry\_minimum\_backoff) | Shortest wait before Pub/Sub redelivers a notification v2-to-fhir did not acknowledge (a duration such as "10s"). Waits grow exponentially up to retry\_maximum\_backoff. | `string` | `"10s"` | no |
| <a name="input_store_iam_service_accounts"></a> [store\_iam\_service\_accounts](#input\_store\_iam\_service\_accounts) | Module identity's map of account name to service account email, passed as it is<br/>(store\_iam\_service\_accounts = module.identity.service\_account\_emails). Keys this module<br/>knows get store-level roles (see the README table); every key is optional, and keys it does<br/>not know (for example scheduler-invoker, pubsub-push) are ignored:<br/>  health-sync -> FHIR store: roles/healthcare.fhirResourceEditor<br/>  seeder      -> FHIR store: roles/healthcare.fhirResourceEditor<br/>  v2-to-fhir  -> FHIR store: roles/healthcare.fhirResourceEditor;<br/>                 HL7v2 store: roles/healthcare.hl7V2Consumer (get message, patch its labels)<br/>  fhir-proxy  -> FHIR store and audit FHIR store: roles/healthcare.fhirResourceReader;<br/>                 HL7v2 store: roles/healthcare.hl7V2Consumer (list and get messages)<br/>  edge-vm     -> HL7v2 store: roles/healthcare.hl7V2Ingest (MLLP adapter)<br/>  cuff-sim    -> audit FHIR store: roles/healthcare.fhirResourceEditor (AuditEvents, D15);<br/>                 HL7v2 store: roles/healthcare.hl7V2Ingest (HTTPS ingest transport);<br/>                 nothing on the main FHIR store<br/>Bindings are chosen by key name only, so Terraform knows at plan time which exist even<br/>while the emails are still unknown (first apply, accounts not yet created). | `map(string)` | `{}` | no |
| <a name="input_time_zone"></a> [time\_zone](#input\_time\_zone) | IANA time zone of the Healthcare dataset. The API uses it to read HL7v2 times that carry no UTC offset (for example an OBX-14 without '+hhmm'). UTC is the safe default; the cuff simulator sends explicit offsets anyway. | `string` | `"UTC"` | no |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_audit_fhir_base_url"></a> [audit\_fhir\_base\_url](#output\_audit\_fhir\_base\_url) | FHIR REST base URL of the audit store (append e.g. /AuditEvent/<id>). The cuff simulator writes here (T13); fhir-proxy reads the messages log from here (T14). |
| <a name="output_audit_fhir_store_id"></a> [audit\_fhir\_store\_id](#output\_audit\_fhir\_store\_id) | Full resource path of the audit FHIR R4 store (send-attempt AuditEvents, D15): projects/{p}/locations/{l}/datasets/{d}/fhirStores/{s}. |
| <a name="output_audit_fhir_store_name"></a> [audit\_fhir\_store\_name](#output\_audit\_fhir\_store\_name) | Short name of the audit FHIR store. |
| <a name="output_dataset_id"></a> [dataset\_id](#output\_dataset\_id) | Full resource path of the dataset: projects/{project}/locations/{location}/datasets/{dataset}. |
| <a name="output_dataset_name"></a> [dataset\_name](#output\_dataset\_name) | Short dataset name (MLLP adapter flag --hl7\_v2\_dataset\_id). |
| <a name="output_dead_letter_subscription_name"></a> [dead\_letter\_subscription\_name](#output\_dead\_letter\_subscription\_name) | Pull subscription that keeps dead-lettered notifications for inspection. |
| <a name="output_dead_letter_topic_id"></a> [dead\_letter\_topic\_id](#output\_dead\_letter\_topic\_id) | Full path of the dead-letter topic for notifications v2-to-fhir could not handle. |
| <a name="output_dead_letter_topic_name"></a> [dead\_letter\_topic\_name](#output\_dead\_letter\_topic\_name) | Short name of the dead-letter topic. |
| <a name="output_fhir_base_url"></a> [fhir\_base\_url](#output\_fhir\_base\_url) | FHIR REST base URL of the store (append e.g. /Observation?patient=...). Used by health-sync, v2-to-fhir and fhir-proxy. |
| <a name="output_fhir_store_id"></a> [fhir\_store\_id](#output\_fhir\_store\_id) | Full resource path of the FHIR R4 store: projects/{p}/locations/{l}/datasets/{d}/fhirStores/{s}. |
| <a name="output_fhir_store_name"></a> [fhir\_store\_name](#output\_fhir\_store\_name) | Short FHIR store name. |
| <a name="output_healthcare_service_agent_email"></a> [healthcare\_service\_agent\_email](#output\_healthcare\_service\_agent\_email) | Cloud Healthcare service agent that publishes the store's notifications. |
| <a name="output_hl7v2_ingest_url"></a> [hl7v2\_ingest\_url](#output\_hl7v2\_ingest\_url) | REST URL for direct HTTPS ingest (POST {"message": {"data": <base64 ER7>}}), used by the cuff simulator's HTTPS transport. |
| <a name="output_hl7v2_store_id"></a> [hl7v2\_store\_id](#output\_hl7v2\_store\_id) | Full resource path of the HL7v2 store: projects/{p}/locations/{l}/datasets/{d}/hl7V2Stores/{s}. Prefix of every stored message name. |
| <a name="output_hl7v2_store_name"></a> [hl7v2\_store\_name](#output\_hl7v2\_store\_name) | Short HL7v2 store name (MLLP adapter flag --hl7\_v2\_store\_id). |
| <a name="output_ingest_subscription_id"></a> [ingest\_subscription\_id](#output\_ingest\_subscription\_id) | Full path of the ingest subscription (push to v2-to-fhir when push\_enabled): projects/{p}/subscriptions/{s}. Appears as "subscription" in every push body. |
| <a name="output_ingest_subscription_name"></a> [ingest\_subscription\_name](#output\_ingest\_subscription\_name) | Short name of the ingest subscription. |
| <a name="output_ingest_topic_id"></a> [ingest\_topic\_id](#output\_ingest\_topic\_id) | Full path of the topic the HL7v2 store publishes ingest notifications to: projects/{p}/topics/{t}. |
| <a name="output_ingest_topic_name"></a> [ingest\_topic\_name](#output\_ingest\_topic\_name) | Short name of the ingest notification topic. |
| <a name="output_location"></a> [location](#output\_location) | Healthcare API location of the dataset and stores (MLLP adapter flag --hl7\_v2\_location\_id). |
| <a name="output_mllp_adapter_store_flags"></a> [mllp\_adapter\_store\_flags](#output\_mllp\_adapter\_store\_flags) | The four store coordinates the MLLP adapter takes as flags (spike S4), keyed by flag name without the leading dashes. |
| <a name="output_project_id"></a> [project\_id](#output\_project\_id) | Project that holds the dataset and topics (echoed so consumers take every coordinate from one place). |
| <a name="output_pubsub_service_agent_email"></a> [pubsub\_service\_agent\_email](#output\_pubsub\_service\_agent\_email) | Pub/Sub service agent that dead-letters and signs push OIDC tokens. |
| <a name="output_push_enabled"></a> [push\_enabled](#output\_push\_enabled) | Whether the ingest subscription currently pushes to v2-to-fhir. |
<!-- END_TF_DOCS -->
