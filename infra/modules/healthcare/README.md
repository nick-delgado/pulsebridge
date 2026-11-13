# Module `healthcare`

The Cloud Healthcare API side of PulseBridge: one **dataset** holding a **FHIR R4 store** and an
**HL7v2 store**, plus the Pub/Sub path that tells `v2-to-fhir` when an HL7v2 message has arrived.

```
cuff-sim ──MLLP──> edge VM adapter ──┐
cuff-sim ──HTTPS messages:ingest─────┴─> HL7v2 store ──notification──> ingest topic
                                                                          │
                       dead-letter topic <──after N failed deliveries── ingest subscription
                              │                                           │ push + OIDC token
                     dead-letter pull subscription                        v
                                                                      v2-to-fhir ──> FHIR R4 store
health-sync ─────────────────────────────────────────────────────────────────────> FHIR R4 store
```

- A **dataset** is a regional container in the Healthcare API. It holds stores and decides where
  the data physically lives (`location`). Deleting it deletes everything inside.
- A **FHIR store** is a managed FHIR server: it speaks the FHIR REST API
  (`GET Observation?patient=...`, `PUT`, transaction `Bundle`s) and is the system of record.
- An **HL7v2 store** receives HL7v2 messages (over the MLLP adapter or the REST `messages:ingest`
  call), replies with the HL7 ACK, keeps each accepted message, and publishes a notification.
  Our own `v2-to-fhir` code turns those messages into FHIR resources in the FHIR store.

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
  push_service_account_email = module.identity.service_account_emails["pubsub-push"]

  store_iam_service_accounts = {
    health_sync = module.identity.service_account_emails["health-sync"]
    v2_to_fhir  = module.identity.service_account_emails["v2-to-fhir"]
    fhir_proxy  = module.identity.service_account_emails["fhir-proxy"]
    edge_vm     = module.identity.service_account_emails["edge-vm"]
    cuff_sim    = module.identity.service_account_emails["cuff-sim"]
  }
}
```

The output names of `module.ingestion` and `module.identity` above are illustrative; T20, T04 and
T25 settle them.

## FHIR store settings

Several of these cannot be changed after the store is created; changing them here would replace
the store and **delete its data**.

| Setting | Value | Why | What the other value would do |
| --- | --- | --- | --- |
| `version` | `R4` | Every writer, the validator (T08) and OpenEMR use R4. | Immutable. |
| `enable_update_create` | `true` | Writers are idempotent on client-derived keys (D8, T06). Update-create lets a `PUT Observation/<id>`, or a conditional update whose body carries an `id`, create the resource when it does not exist yet. A plain conditional update (`PUT Observation?identifier=...` with no `id` in the body) works either way. | With `false`, the first `PUT` to an ID the store has not seen fails, for example the first sync after a nightly dev rebuild. **Rule for writers:** client-chosen IDs appear in audit logs and Pub/Sub, so they must be opaque (a hash), never a patient identifier. |
| `disable_referential_integrity` | `false` (integrity on) | The store refuses a write that references a resource that does not exist, so an Observation can never point at a missing Patient or Device. | With integrity off, a typo in a reference is stored silently and `Patient/$everything` misses resources. **Consequence for writers:** inside a transaction Bundle all entries are resolved together, so entry order does not matter, but references to resources created in the same Bundle must use the entry's `fullUrl` (`urn:uuid:...`). Outside a transaction, write Patient and Device before the Observation, and the Observation before its Provenance. Immutable. |
| `disable_resource_versioning` | `false` (versioning on) | Each update keeps the old version (`_history`). `health-sync` rewrites overlapping windows every run; history shows how a partly synced window changed. About 1 KB per version. | No history; `_history` reads return errors. Immutable. |
| `enable_history_import` | `false` | Only for bulk-importing past versions, which nothing here does. | An import could create versions that appear to have happened in the past. |
| `complex_data_type_reference_parsing` | `ENABLED` | References inside complex types (for example in extensions) are checked by referential integrity too. Set explicitly because Google plans to change the default for new stores. | `DISABLED`: such references are not checked. Turning it on later can fail on existing data. |
| `default_search_handling_strict` | `true` | An unknown search parameter is an error. | Lenient (the FHIR default) ignores it: `Observation?dat=2026-11-01` (a typo) would return every Observation. |
| `notification_configs` | none | Nothing consumes FHIR change events; the dashboard reads through `fhir-proxy`. | About 300 notifications a day to a topic nobody reads. |
| `validation_config` | defaults | Required-field, reference-type, FHIRPath and base-profile validation all stay on. | |

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
time whether to create the token-creator binding, and a Cloud Run URL is unknown until that
service exists. In one env root the whole stack applies in one run: only the subscription depends
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
| Pub/Sub service agent | `roles/iam.serviceAccountTokenCreator` | the push service account only | Mints the push OIDC token. Only when `push_enabled` and `grant_pubsub_token_creator`. |

**Application service accounts** (emails from module identity, each optional, keys of
`store_iam_service_accounts`). The narrowest predefined Healthcare roles:

| Key (account) | Role | Resource | Needed for |
| --- | --- | --- | --- |
| `health_sync` | `roles/healthcare.fhirResourceEditor` | FHIR store | Conditional updates in a transaction Bundle |
| `v2_to_fhir` | `roles/healthcare.fhirResourceEditor` | FHIR store | Writing Patient, Device, Observation, Provenance |
| `v2_to_fhir` | `roles/healthcare.hl7V2Editor` | HL7v2 store | `messages.get` and `messages.patch` (outcome labels, S5). Consumer has no patch; Editor is the narrowest predefined role with it, and also allows create, delete and ingest. A custom role with get + update would be narrower. |
| `fhir_proxy` | `roles/healthcare.fhirResourceReader` | FHIR store | Search and read for the dashboard and share |
| `fhir_proxy` | `roles/healthcare.hl7V2Consumer` | HL7v2 store | `messages.list` filtered by label, `messages.get` for the trace |
| `edge_vm` | `roles/healthcare.hl7V2Ingest` | HL7v2 store | MLLP adapter `messages:ingest` (S4) |
| `cuff_sim` | `roles/healthcare.hl7V2Ingest` | HL7v2 store | HTTPS ingest transport |

None of these least-privilege roles has been exercised against the API yet: spikes S4 and S5 used
broadly privileged accounts. Confirm each on the first apply (T25/T29).

The account that runs `terraform apply` needs `iam.serviceAccounts.actAs` on the push service
account to create a push subscription with its OIDC token (T02's deployer account).

## Cost and destroy

- The Healthcare API bills per request and per GB stored; Pub/Sub bills per message volume.
  **Nothing here is billed by the hour.** Topic retention and retained acknowledged messages,
  which are billed, are not used; keeping unacknowledged messages in a subscription is free.
- `deletion_policy = "DELETE"` (default) lets `terraform destroy` delete the dataset and both
  stores together with every stored resource and message, so a destroyed dev stack leaves nothing
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
- **Token-creator grant**: needed only in projects created before 8 April 2021; granted on the one
  account by default because it is narrow.
- **Service agents exist** once the APIs are enabled (module project-base). If an apply runs
  before the agent is created, the IAM binding fails; re-running the apply fixes it.
- **Least-privilege roles** in the table above are untested.
- **Parser V1 vs V2/V3**: only V1 was exercised.

## Tests

`terraform test` in this directory runs the files under `tests/` against a mocked Google provider:
no credentials, nothing created.

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

## Resources

| Name | Type |
|------|------|
| [google_healthcare_dataset.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/healthcare_dataset) | resource |
| [google_healthcare_fhir_store.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/healthcare_fhir_store) | resource |
| [google_healthcare_fhir_store_iam_member.app](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/healthcare_fhir_store_iam_member) | resource |
| [google_healthcare_hl7_v2_store.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/healthcare_hl7_v2_store) | resource |
| [google_healthcare_hl7_v2_store_iam_member.app](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/healthcare_hl7_v2_store_iam_member) | resource |
| [google_pubsub_subscription.dead_letter](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/pubsub_subscription) | resource |
| [google_pubsub_subscription.ingest](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/pubsub_subscription) | resource |
| [google_pubsub_subscription_iam_member.pubsub_agent_ingest_subscriber](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/pubsub_subscription_iam_member) | resource |
| [google_pubsub_topic.dead_letter](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/pubsub_topic) | resource |
| [google_pubsub_topic.ingest](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/pubsub_topic) | resource |
| [google_pubsub_topic_iam_member.healthcare_agent_publisher](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/pubsub_topic_iam_member) | resource |
| [google_pubsub_topic_iam_member.pubsub_agent_dead_letter_publisher](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/pubsub_topic_iam_member) | resource |
| [google_service_account_iam_member.pubsub_agent_token_creator](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/service_account_iam_member) | resource |
| [google_project.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/data-sources/project) | data source |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_ack_deadline_seconds"></a> [ack\_deadline\_seconds](#input\_ack\_deadline\_seconds) | How long Pub/Sub waits for v2-to-fhir to answer a push before treating it as failed and retrying. 60 s covers a Cloud Run cold start plus a FHIR write; the Pub/Sub default of 10 s does not. | `number` | `60` | no |
| <a name="input_deletion_policy"></a> [deletion\_policy](#input\_deletion\_policy) | What `terraform destroy` (or a replacing apply) may do to the dataset and the two stores.<br/>"DELETE" (default) deletes them together with every stored FHIR resource and HL7v2 message,<br/>so a destroyed dev stack leaves nothing billable. "PREVENT" makes destroy fail instead, which<br/>suits prod where the data is real. The setting is read from state, so switching from PREVENT<br/>to DELETE needs an apply before the destroy. "ABANDON" is not offered: it would leave the<br/>stores, and their storage bill, behind without Terraform knowing. | `string` | `"DELETE"` | no |
| <a name="input_grant_pubsub_token_creator"></a> [grant\_pubsub\_token\_creator](#input\_grant\_pubsub\_token\_creator) | Grant the Pub/Sub service agent roles/iam.serviceAccountTokenCreator on the push service account only, so it can mint the OIDC token. Google adds an equivalent grant automatically only in projects created after 8 April 2021; the per-account grant is narrow and works in both. Only applies when push\_enabled is true. | `bool` | `true` | no |
| <a name="input_hl7v2_parser_version"></a> [hl7v2\_parser\_version](#input\_hl7v2\_parser\_version) | Version of the HL7v2 store's built-in (unschematized) parser, which fills the stored message's parsedData. Cannot be changed after the store is created (spike S4). V1 is the version spikes S4 and S5 tested; see the README before changing it. | `string` | `"V1"` | no |
| <a name="input_labels"></a> [labels](#input\_labels) | Labels added to the stores, topics and subscriptions (the dataset resource has no labels). | `map(string)` | `{}` | no |
| <a name="input_location"></a> [location](#input\_location) | Cloud Healthcare API location for the dataset and both stores (a region such as "us-central1", or a multi-region such as "us"). Stored health data stays in this location. | `string` | `"us-central1"` | no |
| <a name="input_max_delivery_attempts"></a> [max\_delivery\_attempts](#input\_max\_delivery\_attempts) | Delivery attempts before a notification is moved to the dead-letter topic. v2-to-fhir acknowledges mapping errors itself (D7), so only infrastructure failures retry; 10 attempts with the default backoff span roughly 40 minutes, enough to ride out a cold start or a short API outage. | `number` | `10` | no |
| <a name="input_message_retention_duration"></a> [message\_retention\_duration](#input\_message\_retention\_duration) | How long the ingest and dead-letter subscriptions keep unacknowledged notifications (a duration such as "604800s" = 7 days, Pub/Sub's default). Retaining unacknowledged messages is not billed. | `string` | `"604800s"` | no |
| <a name="input_name_prefix"></a> [name\_prefix](#input\_name\_prefix) | Prefix for every resource name in this module (dataset, stores, topics, subscriptions). Lets two stacks share one project without colliding. | `string` | `"pulsebridge"` | no |
| <a name="input_project_id"></a> [project\_id](#input\_project\_id) | ID of the GCP project that holds the Healthcare dataset and the Pub/Sub topics. Comes from the env root; never hardcoded. | `string` | n/a | yes |
| <a name="input_push_audience"></a> [push\_audience](#input\_push\_audience) | Audience (aud claim) of the OIDC token. null (default) uses the endpoint's origin, scheme and host only, which is the Cloud Run service URL that Cloud Run checks tokens against. | `string` | `null` | no |
| <a name="input_push_enabled"></a> [push\_enabled](#input\_push\_enabled) | Turn the ingest subscription into an OIDC-authenticated push subscription to v2-to-fhir.<br/>false (default): the subscription exists as a pull subscription and simply holds<br/>notifications (up to the retention period) until push is switched on, so nothing is lost<br/>while the v2-to-fhir service does not exist yet. true: push\_endpoint and<br/>push\_service\_account\_email are required. This is a plain bool, not derived from<br/>push\_endpoint, so Terraform knows at plan time which resources to create even when the<br/>endpoint is a Cloud Run URL that is only known after apply. | `bool` | `false` | no |
| <a name="input_push_endpoint"></a> [push\_endpoint](#input\_push\_endpoint) | HTTPS URL Pub/Sub posts each notification to: the v2-to-fhir Cloud Run service URL plus its handler path (from module ingestion, T20). Required when push\_enabled is true. | `string` | `null` | no |
| <a name="input_push_service_account_email"></a> [push\_service\_account\_email](#input\_push\_service\_account\_email) | Service account Pub/Sub signs the push request's OIDC token as. It needs roles/run.invoker on the v2-to-fhir service (granted by module ingestion). Required when push\_enabled is true. | `string` | `null` | no |
| <a name="input_retry_maximum_backoff"></a> [retry\_maximum\_backoff](#input\_retry\_maximum\_backoff) | Longest wait between redeliveries of one notification (a duration such as "600s"). | `string` | `"600s"` | no |
| <a name="input_retry_minimum_backoff"></a> [retry\_minimum\_backoff](#input\_retry\_minimum\_backoff) | Shortest wait before Pub/Sub redelivers a notification v2-to-fhir did not acknowledge (a duration such as "10s"). Waits grow exponentially up to retry\_maximum\_backoff. | `string` | `"10s"` | no |
| <a name="input_store_iam_service_accounts"></a> [store\_iam\_service\_accounts](#input\_store\_iam\_service\_accounts) | Service account emails (from module identity) to grant store-level roles to. Each key is<br/>optional; leave a key out and no binding is made for it. Allowed keys and what they get<br/>(see the README table):<br/>  health\_sync -> FHIR store: roles/healthcare.fhirResourceEditor<br/>  v2\_to\_fhir  -> FHIR store: roles/healthcare.fhirResourceEditor;<br/>                 HL7v2 store: roles/healthcare.hl7V2Editor (get message, patch its labels)<br/>  fhir\_proxy  -> FHIR store: roles/healthcare.fhirResourceReader;<br/>                 HL7v2 store: roles/healthcare.hl7V2Consumer (list and get messages)<br/>  edge\_vm     -> HL7v2 store: roles/healthcare.hl7V2Ingest (MLLP adapter)<br/>  cuff\_sim    -> HL7v2 store: roles/healthcare.hl7V2Ingest (HTTPS ingest transport)<br/>A map rather than an object with optional attributes: the keys are written literally by the<br/>caller, so Terraform knows at plan time which bindings exist even while the emails are<br/>still unknown (first apply, accounts not yet created). | `map(string)` | `{}` | no |
| <a name="input_time_zone"></a> [time\_zone](#input\_time\_zone) | IANA time zone of the Healthcare dataset. The API uses it to read HL7v2 times that carry no UTC offset (for example an OBX-14 without '+hhmm'). UTC is the safe default; the cuff simulator sends explicit offsets anyway. | `string` | `"UTC"` | no |

## Outputs

| Name | Description |
|------|-------------|
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
