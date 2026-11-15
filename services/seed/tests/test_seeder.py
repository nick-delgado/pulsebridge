"""One run against the fake FHIR store: Bundles, keys, batching, setup, status, zones, safety."""

from __future__ import annotations

import copy
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


def test_every_resource_is_marked_synthetic():
    store = fresh_store()
    seed(store, length=timedelta(hours=1), until=UNTIL)
    bundles = store.bundles()
    for bundle in bundles:
        for entry in bundle["entry"]:
            assert synthetic.has_test_tag(entry["resource"]), entry["resource"]["resourceType"]
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
    # Observations keep T06's profile in meta beside the tag.
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
    assert synthetic.mark_synthetic(marked) == marked  # one tag, not two


# ---- safety check -------------------------------------------------------------------------------


def stored_patient(**changes) -> dict:
    patient = copy.deepcopy(setup_bundle(make_config())["entry"][0]["resource"])
    patient["id"] = "abc"
    patient.update(changes)
    return patient


def store_with(*patients: dict) -> StoreFake:
    store = StoreFake()
    store.add("GET", "/fhir/Patient", patient_search(*patients))
    return store


def test_safety_search_is_one_patient_identifier_search():
    store = fresh_store()
    report = seed(store, length=timedelta(hours=1), until=UNTIL)
    searches = store.calls("GET", "/fhir/Patient")
    assert len(searches) == 1
    assert searches[0].params["identifier"] == f"urn:pulsebridge:patient|{PATIENT_ID}"
    assert report.safety_check == "no-patient"


def test_a_patient_the_seeder_wrote_is_accepted():
    for patient in (stored_patient(), stored_patient(meta={"versionId": "3"})):  # tag dropped
        store = store_with(patient)
        report = seed(store, length=timedelta(hours=1), until=UNTIL)
        assert report.outcome == Outcome.SEEDED and report.safety_check == "synthetic-patient"


@pytest.mark.parametrize(
    ("patients", "reason"),
    [
        # A real person written by the connect script: another name, no HTEST tag.
        (
            [stored_patient(meta={}, name=[{"use": "official", "family": "Rivera"}])],
            "patient-not-synthetic",
        ),
        # A minimal Patient that health-sync or v2-to-fhir created on first contact.
        (
            [{"resourceType": "Patient", "id": "x", "identifier": [{"value": PATIENT_ID}]}],
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


def test_force_overrides_the_refusal():
    store = store_with(stored_patient(meta={}, name=[{"family": "Rivera"}]))
    report = seed(store, length=timedelta(hours=1), until=UNTIL, force=True)
    assert report.outcome == Outcome.SEEDED and report.safety_check == "forced"
    assert len(store.bundles()) >= 2


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
