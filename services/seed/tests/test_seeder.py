"""One run against the fake FHIR store: Bundles, keys, batching, setup, status, zones, safety."""

from __future__ import annotations

import copy
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pulsebridge_fhir import PatientInput, build_patient, heart_rate_key, window_from_rollup
from pulsebridge_gcp import RetryableError
from pulsebridge_gcp.testing import json_response
from pulsebridge_seed import synthetic
from pulsebridge_seed.generator import generate
from pulsebridge_seed.seeder import Outcome, run, setup_bundle
from pulsebridge_seed.testing import (
    NOW,
    PATIENT_ID,
    StoreFake,
    deps_for,
    fresh_store,
    make_config,
    patient_search,
)

DAY = timedelta(days=1)
UNTIL = datetime(2026, 3, 10, 5, 0, tzinfo=UTC)


def seed(store: StoreFake, **kwargs):
    config = kwargs.pop("config", None) or make_config()
    now = kwargs.pop("now", NOW)
    return run(config, deps_for(store, now=now), **kwargs)


def data_bundles(store: StoreFake) -> list[dict]:
    return store.bundles()[1:]


def entries_of(bundles: list[dict], resource_type: str) -> list[dict]:
    return [
        e for b in bundles for e in b["entry"] if e["resource"]["resourceType"] == resource_type
    ]


def without_recorded(entry: dict) -> dict:
    entry = copy.deepcopy(entry)
    entry["resource"].pop("recorded", None)
    return entry


# ---- re-running -------------------------------------------------------------------------------


def test_rerunning_the_same_range_sends_identical_entries():
    """Same conditional keys and the same resources: the store would rewrite, not duplicate.
    Only Provenance.recorded (the write time) may differ, and here the clock is fixed."""
    first, second = fresh_store(), fresh_store()
    seed(first, length=DAY, until=UNTIL)
    seed(second, length=DAY, until=UNTIL, now=NOW + timedelta(hours=1))
    a, b = data_bundles(first), data_bundles(second)
    assert [e["request"] for e in entries_of(a, "Observation")] == [
        e["request"] for e in entries_of(b, "Observation")
    ]
    assert [without_recorded(e) for bundle in a for e in bundle["entry"]] == [
        without_recorded(e) for bundle in b for e in bundle["entry"]
    ]
    urls = [e["request"]["url"] for e in entries_of(a, "Observation")]
    assert len(urls) == len(set(urls))  # one key per window


def test_overlapping_runs_send_the_same_observation_for_each_shared_window():
    """A run now and a run 30 hours later: every window both cover is written to the same key
    with the same values (history is not rewritten)."""
    early, late = fresh_store(), fresh_store()
    seed(early, length=2 * DAY, until=UNTIL)
    seed(late, length=2 * DAY, until=UNTIL + timedelta(hours=30), now=NOW + timedelta(hours=30))

    def by_url(store):
        return {
            e["request"]["url"]: e["resource"]
            for e in entries_of(data_bundles(store), "Observation")
        }

    a, b = by_url(early), by_url(late)
    shared = set(a) & set(b)
    assert len(shared) > 150
    for url in shared:
        assert a[url]["valueQuantity"] == b[url]["valueQuantity"]
        assert a[url]["component"] == b[url]["component"]
        assert a[url]["effectivePeriod"] == b[url]["effectivePeriod"]


def test_observation_keys_are_t06_keys_on_patient_code_and_utc_start():
    store = fresh_store()
    seed(store, length=timedelta(hours=2), until=UNTIL)
    items = generate(7, ZoneInfo("America/New_York"), UNTIL - timedelta(hours=2), UNTIL).items
    patient_key = build_patient(PatientInput(PATIENT_ID)).key
    expected = [heart_rate_key(patient_key, window_from_rollup(i)) for i in items]
    sent = [
        e["request"]["url"].rsplit("%7C", 1)[-1].rsplit("|", 1)[-1]
        for e in entries_of(data_bundles(store), "Observation")
    ]
    assert sent == expected


# ---- setup, batching, counts --------------------------------------------------------------------


def test_setup_bundle_is_sent_once_and_first():
    store = fresh_store()
    report = seed(store, length=2 * DAY, until=UNTIL)
    bundles = store.bundles()

    def upserts_patient(bundle):
        return any(
            e["request"]["method"] == "PUT" and e["request"]["url"].startswith("Patient")
            for e in bundle["entry"]
        )

    setups = [b for b in bundles if upserts_patient(b)]
    assert setups == [bundles[0]]
    assert report.setup_written
    setup = bundles[0]
    assert [e["resource"]["resourceType"] for e in setup["entry"]] == ["Patient", "Device"]
    assert all(e["request"]["method"] == "PUT" for e in setup["entry"])
    # Data Bundles never PUT the Patient or Device: conditional creates only (D11).
    for bundle in data_bundles(store):
        for entry in bundle["entry"][:2]:
            assert entry["request"]["method"] == "POST"
            assert "ifNoneExist" in entry["request"]
    # The safety search came before every write.
    assert store.requests[0].method == "GET"


def test_batching_100_windows_per_bundle_and_counts():
    store = fresh_store()
    report = seed(store, length=DAY, until=UNTIL)
    expected = generate(7, ZoneInfo("America/New_York"), UNTIL - DAY, UNTIL)
    sizes = [len(b["entry"]) for b in data_bundles(store)]
    n = len(expected.items)
    assert sizes == [202] * (n // 100) + ([2 + 2 * (n % 100)] if n % 100 else [])
    assert report.windows_in_range == 288
    assert report.windows_generated == n == report.windows_written
    assert report.windows_absent == 288 - n
    assert report.bundles_written == report.bundles_planned == len(sizes) == 3
    # A day: about 288 windows minus gaps, about 2 entries per window (plus 2 per Bundle).
    assert 2 * n + 6 == sum(sizes)
    assert 500 <= sum(sizes) <= 582
    assert report.entry_statuses["Observation 201"] == n


def test_batch_size_is_configurable():
    store = fresh_store()
    seed(store, config=make_config(batch_windows=7), length=timedelta(hours=1), until=UNTIL)
    sizes = [len(b["entry"]) for b in data_bundles(store)]
    assert max(sizes) <= 2 + 2 * 7 and len(sizes) >= 2


def test_dry_run_counts_and_sends_nothing():
    store = StoreFake()  # no routes: any request fails the test
    report = seed(store, length=DAY, until=UNTIL, dry_run=True)
    assert store.requests == []
    assert report.outcome == Outcome.DRY_RUN
    assert report.windows_in_range == 288 and report.windows_generated > 250
    assert report.windows_written == 0 and report.bundles_planned == 3


# ---- status by the health-sync cutoff -----------------------------------------------------------


def test_final_and_preliminary_follow_the_health_sync_cutoff():
    """complete_before = floor(now - 60 min): windows ending after it are preliminary."""
    store = fresh_store()
    report = seed(store, length=timedelta(hours=3), until=NOW)
    cutoff = datetime(2026, 3, 10, 11, 0, tzinfo=UTC)  # floor(12:02:30 - 60 min)
    assert report.complete_before == cutoff
    for entry in entries_of(data_bundles(store), "Observation"):
        resource = entry["resource"]
        end = datetime.fromisoformat(resource["effectivePeriod"]["end"])
        assert resource["status"] == ("final" if end <= cutoff else "preliminary")
    assert report.windows_preliminary > 0 and report.windows_final > 0
    assert report.windows_final + report.windows_preliminary == report.windows_written


# ---- time zones ---------------------------------------------------------------------------------


def test_keys_do_not_depend_on_the_time_zone():
    """The same window start has the same key in every zone (T06 keys on the UTC start)."""
    urls = {}
    for zone in ("America/New_York", "Asia/Kolkata", "UTC"):
        store = fresh_store()
        seed(store, config=make_config(time_zone=ZoneInfo(zone)), length=DAY, until=UNTIL)
        urls[zone] = {
            e["resource"]["effectivePeriod"]["start"]: e["request"]["url"]
            for e in entries_of(data_bundles(store), "Observation")
        }
    by_instant = {
        zone: {datetime.fromisoformat(k).astimezone(UTC): v for k, v in found.items()}
        for zone, found in urls.items()
    }
    ny, kolkata, utc = by_instant.values()
    for instant in set(ny) & set(kolkata) & set(utc):
        assert ny[instant] == kolkata[instant] == utc[instant]
    assert len(set(ny) & set(kolkata) & set(utc)) > 200


def test_local_offsets_across_the_spring_daylight_saving_change():
    """New York, 2026-03-08: 02:00 EST jumps to 03:00 EDT (07:00 UTC). Periods before carry
    -05:00, after -04:00; the local day has 23 hours, 276 windows."""
    store = fresh_store()
    start = datetime(2026, 3, 8, 5, 0, tzinfo=UTC)  # local midnight, EST
    end = datetime(2026, 3, 9, 4, 0, tzinfo=UTC)  # next local midnight, EDT
    report = seed(store, length=end - start, until=end, now=end + timedelta(hours=5))
    assert report.windows_in_range == 276
    for entry in entries_of(data_bundles(store), "Observation"):
        period = entry["resource"]["effectivePeriod"]
        instant = datetime.fromisoformat(period["start"])
        offset = "-05:00" if instant < datetime(2026, 3, 8, 7, 0, tzinfo=UTC) else "-04:00"
        assert period["start"].endswith(offset), period
        if instant + timedelta(minutes=5) != datetime(2026, 3, 8, 7, 0, tzinfo=UTC):
            assert period["end"].endswith(offset), period


def test_the_written_period_is_the_generated_window_in_local_time():
    store = fresh_store()
    tokyo = ZoneInfo("Asia/Tokyo")
    seed(store, config=make_config(time_zone=tokyo), length=timedelta(hours=1), until=UNTIL)
    items = generate(7, tokyo, UNTIL - timedelta(hours=1), UNTIL).items
    written = entries_of(data_bundles(store), "Observation")
    for item, entry in zip(items, written, strict=True):
        period = entry["resource"]["effectivePeriod"]
        assert period["start"].endswith("+09:00")
        assert datetime.fromisoformat(period["start"]) == datetime.fromisoformat(item["startTime"])
        assert entry["resource"]["valueQuantity"]["value"] == item["heartRate"]["beatsPerMinuteAvg"]


# ---- synthetic markers --------------------------------------------------------------------------

# Pinned literally, not through the shared constant: a typo in TEST_DATA_LABEL would otherwise
# be "tested" by itself. HL7 v3 ActReason HTEST, "test health data".
HTEST = {
    "system": "http://terminology.hl7.org/CodeSystem/v3-ActReason",
    "code": "HTEST",
    "display": "test health data",
}


def test_every_resource_is_marked_synthetic():
    store = fresh_store()
    seed(store, length=timedelta(hours=1), until=UNTIL)
    bundles = store.bundles()
    for bundle in bundles:
        for entry in bundle["entry"]:
            meta = entry["resource"]["meta"]
            assert HTEST in meta["security"], entry["resource"]["resourceType"]
            assert "tag" not in meta  # the label is a security label, not a tag
    patient = bundles[0]["entry"][0]["resource"]
    assert patient["identifier"][0]["value"].startswith("SYN-")
    assert patient["name"] == [{"use": "official", "family": "Synthetic", "given": ["Demo"]}]
    device = bundles[0]["entry"][1]["resource"]
    assert device["deviceName"][0]["name"] == synthetic.DEVICE_NAME
    # The ingest Device is the same Device (same identifier) as the setup one.
    assert bundles[1]["entry"][1]["resource"]["identifier"] == device["identifier"]
    provenance = entries_of(bundles[1:], "Provenance")[0]["resource"]
    agents = {a["type"]["coding"][0]["code"]: a for a in provenance["agent"]}
    assert agents["custodian"]["who"]["identifier"]["value"] == "PULSEBRIDGE_SYNTHETIC"
    assert agents["performer"]["role"][0]["coding"][0]["code"] == "SYNTHETIC"
    assert agents["assembler"]["who"]["display"] == "PulseBridge seed (synthetic data)"
    # Observations keep T06's profile in meta beside the label.
    observation = entries_of(bundles[1:], "Observation")[0]["resource"]
    assert observation["meta"]["profile"]


def test_mark_synthetic_does_not_change_requests_or_the_input():
    original = setup_bundle(make_config())
    plain = copy.deepcopy(original)
    for entry in plain["entry"]:
        entry["resource"].pop("meta")
    marked = synthetic.mark_synthetic(plain)
    assert [e["request"] for e in marked["entry"]] == [e["request"] for e in plain["entry"]]
    assert all("meta" not in e["resource"] for e in plain["entry"])
    assert synthetic.mark_synthetic(marked) == marked  # one label, not two


def test_a_label_in_meta_tag_is_still_recognised():
    patient = stored_patient(meta={"tag": [dict(HTEST)]}, name=[{"family": "Rivera"}])
    assert synthetic.is_synthetic_patient(patient)


# ---- safety check -------------------------------------------------------------------------------


def stored_patient(**changes) -> dict:
    patient = copy.deepcopy(setup_bundle(make_config())["entry"][0]["resource"])
    patient["id"] = "abc"
    patient.update(changes)
    return patient


def identifier_only(value: str = PATIENT_ID, **extra) -> dict:
    """What v2-to-fhir's conditional create stores when an ingest reaches the store first."""
    patient = {
        "resourceType": "Patient",
        "id": "v2-made",
        "meta": {"versionId": "1"},
        "identifier": [
            {
                "use": "official",
                "type": {
                    "coding": [
                        {"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "PT"}
                    ]
                },
                "system": "urn:pulsebridge:patient",
                "value": value,
            }
        ],
    }
    patient.update(extra)
    return patient


def store_with(*patients: dict, total: int | None = None) -> StoreFake:
    store = StoreFake()
    store.add("GET", "/fhir/Patient", patient_search(*patients, total=total))
    return store


def test_safety_search_is_one_patient_identifier_search():
    store = fresh_store()
    report = seed(store, length=timedelta(hours=1), until=UNTIL)
    searches = store.calls("GET", "/fhir/Patient")
    assert len(searches) == 1
    assert searches[0].params["identifier"] == f"urn:pulsebridge:patient|{PATIENT_ID}"
    assert report.safety_check == "no-patient"


def test_a_patient_the_seeder_wrote_is_accepted():
    for patient in (stored_patient(), stored_patient(meta={"versionId": "3"})):  # label dropped
        store = store_with(patient)
        report = seed(store, length=timedelta(hours=1), until=UNTIL)
        assert report.outcome == Outcome.SEEDED and report.safety_check == "synthetic-patient"


@pytest.mark.parametrize(
    "patient",
    [
        identifier_only(),
        # v2-to-fhir with the cuff simulator's PID-5 set to the synthetic name (T22).
        identifier_only(name=[{"use": "official", "family": "Synthetic", "given": ["Demo"]}]),
    ],
)
def test_an_identifier_only_syn_patient_is_accepted_and_completed(patient):
    """An ingest that reached the store first must not lock the seeder out: the Patient it
    created holds nothing real, and the setup Bundle completes it."""
    store = store_with(patient)
    report = seed(store, length=timedelta(hours=1), until=UNTIL)
    assert report.outcome == Outcome.SEEDED
    assert report.safety_check == "synthetic-incomplete"
    assert report.setup_written
    first = store.bundles()[0]
    assert [e["request"]["method"] for e in first["entry"]] == ["PUT", "PUT"]


@pytest.mark.parametrize(
    ("patients", "reason"),
    [
        # A real person written by the connect script: another name, no HTEST label.
        (
            [stored_patient(meta={}, name=[{"use": "official", "family": "Rivera"}])],
            "patient-not-synthetic",
        ),
        # A real name beside the synthetic one: every name must be the synthetic one.
        (
            [
                stored_patient(
                    meta={},
                    name=[
                        {"use": "official", "family": "Synthetic", "given": ["Demo"]},
                        {"use": "usual", "family": "Rivera", "given": ["Sam"]},
                    ],
                )
            ],
            "patient-not-synthetic",
        ),
        # Synthetic name, but real-looking demographics.
        ([stored_patient(meta={}, gender="female")], "patient-not-synthetic"),
        ([stored_patient(meta={}, birthDate="1971-06-02")], "patient-not-synthetic"),
        (
            [identifier_only(telecom=[{"system": "phone", "value": "555-0100"}])],
            "patient-not-synthetic",
        ),
        # Identifier-only, but not a SYN- identifier in PulseBridge's system.
        ([identifier_only(value="PB-0001")], "patient-not-synthetic"),
        (
            [{"resourceType": "Patient", "id": "x", "identifier": [{"value": PATIENT_ID}]}],
            "patient-not-synthetic",
        ),
        (
            [
                identifier_only(
                    identifier=identifier_only()["identifier"]
                    + [{"system": "urn:pulsebridge:google-health-user-id", "value": "g"}]
                )
            ],
            "patient-not-synthetic",
        ),
        ([stored_patient(), stored_patient(id="def")], "patients-duplicated"),
    ],
)
def test_refuses_before_any_write(patients, reason):
    store = store_with(*patients)
    report = seed(store, length=timedelta(hours=1), until=UNTIL)
    assert report.outcome == Outcome.REFUSED and report.reason == reason
    assert report.safety_check == "refused"
    assert store.bundles() == []


def test_force_overrides_the_refusal_and_warns(caplog):
    caplog.set_level(logging.WARNING)
    store = store_with(stored_patient(meta={}, name=[{"family": "Rivera"}]))
    report = seed(store, length=timedelta(hours=1), until=UNTIL, force=True)
    assert report.outcome == Outcome.SEEDED and report.safety_check == "forced"
    assert report.setup_written  # --force replaces the record
    forced = [r for r in caplog.records if getattr(r, "fields", {}).get("event") == "seed-forced"]
    assert len(forced) == 1 and forced[0].levelno == logging.WARNING
    assert forced[0].fields["reason"] == "patient-not-synthetic"


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (json_response(200, {}), "safety-search-not-a-bundle"),  # (an empty body reads as {})
        (
            json_response(200, {"resourceType": "OperationOutcome", "issue": []}),
            "safety-search-not-a-bundle",
        ),
        (
            json_response(200, {"resourceType": "Bundle", "type": "batch-response"}),
            "safety-search-not-a-searchset",
        ),
        (
            json_response(
                200,
                {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "entry": {"resource": {"resourceType": "Patient"}},
                },
            ),
            "safety-search-malformed-entry",
        ),
        (
            json_response(
                200,
                {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "entry": [{"resource": {"resourceType": "Observation"}}],
                },
            ),
            "safety-search-malformed-entry",
        ),
        (
            json_response(200, {"resourceType": "Bundle", "type": "searchset", "total": "1"}),
            "safety-search-malformed-total",
        ),
        (
            json_response(
                200,
                {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "total": 3,
                    "entry": [{"resource": stored_patient()}],
                },
            ),
            "safety-search-incomplete",
        ),
    ],
)
def test_an_unusable_safety_answer_fails_closed(answer, reason):
    store = StoreFake()
    store.add("GET", "/fhir/Patient", answer)
    report = seed(store, length=timedelta(hours=1), until=UNTIL)
    assert (report.outcome, report.reason) == (Outcome.FHIR_REJECTED, reason)
    assert store.bundles() == []


def test_an_empty_200_body_fails_closed():
    from pulsebridge_gcp import HttpResponse

    store = StoreFake()
    store.add("GET", "/fhir/Patient", HttpResponse(status=200, body=b"", headers={}))
    report = seed(store, length=timedelta(hours=1), until=UNTIL)
    assert report.outcome == Outcome.FHIR_REJECTED
    assert store.bundles() == []


def test_a_search_outcome_entry_is_allowed():
    outcome = {"resourceType": "OperationOutcome", "issue": []}
    store = StoreFake()
    store.add(
        "GET",
        "/fhir/Patient",
        json_response(
            200,
            {
                "resourceType": "Bundle",
                "type": "searchset",
                "total": 0,
                "entry": [{"resource": outcome, "search": {"mode": "outcome"}}],
            },
        ),
    )
    assert seed(store, length=timedelta(hours=1), until=UNTIL).outcome == Outcome.SEEDED


@pytest.mark.parametrize(
    ("status", "outcome"),
    [(503, Outcome.FHIR_UNAVAILABLE), (403, Outcome.FHIR_MISCONFIGURED)],
)
def test_a_failed_safety_search_sends_no_bundle(status, outcome):
    store = StoreFake()
    store.add("GET", "/fhir/Patient", json_response(status, {}))
    report = seed(store, length=timedelta(hours=1), until=UNTIL)
    assert (report.outcome, report.fhir_status) == (outcome, status)
    assert store.bundles() == []


# ---- setup only when needed (D11: written once) --------------------------------------------------


def test_setup_is_sent_on_the_first_run_only():
    store = fresh_store()
    first = seed(store, length=timedelta(hours=2), until=UNTIL)
    assert first.setup_written and first.safety_check == "no-patient"
    sent = len(store.bundles())
    second = seed(
        store,
        length=timedelta(hours=2),
        until=UNTIL + timedelta(hours=1),
        now=NOW + timedelta(hours=1),
    )
    assert second.safety_check == "synthetic-patient" and not second.setup_written
    for bundle in store.bundles()[sent:]:
        for entry in bundle["entry"]:
            if entry["resource"]["resourceType"] in ("Patient", "Device"):
                assert entry["request"]["method"] == "POST"  # conditional create only


# ---- read once ----------------------------------------------------------------------------------


def test_a_second_run_over_the_same_range_writes_nothing():
    store = fresh_store()
    seed(store, length=DAY, until=UNTIL)
    before = len(store.bundles())
    report = seed(store, length=DAY, until=UNTIL)
    assert report.stored_read == "ok"
    assert report.windows_written == 0 and report.bundles_written == 0
    assert report.windows_unchanged == report.windows_generated
    assert len(store.bundles()) == before


def test_each_window_is_written_at_most_twice_over_hourly_top_ups():
    """Hourly runs with --hours 4 for a day: every window first appears preliminary (it ended
    under an hour ago), is written final once, and never again."""
    store = fresh_store()
    writes: dict[str, list[str]] = {}
    for hour in range(26):
        now = NOW + timedelta(hours=hour)
        seen = len(store.bundles())
        seed(store, length=timedelta(hours=4), until=now, now=now)
        for bundle in store.bundles()[seen:]:
            for entry in entries_of([bundle], "Observation"):
                writes.setdefault(entry["request"]["url"], []).append(entry["resource"]["status"])
    assert writes
    middle = [w for w in writes.values() if len(w) > 1 or w == ["final"]]
    assert all(len(w) <= 2 for w in writes.values())
    assert all(w in (["preliminary", "final"], ["final"]) for w in middle)
    assert sum(1 for w in writes.values() if w == ["preliminary", "final"]) > 200


def test_read_follows_next_links():
    store = fresh_store()
    seed(store, length=DAY, until=UNTIL)
    store.page_size_cap = 50
    report = seed(store, length=DAY, until=UNTIL)
    assert report.windows_written == 0
    assert len(store.calls("GET", "/fhir/Observation")) >= 6


def test_a_failed_read_writes_everything():
    store = fresh_store()
    seed(store, length=timedelta(hours=2), until=UNTIL)
    store.add("GET", "/fhir/Observation", json_response(500, {}))
    report = seed(store, length=timedelta(hours=2), until=UNTIL)
    assert report.stored_read == "failed:500"
    assert report.windows_written == report.windows_generated > 0


def test_an_unusable_read_writes_everything():
    store = fresh_store()
    seed(store, length=timedelta(hours=2), until=UNTIL)
    store.add("GET", "/fhir/Observation", json_response(200, {"resourceType": "OperationOutcome"}))
    report = seed(store, length=timedelta(hours=2), until=UNTIL)
    assert report.stored_read == "failed:StoredReadError"
    assert report.windows_written == report.windows_generated


def test_duplicate_keys_are_written_and_withdrawn_windows_left_alone():
    store = fresh_store()
    seed(store, length=timedelta(hours=2), until=UNTIL)
    keys = list(store.observations)
    withdrawn = store.observations[keys[0]]
    withdrawn["status"] = "entered-in-error"
    duplicate = copy.deepcopy(store.observations[keys[1]])
    store.observations[keys[1] + "-copy"] = duplicate  # same identifier stored twice
    report = seed(store, length=timedelta(hours=2), until=UNTIL)
    assert report.windows_withdrawn_kept == 1
    assert report.windows_written == 1  # only the duplicated key
    assert store.observations[keys[0]]["status"] == "entered-in-error"


def test_changed_values_are_rewritten():
    """After a change of seed the stored windows differ and are rewritten."""
    store = fresh_store()
    seed(store, length=timedelta(hours=2), until=UNTIL)
    report = seed(store, config=make_config(seed=8), length=timedelta(hours=2), until=UNTIL)
    assert report.windows_changed > 0
    assert report.windows_written == report.windows_changed + report.windows_new


# ---- failures -----------------------------------------------------------------------------------


def test_transient_failures_are_retried_with_backoff():
    store = fresh_store()
    store.bundle_errors = [json_response(503, {}), RetryableError("POST x", None)]
    sleeps: list[float] = []
    report = run(
        make_config(), deps_for(store, sleeps=sleeps), length=timedelta(hours=1), until=UNTIL
    )
    assert report.outcome == Outcome.SEEDED
    assert sleeps == [1.0, 2.0]
    assert len(store.bundles()) == 2 + 2  # two failed setup attempts, setup, one data Bundle


def test_a_conflict_is_retried():
    store = fresh_store()
    store.bundle_errors = [None, json_response(409, {"resourceType": "OperationOutcome"})]
    sleeps: list[float] = []
    report = run(
        make_config(), deps_for(store, sleeps=sleeps), length=timedelta(hours=1), until=UNTIL
    )
    assert report.outcome == Outcome.SEEDED and sleeps == [1.0]
    assert report.bundles_written == 1


def test_retries_are_bounded():
    store = fresh_store()
    store.bundle_errors = [json_response(503, {})] * 10
    report = seed(store, config=make_config(max_attempts=3), length=timedelta(hours=1), until=UNTIL)
    assert report.outcome == Outcome.FHIR_UNAVAILABLE and report.fhir_status == 503
    assert len(store.bundles()) == 3
    assert not report.setup_written


@pytest.mark.parametrize(
    ("status", "outcome", "reason"),
    [
        (403, Outcome.FHIR_MISCONFIGURED, "fhir-permission-denied"),
        (404, Outcome.FHIR_MISCONFIGURED, "fhir-store-missing"),
        (400, Outcome.FHIR_REJECTED, "fhir-refused"),
    ],
)
def test_failures_are_classified_and_stop_the_run(status, outcome, reason):
    store = fresh_store()
    body = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "invalid", "diagnostics": "value 71.3 rejected"}],
    }
    store.bundle_errors = [None, json_response(status, body)]  # setup ok, first data Bundle fails
    report = seed(store, length=DAY, until=UNTIL)
    assert (report.outcome, report.reason, report.fhir_status) == (outcome, reason, status)
    assert report.setup_written and report.bundles_written == 0
    assert len(store.bundles()) == 2  # no retry of a refusal, and the run stopped there
    assert "71.3" not in str(report.as_dict())  # the diagnostics text is not kept


# ---- the clock ----------------------------------------------------------------------------------


def test_a_future_until_is_refused_by_run_itself():
    with pytest.raises(ValueError, match="later than now"):
        seed(StoreFake(), length=DAY, until=NOW + timedelta(minutes=1))


def test_a_backdated_until_uses_the_real_clock_for_the_cutoff():
    """--until 30 minutes ago: windows ending in the last hour of real time are still
    preliminary, although they end before --until."""
    store = fresh_store()
    until = NOW - timedelta(minutes=30)
    report = seed(store, length=timedelta(hours=3), until=until)
    assert report.complete_before == datetime(2026, 3, 10, 11, 0, tzinfo=UTC)
    statuses = {
        e["resource"]["effectivePeriod"]["end"]: e["resource"]["status"]
        for e in entries_of(data_bundles(store), "Observation")
    }
    late = [
        s
        for end, s in statuses.items()
        if datetime.fromisoformat(end) > datetime(2026, 3, 10, 11, 0, tzinfo=UTC)
    ]
    assert late and set(late) == {"preliminary"}


def test_an_unexpected_error_is_reported_with_counts():
    store = fresh_store()
    store.bundle_errors = [None, KeyError("boom")]
    report = seed(store, length=timedelta(hours=1), until=UNTIL)
    assert (report.outcome, report.reason) == (Outcome.BUG, "KeyError")
    assert report.windows_generated > 0 and report.setup_written


def test_an_endless_next_chain_writes_everything():
    """A server that always sends a next link cannot loop the run: the read gives up after a
    bounded number of pages and every window is written."""
    store = fresh_store()
    seed(store, length=timedelta(hours=1), until=UNTIL)
    base = deps_for(store).fhir.fhir_base_url("health")
    page = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [],
        "link": [{"relation": "next", "url": f"{base}/Observation?_page=again"}],
    }
    store.add("GET", "/fhir/Observation", json_response(200, page))
    report = seed(store, length=timedelta(hours=1), until=UNTIL)
    assert report.stored_read == "failed:StoredReadError"
    assert report.windows_written == report.windows_generated
    assert len(store.calls("GET", "/fhir/Observation")) <= 12 + 2 + 1
