"""The messages log: AuditEvents joined to stored messages, store-only rows, transactions."""

from __future__ import annotations

import base64
import copy
from datetime import UTC, datetime

import pytest
from fhir_proxy.messages import Cursor, Item, Stream, decode_cursor, merge
from fhir_proxy.testing import AUDIT_STORE, STORE_NAME, example, sample_bytes
from pulsebridge_gcp import HttpResponse, TimedOutError

HTTPS_NAME = f"{STORE_NAME}/messages/synthetic-message-0002"
DERIVED_NAME = f"{STORE_NAME}/messages/c3ludGhldGljLWRlcml2ZWQtbWVzc2FnZS0wMDA2LTA="
EXAMPLES = {
    "https": "auditevent-send-accepted.json",
    "mllp": "auditevent-send-accepted-mllp.json",
    "rejected": "auditevent-send-rejected.json",
    "no-ack": "auditevent-send-no-ack.json",
    "unreadable-reply": "auditevent-send-unreadable-reply.json",
}


def _ids() -> dict[str, str]:
    return {key: example(name)["id"] for key, name in EXAMPLES.items()}


def _put_examples(harness, *keys: str) -> None:  # noqa: ANN001
    for key in keys or EXAMPLES:
        harness.fake.put(AUDIT_STORE, example(EXAMPLES[key]))


def _rows(harness, query: str = "") -> dict:  # noqa: ANN001
    response = harness.get(f"/messages{query}")
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def _by_id(page: dict) -> dict[str, dict]:
    return {row["id"]: row for row in page["entries"]}


@pytest.fixture
def stored(harness, map_into_store):  # noqa: ANN001, ANN201
    """The HTTPS example's message, mapped (accepted); the MLLP example's derived name stored
    with an unmappable sample (error)."""
    https_bytes = sample_bytes("valid_bp_reading")
    labels = map_into_store(HTTPS_NAME, https_bytes)
    assert labels == {"application_outcome": "accepted", "reason": "mapped"}
    harness.fake.add_message(
        HTTPS_NAME,
        https_bytes,
        labels={**labels, "transport": "https"},
        create_time="2026-01-01T14:30:00.900000Z",
    )
    mllp_bytes = sample_bytes("unmappable_missing_obx14")
    mllp_labels = map_into_store(DERIVED_NAME, mllp_bytes)
    harness.fake.add_message(
        DERIVED_NAME, mllp_bytes, labels=mllp_labels, create_time="2026-01-01T14:30:31.000000Z"
    )
    return {"https": https_bytes, "mllp": mllp_bytes}


def test_every_example_becomes_a_row_newest_first(harness, stored) -> None:  # noqa: ARG001
    _put_examples(harness)
    page = _rows(harness, "?kind=cuff-send")
    ids = _ids()
    assert [r["id"] for r in page["entries"]] == [
        ids["unreadable-reply"],  # 14:33:01
        ids["no-ack"],  # 14:32:05
        ids["rejected"],  # 14:31:01
        ids["mllp"],  # 14:30:31.250
        ids["https"],  # 14:30:01.125
    ]
    assert page["nextCursor"] is None
    assert page["unreadable"] == 0


def test_https_attempt_joined_by_the_store_returned_name(harness, stored) -> None:  # noqa: ARG001
    _put_examples(harness, "https")
    row = _by_id(_rows(harness))[_ids()["https"]]
    assert row["source"] == "audit-event"
    assert row["transport"] == "https"
    assert row["messageName"] == HTTPS_NAME
    assert row["derivedMessageName"] is None
    assert row["messageLookup"] == "confirmed"
    assert row["result"] == "accepted" and row["outcome"] == "0"
    assert row["commitAck"]["received"] is True and row["commitAck"]["code"] == "AA"
    assert row["commitAck"]["raw"].startswith("MSH|")  # decoded from ack-message
    assert row["application"] == {"outcome": "accepted", "reason": None}
    assert row["observation"].startswith("Observation/")
    # The Observation it names is the one the mapper wrote for this message.
    obs_id = row["observation"].split("/")[1]
    assert ("Observation", obs_id) in harness.fake.fhir["main"]


def test_mllp_attempt_joined_by_the_derived_name_after_a_get(harness, stored) -> None:  # noqa: ARG001
    _put_examples(harness, "mllp")
    row = _by_id(_rows(harness))[_ids()["mllp"]]
    assert row["messageName"] is None  # never the derived name
    assert row["derivedMessageName"] == DERIVED_NAME
    assert row["messageLookup"] == "confirmed"
    assert row["application"] == {"outcome": "error", "reason": "missing-obx-14"}
    assert row["observation"] is None
    gets = harness.fake.calls("GET", "/messages/c3lud")
    assert len(gets) == 1 and ("view", "RAW_ONLY") in gets[0][2]


def test_derived_name_that_the_store_does_not_have_is_not_confirmed(harness) -> None:
    _put_examples(harness, "mllp")
    row = _by_id(_rows(harness))[_ids()["mllp"]]
    assert row["derivedMessageName"] == DERIVED_NAME
    assert row["messageLookup"] == "not-found"
    assert row["application"] is None  # no outcome is claimed for a message we could not find
    assert row["observation"] is None
    assert row["result"] == "accepted"  # the commit-level ACK still says AA


def test_lookup_that_fails_is_reported_as_failed(harness, stored) -> None:  # noqa: ARG001
    _put_examples(harness, "mllp")
    harness.fake.fail("GET", "/messages/c3lud", TimedOutError("GET message", None))
    row = _by_id(_rows(harness))[_ids()["mllp"]]
    assert row["messageLookup"] == "failed"
    assert row["application"] is None


def test_a_derived_name_outside_our_store_is_never_fetched(harness) -> None:
    event = example(EXAMPLES["mllp"])
    other_store = STORE_NAME.replace("example-store", "other-store")
    for entity in event["entity"]:
        for detail in entity.get("detail", []):
            if detail["type"] == "derived-message-name":
                detail["valueString"] = f"{other_store}/messages/abc="
    for agent in event["agent"]:
        if agent["who"]["identifier"]["system"] == "urn:pulsebridge:hl7v2-store":
            agent["who"]["identifier"]["value"] = other_store
    harness.fake.put(AUDIT_STORE, event)
    page = _rows(harness)
    assert event["id"] not in _by_id(page)  # another store's attempt: skipped (the pin)
    assert harness.fake.calls("GET", "/hl7V2Stores/other-store") == []


def test_rejected_attempt_shows_the_reason_and_raw_ack(harness) -> None:
    _put_examples(harness, "rejected")
    row = _by_id(_rows(harness))[_ids()["rejected"]]
    assert row["result"] == "rejected" and row["outcome"] == "4"
    assert row["sample"] == "unparseable_no_msh"
    assert row["controlId"] is None
    ack = row["commitAck"]
    assert ack["received"] is True and ack["code"] == "AE"
    assert ack["reason"] == "Invalid MSH header"
    assert "MSA|AE" in ack["raw"]
    assert row["application"] is None and row["messageLookup"] is None
    assert harness.fake.calls("GET", "/messages/") == []


@pytest.mark.parametrize(
    ("key", "why"), [("no-ack", "timeout"), ("unreadable-reply", "unreadable-reply")]
)
def test_unacknowledged_attempts_show_why(harness, key: str, why: str) -> None:
    _put_examples(harness, key)
    row = _by_id(_rows(harness))[_ids()[key]]
    assert row["result"] == "no-acknowledgment" and row["outcome"] == "8"
    ack = row["commitAck"]
    assert ack["received"] is False and ack["why"] == why
    assert ack["detail"]
    if why == "unreadable-reply":
        prefix = base64.b64decode(ack["replyPrefixBase64"])
        assert prefix.startswith(b"\xef\xbb\xbfMSH")
    else:
        assert ack["replyPrefixBase64"] is None
    assert row["application"] is None


def test_api_error_attempt_keeps_its_http_status(harness) -> None:
    from pulsebridge_fhir import NotAcknowledged, SendAttempt, build_send_attempt_audit_event

    sent = datetime(2026, 1, 1, 14, 40, tzinfo=UTC)
    built = build_send_attempt_audit_event(
        SendAttempt(
            sent_at=sent,
            transport="https",
            control_id="MSG-SYN-0009",
            result=NotAcknowledged(why="api-error", http_status=503),
            patient_id="PB-SYN-0001",
            agent="cuff-sim",
            destination=STORE_NAME,
        ),
        recorded=sent,
    )
    harness.fake.put(AUDIT_STORE, built.resource)
    row = _by_id(_rows(harness))[built.resource["id"]]
    assert row["commitAck"]["why"] == "api-error" and row["commitAck"]["httpStatus"] == 503


def test_unreadable_audit_event_is_a_visible_row_and_counted(harness, logs) -> None:
    broken = copy.deepcopy(example(EXAMPLES["rejected"]))
    broken["id"] = "broken-attempt-1"
    for entity in broken["entity"]:
        entity["detail"] = [d for d in entity.get("detail", []) if d["type"] != "transport"]
    harness.fake.put(AUDIT_STORE, broken)
    page = _rows(harness)
    row = _by_id(page)["broken-attempt-1"]
    assert row["kind"] == "unreadable-send-attempt"
    assert row["recordedAt"] == "2026-01-01T14:31:01.000Z"
    assert "transport" in row["problem"]
    assert page["unreadable"] == 1
    assert any(e.get("event") == "unreadable-send-attempt" for e in logs.events())
    only = _rows(harness, "?kind=unreadable-send-attempt")
    assert [r["id"] for r in only["entries"]] == ["broken-attempt-1"]


def test_audit_event_that_is_not_ours_is_skipped(harness) -> None:
    foreign = copy.deepcopy(example(EXAMPLES["https"]))
    foreign["id"] = "foreign-1"
    foreign["source"]["observer"]["identifier"]["system"] = "urn:example:other-system"
    harness.fake.put(AUDIT_STORE, foreign)
    page = _rows(harness)
    assert "foreign-1" not in _by_id(page)
    assert page["unreadable"] == 0


def test_another_patients_attempt_is_not_listed(harness) -> None:
    from pulsebridge_fhir import Acknowledged, SendAttempt, build_send_attempt_audit_event

    sent = datetime(2026, 1, 1, 14, 41, tzinfo=UTC)
    built = build_send_attempt_audit_event(
        SendAttempt(
            sent_at=sent,
            transport="mllp",
            control_id="MSG-SYN-0010",
            result=Acknowledged(code="AE", raw_ack=b"MSH|^~\\&|||||||ACK|X|P|2.5.1\rMSA|AE|"),
            patient_id="PB-SYN-9999",
            agent="cuff-sim",
            destination=STORE_NAME,
        ),
        recorded=sent,
    )
    harness.fake.put(AUDIT_STORE, built.resource)
    assert built.resource["id"] not in _by_id(_rows(harness))


# ---- stored messages and their outcome labels --------------------------------------------------


def test_store_only_message_is_listed(harness, map_into_store) -> None:
    _put_examples(harness, "rejected")
    name = f"{STORE_NAME}/messages/c3RvcmUtb25seS0wMDAx"
    data = sample_bytes("valid_bp_reading_offset")
    labels = map_into_store(name, data, prefix="so")
    harness.fake.add_message(
        name,
        data,
        labels=labels,
        create_time="2026-01-01T14:35:00.000000Z",
        patient_ids=["PB-SYN-0001"],
    )
    row = _by_id(_rows(harness))[name]
    assert row["source"] == "store-only"
    assert row["transport"] is None  # no transport label: unknown, not "mllp"
    assert row["commitAck"] == {
        "received": True,
        "code": "AA",
        "controlId": None,
        "reason": None,
        "raw": None,
    }
    assert row["application"] == {"outcome": "accepted", "reason": None}
    assert row["observation"].startswith("Observation/")
    assert row["controlId"]  # read from the stored bytes with hl7apy
    assert row["recordedAt"] == "2026-01-01T14:35:00.000Z"


def test_a_stored_message_with_an_audit_event_is_not_store_only(harness, stored) -> None:  # noqa: ARG001
    _put_examples(harness, "https", "mllp")
    rows = _rows(harness)["entries"]
    assert [r["source"] for r in rows if r["kind"] == "cuff-send"] == ["audit-event"] * 2


def test_store_only_message_whose_attempt_is_on_another_page_is_not_doubled(
    harness, stored
) -> None:  # noqa: ARG001
    _put_examples(harness)
    seen = []
    cursor = None
    for _ in range(10):
        query = "?kind=cuff-send&count=1" + (f"&cursor={cursor}" if cursor else "")
        page = _rows(harness, query)
        seen += [r["id"] for r in page["entries"]]
        cursor = page["nextCursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(_ids().values())


def test_another_patients_stored_message_is_not_listed(harness) -> None:
    name = f"{STORE_NAME}/messages/b3RoZXItcGF0aWVudA=="
    harness.fake.add_message(
        name,
        sample_bytes("valid_bp_reading", patient_id="PB-SYN-9999"),
        create_time="2026-01-01T14:35:00.000000Z",
        patient_ids=["PB-SYN-9999"],
    )
    assert name not in _by_id(_rows(harness))


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (None, {"outcome": "pending", "reason": None}),
        ({"transport": "https"}, {"outcome": "pending", "reason": None}),
        (
            {"application_outcome": "accepted", "reason": "mapped"},
            {"outcome": "accepted", "reason": None},
        ),
        (
            {"application_outcome": "error", "reason": "missing-obx-14"},
            {"outcome": "error", "reason": "missing-obx-14"},
        ),
        (
            {"application_outcome": "error", "reason": "BAD REASON!"},
            {"outcome": "error", "reason": None},
        ),
        ({"application_outcome": "maybe"}, {"outcome": "pending", "reason": None}),
    ],
)
def test_application_outcome_from_labels(harness, labels, expected) -> None:
    _put_examples(harness, "https")
    harness.fake.add_message(HTTPS_NAME, sample_bytes("valid_bp_reading"), labels=labels)
    row = _by_id(_rows(harness))[_ids()["https"]]
    assert row["application"] == expected
    if expected["outcome"] != "accepted":
        assert row["observation"] is None


@pytest.mark.parametrize("outcome", ["accepted", "error", "pending"])
def test_outcome_filter(harness, stored, outcome: str) -> None:  # noqa: ARG001
    _put_examples(harness)
    pending_name = f"{STORE_NAME}/messages/cGVuZGluZw=="
    harness.fake.add_message(
        pending_name, sample_bytes("valid_bp_reading"), create_time="2026-01-01T14:36:00.000000Z"
    )
    rows = _rows(harness, f"?outcome={outcome}")["entries"]
    expected = {
        "accepted": {_ids()["https"]},
        "error": {_ids()["mllp"]},
        "pending": {pending_name},
    }[outcome]
    assert {r["id"] for r in rows} == expected
    assert all(r["application"]["outcome"] == outcome for r in rows)


# ---- FHIR transaction rows ---------------------------------------------------------------------


def test_fhir_transaction_rows_come_from_provenance(harness, seed_heart_rate) -> None:
    seed_heart_rate(datetime(2026, 1, 1, 5, 0, tzinfo=UTC), 3)
    rows = _rows(harness, "?kind=fhir-transaction")["entries"]
    assert len(rows) == 1
    (row,) = rows
    assert row["writer"] == "health-sync"
    assert row["httpStatus"] is None
    assert row["recordedAt"] == "2026-01-02T12:00:00.000Z"
    observations = [r for r in row["resources"] if r.startswith("Observation/")]
    provenances = [r for r in row["resources"] if r.startswith("Provenance/")]
    assert len(observations) == len(provenances) == 3


def test_transaction_rows_are_merged_with_attempts_newest_first(harness, seed_heart_rate) -> None:
    _put_examples(harness, "rejected", "no-ack")
    seed_heart_rate(datetime(2026, 1, 1, 5, 0, tzinfo=UTC), 2)
    kinds = [r["kind"] for r in _rows(harness)["entries"]]
    assert kinds == ["fhir-transaction", "cuff-send", "cuff-send"]


# ---- paging and the cursor ----------------------------------------------------------------------


def test_paging_through_everything_shows_each_row_once(harness, stored, seed_heart_rate) -> None:  # noqa: ARG001
    _put_examples(harness)
    seed_heart_rate(datetime(2026, 1, 1, 5, 0, tzinfo=UTC), 2)
    everything = {r["id"] for r in _rows(harness, "?count=50")["entries"]}
    seen: list[str] = []
    cursor = None
    for _ in range(20):
        page = _rows(harness, "?count=2" + (f"&cursor={cursor}" if cursor else ""))
        seen += [r["id"] for r in page["entries"]]
        cursor = page["nextCursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen))
    assert set(seen) == everything


@pytest.mark.parametrize(
    "query",
    [
        "?cursor=not!base64",
        "?cursor="
        + base64.urlsafe_b64encode(b'{"v":2,"b":"2026-01-01T00:00:00Z","s":[]}')
        .decode()
        .rstrip("="),
        "?cursor="
        + base64.urlsafe_b64encode(b'{"v":1,"b":"yesterday","s":[]}').decode().rstrip("="),
        "?cursor="
        + base64.urlsafe_b64encode(b'{"v":1,"b":"2026-01-01T00:00:00Z","s":["../x y"]}')
        .decode()
        .rstrip("="),
        "?kind=everything",
        "?outcome=maybe",
        "?count=0",
        "?count=-1",
        "?count=abc",
        "?count=1&count=2",
        "?patient=Patient/other",
        "?store=audit",
    ],
)
def test_bad_messages_queries_are_refused(harness, query: str) -> None:
    response = harness.get(f"/messages{query}")
    assert response.status_code == 400
    assert response.get_json()["resourceType"] == "OperationOutcome"


def test_count_is_capped(harness) -> None:
    _rows(harness, "?count=999")
    (call,) = harness.fake.calls("GET", "/AuditEvent$")
    assert ("_count", "50") in call[2]


def test_cursor_round_trip() -> None:
    cursor = Cursor(datetime(2026, 1, 1, 14, 30, 1, 125000, tzinfo=UTC), frozenset({"a", "b"}))
    assert decode_cursor(cursor.encode()) == cursor


def test_merge_never_skips_what_a_truncated_source_may_still_hold() -> None:
    t = lambda m: datetime(2026, 1, 1, 12, m, tzinfo=UTC)  # noqa: E731
    attempts = Stream([Item(t(50), "a1", "attempt"), Item(t(40), "a2", "attempt")], True, t(40))
    # The transactions source returned more than enough, down to minute 10.
    txs = Stream([Item(t(m), f"t{m}", "transaction") for m in (45, 35, 20, 10)])
    merged = merge([attempts, txs], 10, None)
    # Nothing older than minute 40 may be shown yet: the attempts source may have more there.
    assert [i.id for i in merged.page] == ["a1", "t45", "a2"]
    assert merged.more and merged.lower == t(40)


def test_store_failure_on_the_log_is_a_bounded_error(harness) -> None:
    harness.fake.fail("GET", "/AuditEvent$", HttpResponse(403, b"{}"))
    response = harness.get("/messages")
    assert response.status_code == 502
    assert response.get_json()["issue"][0]["details"]["coding"][0]["code"] == (
        "upstream-permission-denied"
    )
