"""T47: only the last ``HISTORY_DAYS`` days are shown; older data is hidden, never deleted.

The harness clock is 2026-01-02T12:00:00Z (``fhir_proxy.testing.START``), so with the default
seven days the boundary is 2025-12-26T12:00:00.000Z. Every reading below is synthetic.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlsplit

import pytest
from fhir_proxy.config import VARIABLES, ConfigError, ServiceConfig
from fhir_proxy.history import (
    bounded_query,
    effective_in_window,
    window_start,
)
from fhir_proxy.search import parse_observation_query
from fhir_proxy.testing import AUDIT_STORE, FHIR_STORE, STORE_NAME, build_harness, example

BOUNDARY = datetime(2025, 12, 26, 12, 0, tzinfo=UTC)
BOUNDARY_TEXT = "2025-12-26T12:00:00.000Z"
HR = "code=http://loinc.org|8867-4"
BP = "code=http://loinc.org|85354-9"
LOINC = "http://loinc.org"


def _z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def heart_rate(rid: str, start: datetime, minutes: int = 5) -> dict:
    return {
        "resourceType": "Observation",
        "id": rid,
        "status": "final",
        "subject": {"reference": "Patient/pat-1"},
        "code": {"coding": [{"system": LOINC, "code": "8867-4"}]},
        "effectivePeriod": {"start": _z(start), "end": _z(start + timedelta(minutes=minutes))},
        "valueQuantity": {"value": 64, "unit": "/min", "system": "http://unitsofmeasure.org"},
    }


def blood_pressure(rid: str, at: datetime) -> dict:
    return {
        "resourceType": "Observation",
        "id": rid,
        "status": "final",
        "subject": {"reference": "Patient/pat-1"},
        "code": {"coding": [{"system": LOINC, "code": "85354-9"}]},
        "effectiveDateTime": _z(at),
    }


@pytest.fixture
def readings(harness, patient) -> dict[str, dict]:  # noqa: ARG001
    """Windows and readings around the boundary, by name."""
    second = timedelta(seconds=1)
    made = {
        # ends one second before the boundary: outside
        "hr-before": heart_rate("hr-before", BOUNDARY - timedelta(minutes=5) - second),
        # starts two minutes before the boundary, ends three after: overlaps, so shown
        "hr-overlap": heart_rate("hr-overlap", BOUNDARY - timedelta(minutes=2)),
        # ends exactly at the boundary: ge is inclusive, so shown
        "hr-ends-at": heart_rate("hr-ends-at", BOUNDARY - timedelta(minutes=5)),
        "hr-inside": heart_rate("hr-inside", BOUNDARY + timedelta(hours=1)),
        "bp-before": blood_pressure("bp-before", BOUNDARY - timedelta(milliseconds=1)),
        "bp-at": blood_pressure("bp-at", BOUNDARY),
        "bp-old": blood_pressure("bp-old", BOUNDARY - timedelta(days=60)),
    }
    for resource in made.values():
        harness.fake.put(FHIR_STORE, resource)
    return made


def _ids(response) -> list[str]:  # noqa: ANN001
    assert response.status_code == 200, response.get_data(as_text=True)
    return sorted(e["resource"]["id"] for e in response.get_json()["entry"])


# ---- the rule itself ---------------------------------------------------------------------------


def test_the_boundary_is_now_minus_the_days_to_the_millisecond() -> None:
    now = datetime(2026, 3, 10, 8, 30, 15, 123987, tzinfo=UTC)
    assert window_start(now, 7) == datetime(2026, 3, 3, 8, 30, 15, 123000, tzinfo=UTC)
    assert window_start(now, 1) == datetime(2026, 3, 9, 8, 30, 15, 123000, tzinfo=UTC)


@pytest.mark.parametrize(
    ("effective", "inside"),
    [
        ({"effectiveDateTime": "2025-12-26T12:00:00Z"}, True),  # exactly at: inside
        ({"effectiveDateTime": "2025-12-26T11:59:59.999Z"}, False),  # just outside
        ({"effectiveDateTime": "2025-12-26T07:00:00-05:00"}, True),  # the same instant, offset
        ({"effectiveDateTime": "2025-12-26T06:59:59-05:00"}, False),
        (
            {"effectivePeriod": {"start": "2025-12-26T11:58:00Z", "end": "2025-12-26T12:03:00Z"}},
            True,  # overlaps the boundary
        ),
        (
            {"effectivePeriod": {"start": "2025-12-26T11:55:00Z", "end": "2025-12-26T11:59:59Z"}},
            False,  # ends just before
        ),
        ({"effectivePeriod": {"start": "2025-01-01T00:00:00Z"}}, True),  # ongoing (R4)
        ({"effectiveDateTime": "2025-12-27"}, False),  # a partial date cannot be placed
        ({}, False),
    ],
)
def test_a_reading_is_inside_by_its_effective_time(effective: dict, inside: bool) -> None:
    assert effective_in_window(effective, BOUNDARY) is inside


def _query(text: str):  # noqa: ANN202
    return parse_observation_query(parse_qsl(text))


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (HR, f"ge{BOUNDARY_TEXT}"),  # none: added
        (f"{HR}&date=gt2025-11-01T00:00:00Z", f"ge{BOUNDARY_TEXT}"),  # earlier: raised
        (f"{HR}&date=ge2025-12-26T11:59:59Z", f"ge{BOUNDARY_TEXT}"),  # a second earlier
        (f"{HR}&date=gt2025-12-26T12:00:00Z", "gt2025-12-26T12:00:00Z"),  # at it, stricter: kept
        (f"{HR}&date=ge2025-12-30T00:00:00Z", "ge2025-12-30T00:00:00Z"),  # later: kept
        # A continuation whose ge bound is the proxy's own, written moments ago: kept as sent,
        # so the store's page token belongs to its query.
        (f"{HR}&date=ge2025-12-26T11:59:30.000Z&_page_token=10", "ge2025-12-26T11:59:30.000Z"),
        # ... but not when it is older than the grace, nor a gt, nor without a page token.
        (f"{HR}&date=ge2025-12-26T11:49:00.000Z&_page_token=10", f"ge{BOUNDARY_TEXT}"),
        (f"{HR}&date=gt2025-12-26T11:59:30.000Z&_page_token=10", f"ge{BOUNDARY_TEXT}"),
        (f"{HR}&date=ge2025-12-26T11:59:30.000Z", f"ge{BOUNDARY_TEXT}"),
    ],
)
def test_the_lower_bound_is_added_or_raised(query: str, expected: str) -> None:
    bounded = bounded_query(_query(query), BOUNDARY)
    assert ("date", expected) in bounded.store_params("Patient/pat-1")
    assert ("date", expected) in bounded.own_params(None)


def test_an_upper_bound_is_kept_and_a_range_before_the_boundary_is_not_refused() -> None:
    bounded = bounded_query(_query(f"{HR}&date=lt2025-12-01T00:00:00Z"), BOUNDARY)
    dates = [v for k, v in bounded.store_params("Patient/pat-1") if k == "date"]
    assert dates == [f"ge{BOUNDARY_TEXT}", "lt2025-12-01T00:00:00Z"]


# ---- searches -----------------------------------------------------------------------------------


def test_a_search_shows_only_the_window_and_tells_the_store(harness, readings) -> None:  # noqa: ARG001
    asked = harness.get(f"/fhir/Observation?{HR}&date=gt2025-11-01T00:00:00Z")
    assert _ids(asked) == ["hr-ends-at", "hr-inside", "hr-overlap"]
    assert _ids(harness.get(f"/fhir/Observation?{HR}")) == ["hr-ends-at", "hr-inside", "hr-overlap"]
    assert _ids(harness.get(f"/fhir/Observation?{BP}")) == ["bp-at"]
    for _, _, params in harness.fake.calls("GET", "/Observation$"):
        if ("_elements", "id") in params:
            continue
        assert [v for k, v in params if k == "date"] == [f"ge{BOUNDARY_TEXT}"]
    # The self link says what was really searched.
    links = {link["relation"]: link["url"] for link in asked.get_json()["link"]}
    assert ("date", f"ge{BOUNDARY_TEXT}") in parse_qsl(urlsplit(links["self"]).query)


def test_a_store_that_ignores_the_bound_still_shows_nothing_older(harness, readings) -> None:  # noqa: ARG001
    # The fake store applies no date rule at all: what is hidden is hidden by the proxy itself.
    later = harness.get(f"/fhir/Observation?{BP}&date=ge2025-12-20T00:00:00Z&_page_token=0")
    assert _ids(later) == ["bp-at"]


def test_a_later_lower_bound_is_kept(harness, readings) -> None:  # noqa: ARG001
    response = harness.get(f"/fhir/Observation?{HR}&date=ge2025-12-26T12:30:00Z")
    assert _ids(response) == ["hr-inside"]


def test_a_range_wholly_before_the_window_is_empty_not_refused(harness, readings) -> None:  # noqa: ARG001
    response = harness.get(f"/fhir/Observation?{BP}&date=lt2025-12-01T00:00:00Z")
    assert _ids(response) == []


def test_the_window_moves_with_the_clock(harness, readings) -> None:  # noqa: ARG001
    harness.clock.advance(4 * 60)  # four minutes: the overlapping window now ends before it
    assert _ids(harness.get(f"/fhir/Observation?{HR}")) == ["hr-inside"]


def test_history_days_sets_the_window(tmp_path) -> None:
    h = build_harness(tmp_path, history_days=1)
    h.fake.put(
        FHIR_STORE,
        {
            "resourceType": "Patient",
            "id": "pat-1",
            "identifier": [{"system": "urn:pulsebridge:patient", "value": "PB-SYN-0001"}],
        },
    )
    day = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)  # the harness clock minus one day
    h.fake.put(FHIR_STORE, blood_pressure("bp-in", day))
    h.fake.put(FHIR_STORE, blood_pressure("bp-out", day - timedelta(seconds=1)))
    assert _ids(h.get(f"/fhir/Observation?{BP}")) == ["bp-in"]
    assert h.get("/fhir/Observation/bp-out").status_code == 404


def test_paging_keeps_the_bound_and_never_crosses_it(harness, patient) -> None:  # noqa: ARG001
    for n in range(12):
        harness.fake.put(
            FHIR_STORE,
            blood_pressure(f"bp-{n:02d}", BOUNDARY + timedelta(hours=n - 6, minutes=1)),
        )
    harness.fake.page_size_override = 4
    page = harness.get(f"/fhir/Observation?{BP}&_count=4").get_json()
    seen = [e["resource"]["id"] for e in page["entry"]]
    while True:
        links = {link["relation"]: link["url"] for link in page["link"]}
        if "next" not in links:
            break
        assert ("date", f"ge{BOUNDARY_TEXT}") in parse_qsl(urlsplit(links["next"]).query)
        harness.clock.advance(5)  # the next page is followed a little later
        page = harness.get(links["next"]).get_json()
        seen += [e["resource"]["id"] for e in page["entry"]]
    assert seen == [f"bp-{n:02d}" for n in range(6, 12)]


# ---- reads, traces and shares of older data: exactly like missing -------------------------------


def _same_as_missing(harness, logs, old: str, missing: str) -> None:  # noqa: ANN001
    harness.get("/fhir/metadata")  # start the session first, so its log line is not compared
    before = len(logs.events())
    a = harness.get(old)
    after_old = logs.events()[before:]
    b = harness.get(missing)
    after_missing = logs.events()[before + len(after_old) :]
    assert a.status_code == b.status_code == 404
    assert a.get_data() == b.get_data()
    assert sorted(a.headers.items()) == sorted(b.headers.items())
    assert after_old == after_missing


def test_reading_an_older_observation_is_like_reading_a_missing_one(
    harness, readings, logs
) -> None:  # noqa: ARG001
    _same_as_missing(harness, logs, "/fhir/Observation/bp-before", "/fhir/Observation/no-such")
    _same_as_missing(harness, logs, "/fhir/Observation/hr-before", "/fhir/Observation/no-such")
    assert harness.get("/fhir/Observation/bp-at").status_code == 200
    assert harness.get("/fhir/Observation/hr-overlap").status_code == 200


def test_tracing_an_older_heart_rate_is_like_tracing_a_missing_one(harness, readings, logs) -> None:  # noqa: ARG001
    _same_as_missing(harness, logs, "/trace/heart-rate/hr-before", "/trace/heart-rate/no-such")
    assert harness.get("/trace/heart-rate/hr-overlap").status_code == 200


def _attempt(harness, sent: datetime, recorded: datetime) -> str:  # noqa: ANN001
    from pulsebridge_fhir import Acknowledged, SendAttempt, build_send_attempt_audit_event

    built = build_send_attempt_audit_event(
        SendAttempt(
            sent_at=sent,
            transport="mllp",
            control_id=f"MSG-{int(sent.timestamp())}",
            result=Acknowledged(code="AE", raw_ack=b"MSH|^~\\&|||||||ACK|X|P|2.5.1\rMSA|AE|"),
            patient_id="PB-SYN-0001",
            agent="cuff-sim",
            destination=STORE_NAME,
        ),
        recorded=recorded,
    )
    harness.fake.put(AUDIT_STORE, built.resource)
    return built.resource["id"]


def test_tracing_an_older_attempt_is_like_tracing_a_missing_one(harness, patient, logs) -> None:  # noqa: ARG001
    second = timedelta(seconds=1)
    # Sent just before the boundary, recorded just after it: outside, by its attempt time.
    old = _attempt(harness, BOUNDARY - second, BOUNDARY + second)
    new = _attempt(harness, BOUNDARY, BOUNDARY + second)
    _same_as_missing(
        harness, logs, f"/trace/cuff-attempts/{old}", "/trace/cuff-attempts/no-such-attempt"
    )
    assert harness.get(f"/trace/cuff-attempts/{new}").status_code == 200


def test_an_attempt_inside_is_traced_without_an_older_observation(harness, map_into_store) -> None:
    from fhir_proxy.testing import sample_bytes

    name = f"{STORE_NAME}/messages/synthetic-message-0002"  # the HTTPS example's message name
    data = sample_bytes("valid_bp_reading")  # OBX-14 2026-11-13T20:53:29Z
    harness.fake.add_message(name, data, labels=map_into_store(name, data))
    event = copy.deepcopy(example("auditevent-send-accepted.json"))
    event["period"] = {"start": "2026-12-19T10:00:00+00:00", "end": "2026-12-19T10:00:01+00:00"}
    event["recorded"] = "2026-12-19T10:00:01+00:00"
    harness.fake.put(AUDIT_STORE, event)

    def trace() -> dict:
        response = harness.get(f"/trace/cuff-attempts/{event['id']}")
        assert response.status_code == 200, response.get_data(as_text=True)
        return response.get_json()

    harness.clock.now = datetime(2026, 12, 19, 12, tzinfo=UTC)  # the reading is 36 days old
    body = trace()
    types = [e["resource"]["resourceType"] for e in body["resources"]["entry"]]
    assert types == ["AuditEvent"]  # the attempt, but not the older Observation or Provenance
    assert body["attempt"]["observation"] is None
    assert body["messageSent"] == data.decode()  # the message itself is the attempt's
    harness.clock.now = datetime(2026, 11, 14, 12, tzinfo=UTC)  # the reading is a day old
    harness._session = None  # the clock went back: start a session valid at this time
    types = [e["resource"]["resourceType"] for e in trace()["resources"]["entry"]]
    assert types == ["AuditEvent", "Observation", "Provenance"]


def test_an_older_unreadable_attempt_is_not_found_not_unreadable(harness, logs) -> None:
    def broken(rid: str, recorded: str) -> None:
        resource = copy.deepcopy(example("auditevent-send-rejected.json"))
        resource["id"] = rid
        resource["outcome"] = "0"  # contradicts the AE: unreadable
        resource["recorded"] = recorded
        harness.fake.put(AUDIT_STORE, resource)

    broken("broken-old", "2025-12-26T11:59:59Z")
    broken("broken-new", "2025-12-26T12:00:01Z")
    _same_as_missing(
        harness, logs, "/trace/cuff-attempts/broken-old", "/trace/cuff-attempts/no-such-attempt"
    )
    assert harness.get("/trace/cuff-attempts/broken-new").status_code == 422


def test_sharing_an_older_reading_is_like_sharing_a_missing_one(tmp_path) -> None:
    from fhir_proxy.share.testing import (
        BP_ID,
        build_share_env,
        make_certificate,
        make_key,
        stored_observation,
    )

    env = build_share_env(tmp_path, make_key(), make_certificate(["127.0.0.1"]))
    old = stored_observation("observation-blood-pressure.json", "pb-bp-old")
    old["effectiveDateTime"] = "2025-12-26T06:59:59-05:00"  # one second before the boundary
    env.store.put(FHIR_STORE, old)
    a, b = env.share(observation_id="pb-bp-old"), env.share(observation_id="no-such-reading")
    assert a.status_code == b.status_code == 404
    assert a.get_data() == b.get_data()
    assert sorted(a.headers.items()) == sorted(b.headers.items())
    assert env.openemr.requests == []
    assert env.share(observation_id=BP_ID).status_code == 200  # inside: shared as before


# ---- the messages log and the status strip cover the same window --------------------------------


def test_the_messages_log_covers_the_window(harness, patient) -> None:  # noqa: ARG001
    second = timedelta(seconds=1)
    old = _attempt(harness, BOUNDARY - second, BOUNDARY + second)
    older = _attempt(harness, BOUNDARY - timedelta(days=3), BOUNDARY - timedelta(days=3))
    new = _attempt(harness, BOUNDARY, BOUNDARY + second)
    name_old = f"{STORE_NAME}/messages/b2xkLXN0b3JlLW9ubHk="
    name_new = f"{STORE_NAME}/messages/bmV3LXN0b3JlLW9ubHk="
    from fhir_proxy.testing import sample_bytes

    data = sample_bytes("valid_bp_reading")
    harness.fake.add_message(name_old, data, create_time="2025-12-26T11:59:59.999000Z")
    harness.fake.add_message(name_new, data, create_time="2025-12-26T12:00:00.000000Z")
    response = harness.get("/messages")
    assert response.status_code == 200
    ids = [row["id"] for row in response.get_json()["entries"]]
    assert new in ids and name_new in ids
    assert old not in ids and older not in ids and name_old not in ids
    audit = [p for _, _, p in harness.fake.calls("GET", "/AuditEvent$") if ("_count", "50") in p]
    assert audit and all(("date", f"ge{BOUNDARY_TEXT}") in p for p in audit)


def test_a_transaction_that_wrote_only_older_windows_is_not_a_row(harness, seed_heart_rate) -> None:
    # A backfill run today that wrote windows from before the window: it names nothing shown.
    seed_heart_rate(BOUNDARY - timedelta(hours=2), 3, prefix="old")
    seed_heart_rate(BOUNDARY - timedelta(minutes=5), 2, prefix="edge")  # 1 ends at, 1 after
    rows = harness.get("/messages?kind=fhir-transaction").get_json()["entries"]
    assert len(rows) == 1
    refs = rows[0]["resources"]
    observations = [r for r in refs if r.startswith("Observation/")]
    assert len(observations) == 2 and all("edge" in r for r in observations)


def test_a_transaction_recorded_before_the_window_is_not_a_row(harness, seed_heart_rate) -> None:
    now = harness.clock.now
    harness.clock.now = BOUNDARY - timedelta(seconds=1)  # the Provenance's recorded time
    seed_heart_rate(BOUNDARY + timedelta(hours=1), 2)
    harness.clock.now = now
    assert harness.get("/messages?kind=fhir-transaction").get_json()["entries"] == []
    params = harness.fake.calls("GET", "/Provenance$")[-1][2]
    assert ("recorded", f"ge{BOUNDARY_TEXT}") in params


def test_a_row_never_links_an_observation_older_than_the_window(harness, map_into_store) -> None:
    from fhir_proxy.testing import sample_bytes

    name = f"{STORE_NAME}/messages/c3RvcmUtb25seS0wMDAx"
    data = sample_bytes("valid_bp_reading")  # OBX-14 2026-11-13T20:53:29Z
    labels = map_into_store(name, data)
    harness.fake.add_message(name, data, labels=labels, create_time="2026-12-20T00:00:00.000Z")
    harness.clock.now = datetime(2026, 11, 20, 12, tzinfo=UTC)  # the reading is 7 days old
    row = next(r for r in harness.get("/messages").get_json()["entries"] if r["id"] == name)
    assert row["observation"] is not None
    # A month later the message (created later still) is listed, but its reading is not shown.
    harness.clock.now = datetime(2026, 12, 20, 12, tzinfo=UTC)
    harness._session = None  # the first session has expired meanwhile: start another
    row = next(r for r in harness.get("/messages").get_json()["entries"] if r["id"] == name)
    assert row["observation"] is None


def test_the_status_counts_cover_the_window(harness, readings) -> None:  # noqa: ARG001
    harness.fake.observation_dates = True  # the store applies R4's rule to the count search
    harness.sender.answer("/status", 200, {"googleHealth": "connected"})
    # Written last, so the newest write overall; but it is not shown, so neither is its time.
    harness.fake.put(FHIR_STORE, heart_rate("hr-written-last", BOUNDARY - timedelta(days=40)))
    body = harness.get("/status").get_json()
    assert body["observationCounts"] == {"watch": 3, "cuff": 1}
    newest_inside = max(
        harness.fake.fhir[FHIR_STORE][("Observation", rid)]["meta"]["lastUpdated"]
        for rid in ("hr-overlap", "hr-ends-at", "hr-inside")
    )
    written_last = harness.fake.fhir[FHIR_STORE][("Observation", "hr-written-last")]
    assert newest_inside < written_last["meta"]["lastUpdated"]
    from fhir_proxy.search import parse_instant

    assert parse_instant(body["watchSync"]["lastSyncAt"]) == parse_instant(newest_inside)
    for _, _, params in harness.fake.calls("GET", "/Observation$"):
        if ("_count", "0") in params or ("_count", "1") in params:
            assert ("date", f"ge{BOUNDARY_TEXT}") in params
            assert "_summary" not in {k for k, _ in params}


# ---- configuration and the CapabilityStatement --------------------------------------------------


def test_history_days_defaults_to_seven_from_one_to_thirty() -> None:
    from tests.test_config_and_extensions import ENV

    assert VARIABLES["HISTORY_DAYS"].default == "7"
    assert ServiceConfig.from_env(ENV).history_days == 7
    assert ServiceConfig.from_env({**ENV, "HISTORY_DAYS": "1"}).history_days == 1
    assert ServiceConfig.from_env({**ENV, "HISTORY_DAYS": "30"}).history_days == 30
    for bad in ("0", "31", "7.5", "seven", "-1"):
        with pytest.raises(ConfigError, match="HISTORY_DAYS"):
            ServiceConfig.from_env({**ENV, "HISTORY_DAYS": bad})


def test_the_capability_statement_documents_the_window(tmp_path) -> None:
    h = build_harness(tmp_path, history_days=3)
    statement = h.get("/fhir/metadata").get_json()
    (observation,) = statement["rest"][0]["resource"]
    date = next(p for p in observation["searchParam"] if p["name"] == "date")
    assert "Only the last 3 days are shown" in date["documentation"]
    assert "ge<now minus 3 days>" in date["documentation"]
    assert "raised to it, not refused" in date["documentation"]
    assert "404 exactly like a missing one" in observation["documentation"]
    assert "Nothing is deleted" in observation["documentation"]
