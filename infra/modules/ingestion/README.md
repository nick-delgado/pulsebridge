# Module `ingestion`

The compute that brings data into the FHIR store: the **`v2-to-fhir`** Cloud Run service (the
cuff path, always deployed), and for the wearable path either the **`health-sync`** Cloud Run
service (real Google Health data) or the **seeder** Cloud Run job (synthetic data), each with
its **Cloud Scheduler** trigger, plus **log-based alerting** on the wearable path and on
Scheduler's own attempts.

```
                      module healthcare                         this module
HL7v2 store ──notification──> ingest topic ──push, OIDC as pubsub-push──> v2-to-fhir  /pubsub/hl7v2
                                                                           │ (always)
Cloud Scheduler, every 15 min ──OIDC as scheduler-invoker──> health-sync  /sync     ┐ use_synthetic_data
fhir-proxy (module phr) ───────OIDC as fhir-proxy─────────> health-sync  /status   ┘ = false
                                                                │
     service and Scheduler log lines ──> log-based metrics ──> alert policies (if alert_email or channels)

Cloud Scheduler, hourly ──OAuth as scheduler-invoker──> Cloud Run Admin API jobs.run ─┐ use_synthetic_data
                                                         seeder job: pulsebridge-seed --hours 4 ┘ = true

Scheduler jobs exist only when ingest_enabled = true.
```

## Terms used here

- **Cloud Run service**: a container Google runs for you behind an HTTPS URL. It starts
  **instances** (copies of the container) when requests arrive and stops them when idle. With
  `min_instance_count = 0` it **scales to zero**: no instance and no charge while nothing calls it.
- **Revision**: each deploy of a service creates an immutable revision (image + settings).
  Cloud Run sends traffic to the new revision only once it is **Ready**, which means the
  container started and opened its port.
- **Cloud Run job**: a container that runs to completion and exits, started on demand (an
  **execution**). No URL, no port; billed only while it runs.
- **Ingress**: which *network sources* may reach a service at all, checked before anything else.
  "Internal" means only Google-internal and VPC sources, not the public internet.
- **Invoker**: who may *call* a service (IAM role `roles/run.invoker`, permission
  `run.routes.invoke`). Ingress and invoker are two separate locks: a request must pass both.
- **OIDC ID token** vs **OAuth access token**: Scheduler and Pub/Sub can attach a short-lived
  token proving "this request comes from service account X". An *ID token* (OIDC) is what
  Cloud Run services check; an *access token* (OAuth) is what Google APIs, such as the Cloud Run
  Admin API that starts a job, check. No key file is involved: Google mints both.
- **Image by digest**: `…/v2-to-fhir@sha256:<64 hex>` names the image's exact bytes. A tag
  (`:v1`) is a movable pointer; deploying by tag could deploy something nobody tested.
- **Log-based metric**: a counter Cloud Logging increments for every log entry matching a filter.
  **Alert policy**: a Cloud Monitoring rule that notifies a **notification channel** (email, SMS…)
  when a metric crosses a threshold.
- **`count` / `for_each`**: Terraform's way of creating a resource zero, one or several times.
  `count = var.x ? 1 : 0` means "exists only when x is true"; it is how the two switches below
  make whole resources appear and disappear.

## Usage

How T25's env root wires it (output names are the real ones of modules identity and healthcare):

```hcl
module "ingestion" {
  source     = "../../modules/ingestion"
  project_id = var.project_id
  region     = var.region

  use_synthetic_data = var.use_synthetic_data
  ingest_enabled     = var.ingest_enabled # false on the first apply; see "First run"

  # By digest, as the image build prints them (services/README.md, "Container images").
  v2_to_fhir_image  = var.v2_to_fhir_image
  health_sync_image = var.health_sync_image # null with synthetic data
  seed_image        = var.seed_image        # null with real data

  healthcare_project_id = module.healthcare.project_id
  healthcare_location   = module.healthcare.location
  healthcare_dataset_id = module.healthcare.dataset_name
  fhir_store_id         = module.healthcare.fhir_store_name
  hl7v2_store_id        = module.healthcare.hl7v2_store_name

  patient_id = var.patient_id # SYN-… exactly when use_synthetic_data
  time_zone  = var.time_zone  # e.g. "America/New_York"

  # Module identity's maps, passed whole; this module picks the keys it needs.
  service_account_emails = module.identity.service_account_emails
  secret_ids             = module.identity.secret_ids

  # Set at least one, or the alerts are metrics only and nobody is told.
  alert_email           = var.alert_email                 # creates an email channel
  notification_channels = var.alert_notification_channels # existing channels, may be []

  # The secret accessor grants (module identity) must exist before health-sync deploys.
  depends_on = [module.identity]
}

module "healthcare" {
  # ...
  push_enabled               = module.ingestion.ingest_enabled
  push_endpoint              = module.ingestion.v2_to_fhir_push_endpoint
  push_service_account_email = module.identity.service_account_emails["pubsub-push"]
}

module "identity" {
  # ...
  create_seeder_account = var.use_synthetic_data
}
```

Keep the Healthcare dataset in the same project (`module.healthcare.project_id ==
var.project_id`): Pub/Sub push counts as internal traffic only from the same project, so a
validation refuses another project while `v2_to_fhir_ingress` is internal-only.

There is no dependency cycle: this module reads only the *names* of the stores (known from
module healthcare's variables), and module healthcare reads this module's URL only for the
subscription's push config. `depends_on = [module.identity]` is safe because module identity
depends on nothing.

## What each resource is, and why

| Resource | Exists when | What and why |
| --- | --- | --- |
| `google_cloud_run_v2_service.v2_to_fhir` | always | The HL7v2-to-FHIR mapper (T12). Pub/Sub pushes each new message's name to `POST /pubsub/hl7v2`. Runs as `v2-to-fhir`; environment from the variables (see "Environment"); scale to zero, at most `v2_to_fhir_max_instances` (2); request timeout 60 s, Pub/Sub's ack deadline. |
| `google_cloud_run_v2_service.health_sync` | `use_synthetic_data = false` | The Google Health puller (T10). Runs as `health-sync`; the three Google Health secrets mounted as files; `min 0`, **`max 1`** (its one-run-at-a-time lock is per instance, so one instance makes it per service); request timeout 300 s (at least the 180 s attempt deadline); `RUN_DEADLINE_SECONDS` 150 (attempt deadline minus 30 s); HTTP startup probe on `GET /status`. |
| `google_cloud_run_v2_service_iam_member.invoker` | always (one per row of the invoker table) | `roles/run.invoker` on one service for one caller. The whole invoker policy lives in `local.service_invokers` (main.tf). |
| `google_cloud_scheduler_job.health_sync` | real data **and** `ingest_enabled` | `POST /sync` every 15 minutes with an OIDC token for `scheduler-invoker`, audience the service URL; `retry_count = 0`, `attempt_deadline = 180s`. |
| `google_cloud_run_v2_job.seed` | `use_synthetic_data = true` | The seeder (T09): args `--hours 4` to the image's `pulsebridge-seed` entrypoint (no `command` here: the image is the single source of the entrypoint), runs as `seeder`, `max_retries = 0`, one task, task timeout `seed_task_timeout` (1800 s). Exists even while ingest is off, because the first seeding runs it by hand. |
| `google_cloud_run_v2_job_iam_member.seed_runner` | synthetic data | `roles/run.invoker` on **that job only** for `scheduler-invoker`, which gives it `run.jobs.run` there and nowhere else. |
| `google_cloud_scheduler_job.seed` | synthetic data **and** `ingest_enabled` | Hourly (minute 7) `POST https://run.googleapis.com/v2/projects/<p>/locations/<r>/jobs/<job>:run` with an OAuth token for `scheduler-invoker`; no retries. |
| `google_logging_metric.this[...]` | always (keys per mode, see "Alerts") | Counters over the services' own log lines and Cloud Scheduler's attempt logs. |
| `google_monitoring_notification_channel.email` | `alert_email` set | An email notification channel for that address. |
| `google_monitoring_alert_policy.this[...]` | `alert_email` or `notification_channels` set | One per metric: notifies when the counter is above 0 in a 15-minute window. |

**Not created here, on purpose:**

- The **push subscription**: module healthcare owns the topic, the subscription and its
  dead-letter policy, and turns push on with `push_enabled`. This module hands it
  `v2_to_fhir_push_endpoint`.
- **Direct VPC egress** for these services: neither needs the VPC. Both call only Google APIs
  (Healthcare, and for health-sync `oauth2.googleapis.com` and `health.googleapis.com`) over
  Google's public endpoints. Only services that reach the edge VM (cuff simulator, fhir-proxy;
  module phr) join the `serverless` subnet (D14).
- A **health-sync backfill job**: `health-sync-backfill --days N` is a manual one-off; run it
  locally (services/health-sync/README.md, "Backfill") or ask for a job in a later task.

## Ingress: why "internal only", and what it requires

Both services default to `INGRESS_TRAFFIC_INTERNAL_ONLY` (`v2_to_fhir_ingress`,
`health_sync_ingress`). The URL exists, but a request from the open internet is refused at the
network edge before IAM is even consulted, so a leaked token alone would not get in.

Google's "Private networking and Cloud Run" page
(https://docs.cloud.google.com/run/docs/securing/private-networking), read directly:

> Requests from BigQuery, Cloud Scheduler, Cloud Tasks, Dialogflow CX, Eventarc, Pub/Sub,
> synthetic monitors (including uptime checks), and Workflows in the same project or VPC Service
> Controls perimeter are recognized as "internal." Requests from Cloud Run or App Engine that are
> sent from within the same project or VPC Service Controls perimeter all require additional
> configuration before they are recognized as "internal."

> When your destination Cloud Run resource receives traffic from other Cloud Run resources […]
> the traffic must use the VPC network to be considered internal. […] Configure the source
> resource to route all traffic through the VPC network and enable Private Google Access on the
> subnet associated with Direct VPC egress or the connector.

| Caller | Internal? | Consequence |
| --- | --- | --- |
| Pub/Sub push from module healthcare's subscription | yes, **if the dataset is in the same project** | A dataset in another project would have every push refused; a validation on `healthcare_project_id` refuses that combination unless `v2_to_fhir_ingress = "INGRESS_TRAFFIC_ALL"`. |
| Cloud Scheduler in the same project | yes | |
| `fhir-proxy` calling health-sync `/status` | only if routed through the VPC | See "For T22". |

### For T22 (module phr)

fhir-proxy should use Direct VPC egress on project-base's `serverless` subnet with
**`egress = "ALL_TRAFFIC"`** (not the `PRIVATE_RANGES_ONLY` project-base suggests for other
services). Then its calls to `*.run.app` go through the VPC, and the subnet has Private Google
Access, so they count as internal: health-sync and the cuff simulator can both stay
internal-only. Google APIs are reached the same way; no Cloud NAT is needed, so it costs nothing.
That holds as long as fhir-proxy talks only to Google APIs and the edge VM, which is the design.
Its OIDC audience for `/status` is `health_sync_url`.

Fallback, if T22 keeps `PRIVATE_RANGES_ONLY`: set `health_sync_ingress = "INGRESS_TRAFFIC_ALL"`.
IAM still admits only `scheduler-invoker` and `fhir-proxy`; only the network lock is given up.

If pushes are refused at the network level on the first apply (an error *before* the container
logs anything), set `v2_to_fhir_ingress = "INGRESS_TRAFFIC_ALL"`; IAM still admits only
`pubsub-push`. `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER` is not offered: nothing here sits behind
a load balancer.

## Who may invoke what

Every binding is a resource-level, additive `_iam_member` on a resource this module owns
(module identity's README: "a binding lives in the module that owns the resource it is on").

| Caller (identity key) | Role | On | Why |
| --- | --- | --- | --- |
| `pubsub-push` | `roles/run.invoker` | service `v2-to-fhir` | The identity in the push subscription's OIDC token. |
| `scheduler-invoker` | `roles/run.invoker` | service `health-sync` (real data) | The identity in the sync trigger's OIDC token. |
| `fhir-proxy` | `roles/run.invoker` | service `health-sync` (real data) | `GET /status` for the dashboard's status strip (T14). |
| `scheduler-invoker` | `roles/run.invoker` | job `seed` (synthetic data) | `run.jobs.run`, so Scheduler can start an execution. No override permission: the scheduled run changes no arguments. |

Nobody else. No `allUsers` or `allAuthenticatedUsers` (neither service is public; D10 makes
fhir-proxy the only front door), and `invoker_iam_disabled = false` keeps Cloud Run's IAM check
on. `scheduler-invoker` serves health-sync in a real deployment and the seeder in a synthetic one;
the two never exist together.

**Which accounts run what** (they hold the data access, granted in module healthcare):
`v2-to-fhir` runs v2-to-fhir, `health-sync` runs health-sync, `seeder` runs the job.

**Why `roles/run.invoker` on the job** and not `roles/run.jobsExecutor` or
`roles/run.jobsExecutorWithOverrides`: Google's "Cloud Run roles and permissions" page
(https://docs.cloud.google.com/iam/docs/roles-permissions/run) lists Invoker as "Can invoke Cloud
Run services, instances and execute Cloud Run jobs" with `run.instances.invoke`, `run.jobs.run`
and `run.routes.invoke`, grantable on a job; `jobsExecutor` is `run.executions.cancel` and
`run.jobs.run`; `…WithOverrides` adds `run.jobs.runWithOverrides`. On a job, Invoker's only
effective permission is `run.jobs.run`, which is exactly what Scheduler needs. "Execute jobs on
a schedule" (https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule) uses the same
target: `--uri="https://run.googleapis.com/v2/projects/PROJECT_ID/locations/CLOUD_RUN_REGION/jobs/JOB:run"`
with `--oauth-service-account-email`.

## Environment and secrets

From each service's README, "Configuration". `PORT` is not set: Cloud Run sets it.

| Variable | v2-to-fhir | health-sync | seeder | From |
| --- | --- | --- | --- | --- |
| `GCP_PROJECT_ID` | yes | yes | yes | `healthcare_project_id` (default `project_id`) |
| `HEALTHCARE_LOCATION` | yes | yes | yes | `healthcare_location` |
| `HEALTHCARE_DATASET_ID` | yes | yes | yes | `healthcare_dataset_id` |
| `FHIR_STORE_ID` | yes | yes | yes | `fhir_store_id` (the main store) |
| `PATIENT_ID` | yes | yes | yes | `patient_id` |
| `HL7V2_STORE_ID` | yes | | | `hl7v2_store_id` |
| `PATIENT_ASSIGNING_AUTHORITY` | if set | | | `patient_assigning_authority` |
| `TIME_ZONE` | | yes | yes | `time_zone` |
| `SYNC_INTERVAL_MINUTES` | | `15` | | the Scheduler interval, one constant for both |
| `RUN_DEADLINE_SECONDS` | | `150` | | the attempt deadline (180 s) minus 30 s, so a run answers before Scheduler gives up |
| `GOOGLE_HEALTH_*_FILE` | | the three file paths | | the mounts below |

**health-sync's secrets are files, never environment variables.** Each Google Health secret is
a **secret volume** pinned to version `latest`, mounted in its own directory (Cloud Run refuses
two secrets in one directory):

| Secret key (module identity) | File | Variable |
| --- | --- | --- |
| `google-health-client-id` | `/secrets/google-health-client-id/value` | `GOOGLE_HEALTH_CLIENT_ID_FILE` |
| `google-health-client-secret` | `/secrets/google-health-client-secret/value` | `GOOGLE_HEALTH_CLIENT_SECRET_FILE` |
| `google-health-refresh-token` | `/secrets/google-health-refresh-token/value` | `GOOGLE_HEALTH_REFRESH_TOKEN_FILE` |

A volume is read from Secret Manager on each file read; a secret environment variable is fixed
when the instance starts. With files, a reconnect (a new refresh-token version) is picked up by
the next run without a redeploy, and no secret value is in the service's configuration, the plan
or the state: only the secret's *name* and the word `latest` are.

### What a bad configuration does

**At plan time** the variables refuse most bad values before anything is deployed: store,
dataset and project IDs that could not form a resource name, a patient ID outside the services'
character set or with the wrong `SYN-` prefix, a malformed or fixed-offset time zone, an image
without a digest. **Not** caught at plan time: a well-shaped but unknown time zone
(`America/Newyork`, `Mars/Olympus`), because a validation cannot carry the tz database.

**At run time** both services check their configuration in `create_app()` and raise. That alone
does not fail a deploy: gunicorn's master process opens port 8080 *before* its worker builds the
app, so Cloud Run's default TCP startup probe ("If you don't explicitly configure a TCP startup
probe […] Cloud Run automatically configures a TCP startup probe", Cloud Run "Configure
container health checks") can pass while the worker crashes. What each service does about it:

- **health-sync** has an **HTTP startup probe on `GET /status`**. `/status` answers 200 in every
  state once the app exists, and nothing at all if `create_app()` failed. Cloud Run: "Any 2XX or
  3XX response is a success, everything else indicates failure. If a startup probe does not
  succeed within the specified time (failureThreshold * periodSeconds), which cannot exceed 600
  seconds […], the container is shut down." The settings: 5 s initial delay, period 25 s,
  timeout 22 s ("This value cannot exceed the value specified for periodSeconds"; `/status`
  budgets itself 20 s), 5 failures, so about 130 s for a cold start. Probes come from Cloud Run
  inside the instance, not through the URL, so ingress and IAM do not block them. Side effect:
  when the secrets exist, `/status` makes one token request to Google; the probe stops at its
  first success, so that is once per instance start. An unknown `TIME_ZONE` therefore ends in
  a revision that never becomes Ready. It does *not* fail on missing secret files: health-sync
  starts and reports `not-configured`, by design, so it can deploy before the connect step.
- **v2-to-fhir** still relies on the default TCP probe: its only route is `POST /pubsub/hl7v2`,
  and a probe's `GET` there answers 405, which counts as failure. A cheap `GET` route (for
  example `/healthz`) has been asked for in the service; until then, a v2-to-fhir whose
  `create_app()` fails can pass the deploy. Its pushes then get no answer, are retried, and are
  dead-lettered after about 40 minutes (see "Dead-lettered and pending"). The image-side fix
  (gunicorn `--preload`, so the master builds the app before opening the port) belongs to the
  container task. All of v2-to-fhir's environment is validated at plan time, so this path should
  not be reachable with values that passed the variables.
- **Seeder**: a job has no startup probe; a configuration error makes the execution exit with
  code 2 and log `seed-run` at WARNING, which the `seed-run-failed` alert reports.

Whether a revision that never becomes Ready makes `terraform apply` fail (the provider waits for
the service's Ready condition) is not verified.

## The two switches

**`use_synthetic_data`** chooses the wearable source. `false`: health-sync, its trigger, its
metrics and alerts; no seeder. `true`: the seeder job and its trigger; nothing of health-sync.
`patient_id` must start with `SYN-` exactly when it is `true` (the seeder refuses anything else,
and a `SYN-` ID in a real deployment would mix up the two worlds). Set module identity's
`create_seeder_account` to the same value.

**`ingest_enabled`** (default `false`) decides whether anything starts ingestion by itself.
While false, **no Scheduler job exists** (absent, not paused: a paused job can be resumed by hand
and would then race the setup step), and module healthcare's push stays off if T25 feeds this
module's `ingest_enabled` output to its `push_enabled`. Notifications then wait in the pull
subscription (up to 7 days) and are delivered once push is switched on.

**Switching `use_synthetic_data` on an existing deployment** destroys one set of workloads
and creates the other (health-sync, its trigger and metrics; or the seeder job, its trigger and
metric). The destroy fails if `deletion_protection = true`; set it false first. It also means a
different patient: `SYN-` identifiers are allowed only with synthetic data and refused without, so
`patient_id` changes too. In practice switch on a fresh FHIR store (a new dev stack, or after
destroying module healthcare's store), because the store would otherwise hold both the real and
the synthetic patient, and the seeder refuses to touch a store whose Patient is not its own.

Why `ingest_enabled` exists (D11, T25): the setup step writes the full Patient once. If an ingest Bundle
got there first, its conditional create would make an identifier-only Patient, and two ingests at
first contact could make two.

## First run: setup before `ingest_enabled`

1. `terraform apply` with `ingest_enabled = false` (the default).
2. **Real data**: run the connect script (T11, `scripts/connect-google-health`). It stores the
   three secrets and writes the Patient. Never run it with a `SYN-` patient ID.
   **Synthetic data**: run the first seeding as a job execution with overridden arguments, and
   wait for it to finish:

   ```bash
   gcloud run jobs execute "$(terraform output -raw seed_job_name)" \
     --region "$(terraform output -raw region)" \
     --args=--days,30 --task-timeout=1800s --wait
   ```

   `--args` replaces the job's `--hours 4` for this execution only. Overriding arguments needs
   `run.jobs.runWithOverrides`, which the deployer's `roles/run.admin` includes; the scheduled
   runs never need it. (`terraform output` here is the env root's, if T25 re-exports these.)
3. `terraform apply` with `ingest_enabled = true`: the trigger appears here, and push is switched
   on in module healthcare.

**If the first apply fails at health-sync on its secrets.** Cloud Run's documentation does not
say whether a secret volume on a secret with **no versions yet** passes a deploy (Cloud Run is
documented not to check volume secrets at start-up, but the deploy-time check is undocumented).
If the first apply stops at `google_cloud_run_v2_service.health_sync` with a secret error, run
the connect script (step 2) now: it needs only the FHIR store and Secret Manager, which the
failed apply has already created. Then run `terraform apply` again.

**Sizing the seeder's task timeout.** About 10 s per Bundle is an estimate (not measured on
Cloud Healthcare), 100 windows per Bundle, 288 windows a day:

| Run | Windows | Bundles | Estimated time | Timeout to pass |
| --- | --- | --- | --- | --- |
| hourly `--hours 4` | 48 (about 24 written) | 1 | seconds | the default 1800 s |
| `--days 30` | about 8,600 | about 85 | about 15 min | `--task-timeout=1800s` (the default) |
| `--days 366` | about 105,000 | about 1,050 | about 3 h | `--task-timeout=21600s` (6 h) |

Rule of thumb: `days × 3 × 10 s`, doubled. Cloud Run jobs allow up to 24 h per task. Raise
`seed_task_timeout` itself only if the hourly run ever needs more than 30 minutes. A run stopped
by its timeout is safe to repeat: every write is keyed.

## Dead-lettered and "pending" HL7v2 messages

v2-to-fhir labels every stored message it has finished with `application_outcome` (`accepted`
or `error`) and `reason` (D7). A message **without** that label is shown as **"pending"**: not
processed yet, still being retried, or **dead-lettered**. A notification is dead-lettered by
module healthcare's subscription after 10 failed deliveries (about 40 minutes), typically because
of a condition that cannot heal by itself: a missing role (401/403), a missing FHIR store, a
persistent 409. The message stays "pending" until it is replayed. Fix the cause first, or the
replay is dead-lettered again.

Replay works because the notification carries only the stored message's resource name (spike
S5), and v2-to-fhir is idempotent: republishing that name to the **ingest topic** makes it fetch
and map the message again, and every write is keyed. Names (`…/hl7V2Stores/<store>/messages/<id>`)
hold no health data. The commands below need `jq`; the topic and subscription names are module
healthcare's outputs (`ingest_topic_name`, `dead_letter_subscription_name`).

**A. Replay from the dead-letter subscription** (kept 7 days):

```bash
DL_SUB="<prefix>-hl7v2-ingest-dead-letter-pull"   # module.healthcare.dead_letter_subscription_name
TOPIC="<prefix>-hl7v2-ingest"                     # module.healthcare.ingest_topic_name

# One message at a time: pull without acknowledging, republish, then acknowledge. The
# dead-letter subscription keeps Pub/Sub's default 10 s ack deadline, so a large batch could
# expire (and be redelivered) before it is acknowledged.
while msg="$(gcloud pubsub subscriptions pull "$DL_SUB" --limit=1 --format=json)" &&
      [ "$(printf '%s' "$msg" | jq 'length')" -gt 0 ]; do
  name="$(printf '%s' "$msg" | jq -r '.[0].message.data' | base64 -d)"   # the stored message's name
  msg_type="$(printf '%s' "$msg" | jq -r '.[0].message.attributes.msgType // ""')"
  gcloud pubsub topics publish "$TOPIC" --message="$name" --attribute="msgType=${msg_type}" || break
  gcloud pubsub subscriptions ack "$DL_SUB" --ack-ids="$(printf '%s' "$msg" | jq -r '.[0].ackId')" || break
done
```

A publish or acknowledgment that fails stops the loop (`|| break`), so a persistent failure (no
permission, wrong topic) cannot spin forever; fix it and run the loop again. A message whose
publish failed was not acknowledged and comes back on a later pull. One whose
acknowledgment fails after a successful publish is replayed twice, which is harmless (keyed
writes). Keep `msgType`: v2-to-fhir skips `ACK` notifications by it. Not verified: whether
`gcloud … pull --format=json` returns `message.data` base64-encoded (the REST API does); if it is
already decoded, drop the `base64 -d`.

**B. Find every "pending" message in the store** (also those older than the dead-letter
retention): list messages without an outcome label, then publish each name to the topic as in A.

The REST `messages.list` call filters on the server. Its reference
(https://docs.cloud.google.com/healthcare-api/docs/reference/rest/v1/projects.locations.datasets.hl7V2Stores.messages/list)
documents `labels.x` and "The operator `:*` can be used to assert the existence of a label",
and "You can prepend the NOT operator to an expression to negate it":

```bash
STORE="projects/<project>/locations/<location>/datasets/<dataset>/hl7V2Stores/<store>"  # module.healthcare.hl7v2_store_id
curl -sS -G -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://healthcare.googleapis.com/v1/${STORE}/messages" \
  --data-urlencode 'filter=NOT labels."application_outcome":*' \
  --data-urlencode 'pageSize=1000' |
  jq -r '.hl7V2Messages[]?.name'
```

The default view (`BASIC`) returns names only, no message content. Follow `nextPageToken`
(`--data-urlencode "pageToken=<token>"`) if there are more than 1,000. If the filter is refused,
fall back to filtering in `jq`: add `--data-urlencode 'view=FULL'` (needed to see labels) and
replace the `jq` with
`jq -r '.hl7V2Messages[]? | select((.labels.application_outcome // "") == "") | .name'`.
`FULL` returns each message's content, so pipe it straight into `jq` and never save it to a
file. The `gcloud healthcare hl7v2-messages list --filter=...` variant is **not verified**:
`--filter` may be applied by gcloud on the client, over a view that has no labels.

A message mapped a moment ago may not be labelled yet; look again after a minute before
replaying. The dashboard's messages log shows the same set as "pending".

Replaying needs `pubsub.topics.publish` on the ingest topic (the deployer has `roles/pubsub.admin`;
no workload account has it).

## Alerts

Each signal is a log-based metric (a counter of matching log entries); each gets an alert
policy that fires when its count summed over 15 minutes is above 0 and closes after an hour
without a match. **Where alerts go**: `alert_email` (this module creates an email notification
channel for it) and/or `notification_channels` (channels made elsewhere). T25 should set one of
them. **With neither, there are metrics only: no alert policy exists and nobody is told** when
ingestion breaks; the counters can still be charted in Metrics Explorer as
`logging.googleapis.com/user/<metric name>` (charting needs the Monitoring API, enabled by
project-base).

| Metric (`<prefix>-…`) | Mode | Counts | Means |
| --- | --- | --- | --- |
| `health-sync-failed-run` | real data | health-sync's `sync-run` lines with `http_status >= 500`, or its `bug` line whose message starts with `sync failed` | A run needs a person (permissions, a missing store, a refused Bundle), Google or FHIR was down for a whole run, or the sync handler crashed. The `/status` handler's own `bug` line ("status check failed: …") is deliberately not counted: it is not a run. |
| `health-sync-not-connected` | real data | `sync-run` lines with `outcome` `reconnect-needed` or `not-configured` | Both answer 200 at WARNING: the refresh token no longer works (run the connect script again), or a secret is missing or unusable (for example a disabled version). |
| `seed-run-failed` | synthetic | the seeder job's `seed-run` lines at WARNING or above | An execution did not end as `seeded` (exit 1, 2, 3 or 4, or `bug`). |
| `scheduler-attempt-failed` | both | Cloud Scheduler's own `AttemptFinished` entries for this mode's trigger at ERROR | Failures that write no line of the service's own: ingress refusal, a 403 from IAM, a 504 at the request timeout, a crash-looping container, a refused `jobs.run` call. |

All service filters are scoped to this module's workloads (`resource.type="cloud_run_revision"`
and `service_name`, or `cloud_run_job` and `job_name`). Cloud Run turns each JSON line on stdout
or stderr into a log entry with its fields under `jsonPayload` (Cloud Run "Logging and viewing
logs"), which is what the filters match.

**The Scheduler signal, and how sure it is.** Field names from Google's pages, read directly:
the monitored resource `cloud_scheduler_job` has labels `project_id`, `location` and `job_id`
(https://docs.cloud.google.com/logging/docs/api/v2/resource-list); Scheduler "publishes logs at
the start of each execution and at the end of each execution"
(https://docs.cloud.google.com/scheduler/docs/viewing-logs); the entries are
`type.googleapis.com/google.cloud.scheduler.logging.AttemptStarted` and `…AttemptFinished`, and a
failed attempt's entry carries `debugInfo` such as "URL_ERROR-ERROR_OTHER. Original HTTP response
code number = 403" (https://docs.cloud.google.com/scheduler/docs/troubleshooting). That a
**failed** `AttemptFinished` is logged at severity ERROR comes from community reports, not
Google's pages; if it is not, this alert never fires. Check on the first deployment by looking
at one failed attempt's entry. A metric-absence condition on the `sync-run` lines was the
alternative; it was not chosen because the field names above are documented, and absence
alerting would also fire whenever ingest is intentionally off.

**A failed seeder execution** is not covered by the Scheduler signal: Scheduler only sees whether
`jobs.run` accepted the execution, not how the container ended. That is why `seed-run-failed`
exists. A seeder container killed before it logs anything (out of memory, the task timeout) is
not covered by either.

Not created: the health-sync README's optional `stranded-preliminary` alert.

## Cost

**Idle cost: $0.** Nothing here is always on or billed by the hour.

| Item | Billed for | At PulseBridge's volume |
| --- | --- | --- |
| Cloud Run services | request time and instances while serving; `min_instance_count = 0`, `cpu_idle = true` (CPU only during requests) | about 96 short health-sync runs a day plus one push per cuff reading: inside Cloud Run's monthly free tier (general knowledge, not checked) |
| Cloud Run job | seconds of each execution | 24 executions of a few seconds a day (synthetic only) |
| Cloud Scheduler | $0.10 per job per month; "Each Google billing account gets 3 jobs per month free" (https://cloud.google.com/scheduler/pricing) | 1 job: $0 unless the billing account already has 3 others |
| Log-based metrics | metric data ingested; "First 150 MiB per billing account" free | three counters with a handful of points a day: $0 |
| Alert policies (only with `alert_email` or channels) | "Starting no sooner than September 1, 2027 […] $0.35 per month for each metric reference in an alerting policy" and $0.50 per million points returned (https://cloud.google.com/products/observability/pricing) | $0 until then; afterwards 3 policies with one metric reference each: about $1.05 a month |
| Email notification channel | nothing | $0 |
| Logs | Cloud Logging ingestion beyond the free monthly allotment | a few MB a month |

## For T25: what to wire

```hcl
module "healthcare" {
  push_enabled               = module.ingestion.ingest_enabled
  push_endpoint              = module.ingestion.v2_to_fhir_push_endpoint
  push_service_account_email = module.identity.service_account_emails["pubsub-push"]
  project_id                 = var.project_id # the same project as module ingestion
}
module "identity" {
  create_seeder_account = var.use_synthetic_data
}
module "ingestion" {
  depends_on  = [module.identity] # secret accessor grants before health-sync deploys
  alert_email = var.alert_email   # or notification_channels; with neither, metrics only
}
```

**Outputs consumed from other modules** (exact names):

| This module's variable | From |
| --- | --- |
| `healthcare_project_id` | `module.healthcare.project_id` (must equal `project_id` while ingress is internal-only) |
| `healthcare_location` | `module.healthcare.location` |
| `healthcare_dataset_id` | `module.healthcare.dataset_name` |
| `fhir_store_id` | `module.healthcare.fhir_store_name` |
| `hl7v2_store_id` | `module.healthcare.hl7v2_store_name` |
| `service_account_emails` | `module.identity.service_account_emails` (keys `v2-to-fhir`, `pubsub-push`, `scheduler-invoker`, and `health-sync` + `fhir-proxy` or `seeder`) |
| `secret_ids` | `module.identity.secret_ids` (keys `google-health-client-id`, `-client-secret`, `-refresh-token`) |

**This module's outputs consumed elsewhere:**

| Output | To |
| --- | --- |
| `v2_to_fhir_push_endpoint` | `module.healthcare.push_endpoint` (it waits for the invoker binding, so push never starts before its permission) |
| `ingest_enabled` | `module.healthcare.push_enabled` |
| `health_sync_url` | module phr's fhir-proxy configuration (`<url>/status`, OIDC audience `<url>`); `null` with synthetic data |
| `seed_job_name`, `region` | the first-seeding command above |

**Variables T25 sets from its own tfvars:** `project_id`, `region`, `use_synthetic_data`,
`ingest_enabled`, `v2_to_fhir_image`, `health_sync_image` or `seed_image`, `patient_id`,
`time_zone`, `alert_email` and/or `notification_channels`; optionally `name_prefix`, `labels`,
`patient_assigning_authority`, `deletion_protection`, the two ingress variables.

**Provided by other modules:** the deployer's `roles/logging.configWriter`,
`roles/monitoring.alertPolicyEditor` and `roles/monitoring.notificationChannelEditor`
(bootstrap); `monitoring.googleapis.com` in project-base's API list; fhir-proxy's
`ALL_TRAFFIC` egress (phr, see "For T22").

## Not verified

Nothing in this module has been applied, and every test runs against a mocked provider. The
Google facts quoted above were read on Google's pages; what remains to confirm on the first
apply (T25, T29):

- Pub/Sub push and Cloud Scheduler reach the internal-only services; fhir-proxy's `/status`
  passes with `ALL_TRAFFIC` egress.
- A revision that never becomes Ready (health-sync with an unknown time zone) makes the apply
  fail; the HTTP startup probe on `/status` passes on a healthy cold start within its window.
- health-sync deploys while its secrets have no version yet (otherwise see "First run").
- `roles/run.invoker` on the job lets Scheduler's OAuth call to `jobs.run` through.
- The OIDC audience (the bare service URL) is accepted by Cloud Run.
- The log filters match the real entries (`jsonPayload.event`, `http_status`, `outcome`,
  `jsonPayload.message` for the `bug` line, the seeder's lines on stderr), and failed Scheduler
  attempts are logged at ERROR.
- The email channel delivers without further confirmation, and the policies fire as intended.
- The replay commands: whether `gcloud … pull --format=json` returns `message.data`
  base64-encoded, and the `messages.list` filter in practice.

## Tests

`terraform test` in this directory runs `tests/*.tftest.hcl` against a mocked Google provider: no
credentials, nothing created. `make tf-test` runs it with Google credentials removed from the
environment, then runs `tests/static_checks.sh`.

| File | Checks |
| --- | --- |
| `real_data.tftest.hcl` | health-sync and no seeder; exact invokers, nothing public, IAM check on, internal ingress; scale to zero, `max_instance_count = 1`, timeout ≥ 180 s; v2-to-fhir timeout ≤ the 60 s ack deadline and concurrency 4; `deletion_protection` false by default; images and service accounts; exact environments (with `RUN_DEADLINE_SECONDS = 150`); secrets as volumes at `latest`, one directory each, none in environment variables; the HTTP startup probe on `/status` and its timing limits; the three metrics and their filters (including the sync handler's `bug` clause and `not-configured`); no policy or channel without a destination; policies with channels: one per metric, threshold, aggregation, labels; `alert_email` creates the email channel and is used alone or with channels; the trigger (15 min, `retry_count = 0`, `attempt_deadline = 180s`, OIDC, audience); no trigger while `ingest_enabled = false`; outputs |
| `synthetic.tftest.hcl` | the seeder job and nothing of health-sync; job account, retries, image, args, no `command`, timeout, environment, `deletion_protection`; the narrow job grant; the seeder and Scheduler metrics; no trigger until `ingest_enabled`, then hourly with OAuth to `jobs.run` |
| `validation.tftest.hcl` | tag-only, tag-plus-digest, short-digest and `latest` images refused; required images per mode; `SYN-` required with and refused without synthetic data; patient ID characters; fixed-offset and malformed time zones; required accounts and secrets per mode; full paths for store IDs; a dataset in another project with internal ingress; short timeouts; bad channels and alert addresses; unsupported ingress |
| `static_checks.sh` | reads the `.tf` files and fails on any public principal, any `min_instance_count` other than 0 or `manual_instance_count`, any `_iam_binding`/`_iam_policy` or project IAM, any service account key, any other IAM member resource or member not built from `var.service_account_emails`, `invoker_iam_disabled` other than `false`, secrets in environment variables, a hardcoded project (argument, path, service account email, registry path), a child module, and a missing `depends_on` on the push endpoint output or on either Scheduler job |

```bash
make tf-test                      # from the repository root (credential-free)
tests/static_checks.sh            # from this directory, or pass the module path as $1
```

## Documentation relied on

Read directly (with `curl`) unless marked otherwise:

- Terraform provider `hashicorp/google` 8.11: the schema from `terraform providers schema` for
  the pinned version (including `startup_probe.http_get.path/port`, `timeout_seconds`,
  `period_seconds`, `failure_threshold`; `google_monitoring_notification_channel` `type`,
  `labels`), and the provider docs on GitHub, `website/docs/r/`: `cloud_run_v2_service`
  (`ingress` values, `deletion_protection` defaults to true, `invoker_iam_disabled`,
  `template.scaling`, secret volume `items.version` "latest"), `cloud_scheduler_job` (`paused`
  defaults to enabled; OIDC `audience` defaults to the target URI; OAuth `scope` defaults to
  cloud-platform; HTTP `attempt_deadline` 15 s to 30 min).
- Cloud Run "Configure container health checks"
  (https://docs.cloud.google.com/run/docs/configuring/healthchecks): HTTP probe success is 2XX
  or 3XX; timeout must not exceed the period; `failureThreshold * periodSeconds` at most 600 s;
  the default TCP startup probe; "HTTP health check endpoints are externally accessible".
- Cloud Run "Private networking and Cloud Run"
  (https://docs.cloud.google.com/run/docs/securing/private-networking): internal sources.
- Cloud Run "Execute jobs on a schedule"
  (https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule) and IAM "Cloud Run roles and
  permissions" (https://docs.cloud.google.com/iam/docs/roles-permissions/run).
- Cloud Run "Logging and viewing logs" (https://docs.cloud.google.com/run/docs/logging): stdout
  and stderr are collected; single-line JSON goes to `jsonPayload`.
- Cloud Logging monitored resources
  (https://docs.cloud.google.com/logging/docs/api/v2/resource-list): `cloud_scheduler_job`,
  `cloud_run_job`, `cloud_run_revision` and their labels.
- Cloud Scheduler "View logs" (https://docs.cloud.google.com/scheduler/docs/viewing-logs) and
  "Troubleshooting" (https://docs.cloud.google.com/scheduler/docs/troubleshooting):
  `AttemptStarted`/`AttemptFinished`, `debugInfo`. ERROR severity of a failed attempt: community
  reports only (Google Developer forums, search results).
- Cloud Healthcare `hl7V2Stores.messages.list`
  (https://docs.cloud.google.com/healthcare-api/docs/reference/rest/v1/projects.locations.datasets.hl7V2Stores.messages/list):
  label filters, `:*`, `NOT`, `view`.
- Pricing: Google Cloud Observability (https://cloud.google.com/products/observability/pricing),
  Cloud Scheduler (https://cloud.google.com/scheduler/pricing).
- The services' READMEs and code (health-sync `app.py` and `logs.py`, the seeder's `cli.py`, the
  seeder Dockerfile on the T35 branch), and modules healthcare and identity.

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
| [google_cloud_run_v2_job.seed](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/cloud_run_v2_job) | resource |
| [google_cloud_run_v2_job_iam_member.seed_runner](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/cloud_run_v2_job_iam_member) | resource |
| [google_cloud_run_v2_service.health_sync](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/cloud_run_v2_service) | resource |
| [google_cloud_run_v2_service.v2_to_fhir](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/cloud_run_v2_service) | resource |
| [google_cloud_run_v2_service_iam_member.invoker](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/cloud_run_v2_service_iam_member) | resource |
| [google_cloud_scheduler_job.health_sync](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/cloud_scheduler_job) | resource |
| [google_cloud_scheduler_job.seed](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/cloud_scheduler_job) | resource |
| [google_logging_metric.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/logging_metric) | resource |
| [google_monitoring_alert_policy.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/monitoring_alert_policy) | resource |
| [google_monitoring_notification_channel.email](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/monitoring_notification_channel) | resource |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_alert_email"></a> [alert\_email](#input\_alert\_email) | Optional email address the alerts are sent to: creates a Cloud Monitoring email notification channel in this module, used together with notification\_channels. No default and never hardcoded: set it in the env root's tfvars (T25). It is stored in the Terraform state and on the channel. null (default) creates no channel. | `string` | `null` | no |
| <a name="input_deletion_protection"></a> [deletion\_protection](#input\_deletion\_protection) | Refuse to destroy the Cloud Run services and the job (the provider's deletion\_protection, which defaults to true). false (default) here, because they hold no data and dev is destroyed nightly; the data lives in module healthcare, which has its own deletion\_policy. | `bool` | `false` | no |
| <a name="input_fhir_store_id"></a> [fhir\_store\_id](#input\_fhir\_store\_id) | Short ID of the main (health-data) FHIR store (module.healthcare.fhir\_store\_name), not the audit store. Becomes FHIR\_STORE\_ID. | `string` | n/a | yes |
| <a name="input_health_sync_image"></a> [health\_sync\_image](#input\_health\_sync\_image) | health-sync image by digest (same form as v2\_to\_fhir\_image). Required when use\_synthetic\_data is false; ignored, and may be null, when it is true. | `string` | `null` | no |
| <a name="input_health_sync_ingress"></a> [health\_sync\_ingress](#input\_health\_sync\_ingress) | Ingress of health-sync. "INGRESS\_TRAFFIC\_INTERNAL\_ONLY" (default): Cloud Scheduler in the same project counts as internal; fhir-proxy's /status call counts only if fhir-proxy sends it through the VPC (Direct VPC egress with egress "ALL\_TRAFFIC"; see the README, "Ingress", which T22 must follow). "INGRESS\_TRAFFIC\_ALL" if T22 keeps "PRIVATE\_RANGES\_ONLY"; IAM still lets only scheduler-invoker and fhir-proxy invoke it. | `string` | `"INGRESS_TRAFFIC_INTERNAL_ONLY"` | no |
| <a name="input_health_sync_request_timeout"></a> [health\_sync\_request\_timeout](#input\_health\_sync\_request\_timeout) | Cloud Run request timeout for health-sync. At least 180 s, the Scheduler attempt deadline, so Cloud Run never cuts a run that Scheduler is still waiting for; a run budgets itself to 150 s (RUN\_DEADLINE\_SECONDS). | `string` | `"300s"` | no |
| <a name="input_healthcare_dataset_id"></a> [healthcare\_dataset\_id](#input\_healthcare\_dataset\_id) | Short dataset ID (module.healthcare.dataset\_name), not the full resource path. Becomes HEALTHCARE\_DATASET\_ID. | `string` | n/a | yes |
| <a name="input_healthcare_location"></a> [healthcare\_location](#input\_healthcare\_location) | Location of the Healthcare dataset (module.healthcare.location). Becomes HEALTHCARE\_LOCATION. | `string` | n/a | yes |
| <a name="input_healthcare_project_id"></a> [healthcare\_project\_id](#input\_healthcare\_project\_id) | Project of the Healthcare dataset (module.healthcare.project\_id). null (default) means project\_id. Becomes GCP\_PROJECT\_ID. | `string` | `null` | no |
| <a name="input_hl7v2_store_id"></a> [hl7v2\_store\_id](#input\_hl7v2\_store\_id) | Short ID of the HL7v2 store (module.healthcare.hl7v2\_store\_name). Becomes v2-to-fhir's HL7V2\_STORE\_ID. | `string` | n/a | yes |
| <a name="input_ingest_enabled"></a> [ingest\_enabled](#input\_ingest\_enabled) | Whether anything may start ingestion on a schedule. false (default): no Scheduler job<br/>exists, so neither health-sync nor the seeder ever runs by itself. Turn it on only after<br/>the setup step has written the Patient (D11): the connect script (T11) for a real<br/>deployment, or the first seeding (`--days 30`) for a synthetic one. Both run outside<br/>`terraform apply`; see the README, "First run". The cuff path's push subscription lives in<br/>module healthcare: feed this module's `ingest_enabled` output to its push\_enabled so one<br/>switch controls both. | `bool` | `false` | no |
| <a name="input_labels"></a> [labels](#input\_labels) | Labels added to the Cloud Run services and the job (Scheduler jobs and log metrics take no labels). | `map(string)` | `{}` | no |
| <a name="input_name_prefix"></a> [name\_prefix](#input\_name\_prefix) | Prefix for every resource name in this module: "<prefix>-v2-to-fhir", "<prefix>-health-sync", "<prefix>-seed" and so on. Lets two stacks share one project without colliding. | `string` | `"pulsebridge"` | no |
| <a name="input_notification_channels"></a> [notification\_channels](#input\_notification\_channels) | Existing Cloud Monitoring notification channels ("projects/<project>/notificationChannels/<id>", made by hand or in the env root) the alerts are sent to, in addition to alert\_email. With neither set there are log-based metrics only: they can be charted, but no alert policy exists and nobody is told. Alert policies and charts need the Monitoring API (monitoring.googleapis.com, in project-base's default list). | `list(string)` | `[]` | no |
| <a name="input_patient_assigning_authority"></a> [patient\_assigning\_authority](#input\_patient\_assigning\_authority) | Optional. If set, v2-to-fhir also requires PID-3.4.1 of the matching PID-3 to equal it (PATIENT\_ASSIGNING\_AUTHORITY). null (default) leaves the variable unset. | `string` | `null` | no |
| <a name="input_patient_id"></a> [patient\_id](#input\_patient\_id) | The one configured patient identifier (value in the urn:pulsebridge:patient system), the<br/>same one the setup step writes (D11) and every service uses. Becomes PATIENT\_ID. Letters,<br/>digits, '.', '\_' and '-', at most 64 characters (the services' own rule). With<br/>use\_synthetic\_data it must start with "SYN-" (the seeder refuses anything else); without<br/>it, it must not, so a real deployment can never be mistaken for a synthetic one (T25).<br/>It is an identifier, not a secret, but never commit a real one: it comes from tfvars. | `string` | n/a | yes |
| <a name="input_project_id"></a> [project\_id](#input\_project\_id) | ID of the GCP project the Cloud Run services, the job, the Scheduler jobs and the log-based metrics are created in. Comes from the env root; never hardcoded. | `string` | n/a | yes |
| <a name="input_region"></a> [region](#input\_region) | Region for the Cloud Run services, the Cloud Run job and the Scheduler jobs, such as "us-central1". Use the region of the Healthcare dataset, so requests to the store stay in one region. | `string` | `"us-central1"` | no |
| <a name="input_secret_ids"></a> [secret\_ids](#input\_secret\_ids) | Module identity's map of secret key to Secret Manager secret ID (module.identity.secret\_ids), passed as it is. health-sync mounts google-health-client-id, google-health-client-secret and google-health-refresh-token; other keys are ignored. Not used with synthetic data. | `map(string)` | `{}` | no |
| <a name="input_seed_image"></a> [seed\_image](#input\_seed\_image) | Seeder image by digest (same form as v2\_to\_fhir\_image). Required when use\_synthetic\_data is true; ignored, and may be null, when it is false. | `string` | `null` | no |
| <a name="input_seed_task_timeout"></a> [seed\_task\_timeout](#input\_seed\_task\_timeout) | Time limit of one seeder job execution (a duration such as "1800s"). The hourly top-up (--hours 4) is one Bundle and takes seconds; the first --days 30 seeding is about 85 Bundles, roughly 15 minutes at an assumed 10 s per Bundle, so the default 1800 s leaves twice that. A longer first seeding sets its own limit with --task-timeout (README, "First run"). Only seconds used are billed. | `string` | `"1800s"` | no |
| <a name="input_service_account_emails"></a> [service\_account\_emails](#input\_service\_account\_emails) | Module identity's map of workload name to service account email, passed as it is<br/>(module.identity.service\_account\_emails). Keys used:<br/>  v2-to-fhir        runs v2-to-fhir                              (always)<br/>  pubsub-push       the only invoker of v2-to-fhir               (always)<br/>  health-sync       runs health-sync                             (real data)<br/>  scheduler-invoker invokes health-sync /sync, or runs the seeder job (Scheduler's token)<br/>  fhir-proxy        the only other invoker of health-sync (/status) (real data)<br/>  seeder            runs the seeder job                          (synthetic data)<br/>Other keys are ignored. Bindings are chosen by key name only, so the plan knows which exist<br/>even while the emails are unknown. | `map(string)` | n/a | yes |
| <a name="input_time_zone"></a> [time\_zone](#input\_time\_zone) | The user's IANA time zone, such as "America/New\_York" or "Europe/Amsterdam" (TIME\_ZONE for health-sync and the seeder). Fixed offsets ("UTC-5", "Etc/GMT+5", "+02:00") are refused, as the services refuse them: they ignore daylight saving time. "UTC" itself is allowed. Only the SHAPE is checked here: a well-formed but unknown name ("America/Newyork") passes validation, health-sync and the seeder then refuse to start, and health-sync's HTTP startup probe makes that deploy fail (README, "What a bad configuration does"). | `string` | n/a | yes |
| <a name="input_use_synthetic_data"></a> [use\_synthetic\_data](#input\_use\_synthetic\_data) | Which wearable source this deployment has. false (default): the real Google Health<br/>connection, so health-sync is deployed with its Scheduler job and alerts, and the seeder<br/>is not. true: the synthetic-data seeder (T09) runs as a Cloud Run job instead, and nothing<br/>of health-sync is created. The two never exist together. The cuff path (v2-to-fhir) is<br/>deployed either way. patient\_id must start with "SYN-" when this is true and must not<br/>when it is false. | `bool` | `false` | no |
| <a name="input_v2_to_fhir_image"></a> [v2\_to\_fhir\_image](#input\_v2\_to\_fhir\_image) | v2-to-fhir image by digest, as the build prints it: "<region>-docker.pkg.dev/<project>/<repository>/v2-to-fhir@sha256:<64 hex>". A tag (":v1") is refused, with or without a digest. | `string` | n/a | yes |
| <a name="input_v2_to_fhir_ingress"></a> [v2\_to\_fhir\_ingress](#input\_v2\_to\_fhir\_ingress) | Ingress of v2-to-fhir. "INGRESS\_TRAFFIC\_INTERNAL\_ONLY" (default): only internal sources reach it, and a Pub/Sub push from a subscription in the same project counts as internal (README, "Ingress"). "INGRESS\_TRAFFIC\_ALL" is the fallback if the first apply shows pushes refused at the network level; IAM still lets only pubsub-push invoke it. | `string` | `"INGRESS_TRAFFIC_INTERNAL_ONLY"` | no |
| <a name="input_v2_to_fhir_max_instances"></a> [v2\_to\_fhir\_max\_instances](#input\_v2\_to\_fhir\_max\_instances) | Upper bound on v2-to-fhir instances. Each handles 4 concurrent pushes (gunicorn's 4 threads); one cuff reading is one push, so 2 is plenty and caps the bill if something floods the topic. | `number` | `2` | no |
| <a name="input_v2_to_fhir_request_timeout"></a> [v2\_to\_fhir\_request\_timeout](#input\_v2\_to\_fhir\_request\_timeout) | Cloud Run request timeout for v2-to-fhir (a duration such as "60s"). Match module healthcare's ack\_deadline\_seconds (60): Pub/Sub treats a push unanswered by then as failed and retries anyway. | `string` | `"60s"` | no |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_alert_email_channel"></a> [alert\_email\_channel](#output\_alert\_email\_channel) | Name of the email notification channel created from alert\_email, or null. |
| <a name="output_alert_policy_names"></a> [alert\_policy\_names](#output\_alert\_policy\_names) | Alert policy resource names by alert key; empty when neither alert\_email nor notification\_channels is set (metrics only). |
| <a name="output_health_sync_service_name"></a> [health\_sync\_service\_name](#output\_health\_sync\_service\_name) | Name of the health-sync Cloud Run service, or null with synthetic data. |
| <a name="output_health_sync_url"></a> [health\_sync\_url](#output\_health\_sync\_url) | Base URL of health-sync, or null with synthetic data. fhir-proxy (module phr) calls <url>/status with an OIDC token whose audience is this URL. |
| <a name="output_ingest_enabled"></a> [ingest\_enabled](#output\_ingest\_enabled) | Echo of ingest\_enabled. Feed it to module healthcare's push\_enabled, so one switch turns on the Scheduler job here and the push subscription there. |
| <a name="output_log_metric_names"></a> [log\_metric\_names](#output\_log\_metric\_names) | Log-based metric names by alert key (health-sync-failed-run, health-sync-not-connected, seed-run-failed, scheduler-attempt-failed; those of the deployed mode). In Monitoring they are logging.googleapis.com/user/<name>. |
| <a name="output_region"></a> [region](#output\_region) | Region of the services, the job and the Scheduler jobs (for the gcloud commands in the README). |
| <a name="output_scheduler_job_names"></a> [scheduler\_job\_names](#output\_scheduler\_job\_names) | Names of the Scheduler jobs that exist (empty while ingest\_enabled is false). |
| <a name="output_seed_job_name"></a> [seed\_job\_name](#output\_seed\_job\_name) | Name of the seeder Cloud Run job, or null without synthetic data. The first seeding runs it by hand: gcloud run jobs execute <name> --region <region> --args=--days,30 --wait. |
| <a name="output_v2_to_fhir_push_endpoint"></a> [v2\_to\_fhir\_push\_endpoint](#output\_v2\_to\_fhir\_push\_endpoint) | URL module healthcare's push subscription posts to: v2\_to\_fhir\_url plus /pubsub/hl7v2. Pass it as healthcare's push\_endpoint. It is only known once pubsub-push may invoke the service, so push never starts before its permission exists. |
| <a name="output_v2_to_fhir_service_name"></a> [v2\_to\_fhir\_service\_name](#output\_v2\_to\_fhir\_service\_name) | Name of the v2-to-fhir Cloud Run service. |
| <a name="output_v2_to_fhir_url"></a> [v2\_to\_fhir\_url](#output\_v2\_to\_fhir\_url) | Base URL of the v2-to-fhir service (https://...run.app). Also the OIDC audience Cloud Run checks push tokens against. |
<!-- END_TF_DOCS -->
