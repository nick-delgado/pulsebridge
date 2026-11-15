"""One time budget per request (review M3): independent calls run in parallel, no call starts
after the budget is spent, and what is complete is returned."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from fhir_proxy.testing import (
    AUDIT_STORE,
    STORE_NAME,
    build_harness,
    example,
    sample_bytes,
    slow_answer,
)


def _store_message(h, n: int, create: str) -> str:  # noqa: ANN001
    name = f"{STORE_NAME}/messages/c3RvcmUtb25seS0{n:04d}"
    h.fake.add_message(
        name, sample_bytes("valid_bp_reading"), create_time=create, patient_ids=["PB-SYN-0001"]
    )
    return name


def _patient(h) -> None:  # noqa: ANN001
    h.fake.put(
        "main",
        {
            "resourceType": "Patient",
            "id": "pat-1",
            "identifier": [{"system": "urn:pulsebridge:patient", "value": "PB-SYN-0001"}],
        },
    )


def test_store_only_candidates_are_checked_with_one_audit_search(tmp_path) -> None:
    h = build_harness(tmp_path)
    _patient(h)
    h.fake.put(AUDIT_STORE, example("auditevent-send-rejected.json"))
    names = [_store_message(h, n, f"2026-01-01T14:4{n}:00.000000Z") for n in range(8)]
    page = h.get("/messages").get_json()
    assert {r["id"] for r in page["entries"] if r["source"] == "store-only"} == set(names)
    # One search for the attempts, one for all eight candidates (not one per candidate).
    assert len(h.fake.calls("GET", "/AuditEvent$")) == 2
    assert page["partial"] is False


def test_a_refused_guess_over_the_total_is_not_counted_again(tmp_path) -> None:
    h = build_harness(tmp_path, login_failures_total=3)
    login = lambda pw, ip: h.client.post(  # noqa: E731
        "/auth/login", json={"password": pw}, headers={"X-Forwarded-For": ip}
    )
    for n in range(3):
        assert login("wrong", f"198.51.100.{n}").status_code == 401
    h.clock.advance(30)
    for n in range(3):
        assert login("wrong", f"198.51.100.{10 + n}").status_code == 429
    h.clock.advance(31)  # the first three have aged out; the refused three were never counted
    assert login("wrong", "198.51.100.20").status_code == 401


def test_a_truncated_audit_check_lists_no_store_only_row(tmp_path, monkeypatch) -> None:
    from fhir_proxy import messages

    monkeypatch.setattr(messages, "AUDIT_CHECK_PAGES", 2)
    monkeypatch.setattr(messages, "AUDIT_CHECK_PAGE", 1)
    h = build_harness(tmp_path)
    _patient(h)
    for key in ("rejected", "no-ack", "unreadable-reply"):
        name = {
            "rejected": "auditevent-send-rejected.json",
            "no-ack": "auditevent-send-no-ack.json",
            "unreadable-reply": "auditevent-send-unreadable-reply.json",
        }[key]
        h.fake.put(AUDIT_STORE, example(name))
    _store_message(h, 1, "2026-01-01T14:32:00.000000Z")
    page = h.get("/messages").get_json()
    # Three attempts in the check's span, read one per page, two pages allowed: cut off. The
    # message is never guessed to be store-only, and the page says it is incomplete.
    assert [r for r in page["entries"] if r.get("source") == "store-only"] == []
    assert page["partial"] is True


def test_message_lookups_run_in_parallel(tmp_path) -> None:
    h = build_harness(tmp_path)
    _patient(h)
    from pulsebridge_fhir import Acknowledged, SendAttempt, build_send_attempt_audit_event

    for n in range(8):
        sent = datetime(2026, 1, 1, 14, n, tzinfo=UTC)
        name = f"{STORE_NAME}/messages/bWVzc2FnZS0{n:04d}"
        built = build_send_attempt_audit_event(
            SendAttempt(
                sent_at=sent,
                transport="https",
                control_id=f"MSG-{n}",
                result=Acknowledged(code="AA", raw_ack=b"MSH|^~\\&|||||||ACK|X|P|2.5.1\rMSA|AA|X"),
                patient_id="PB-SYN-0001",
                agent="cuff-sim",
                destination=STORE_NAME,
                message_name=name,
            ),
            recorded=sent,
        )
        h.fake.put(AUDIT_STORE, built.resource)
        h.fake.add_message(
            name, sample_bytes("valid_bp_reading"), create_time=f"2026-01-01T14:0{n}:00.5Z"
        )
    h.fake.delays.append(("GET", r"/messages/bWVzc2Fn", 0.5))
    started = time.monotonic()
    page = h.get("/messages").get_json()
    elapsed = time.monotonic() - started
    assert all(
        r["messageLookup"] == "confirmed" for r in page["entries"] if r["kind"] == "cuff-send"
    )
    assert elapsed < 2.5, elapsed  # eight 0.5 s lookups in a row would take 4 s


def test_the_budget_ends_a_slow_log_page_with_a_partial_answer(tmp_path) -> None:
    h = build_harness(tmp_path, request_budget=1.5)
    _patient(h)
    h.fake.put(AUDIT_STORE, example("auditevent-send-accepted.json"))
    h.fake.add_message(
        f"{STORE_NAME}/messages/synthetic-message-0002", sample_bytes("valid_bp_reading")
    )
    h.fake.delays.append(("GET", r"/messages/synthetic", 5))
    token = h.login()
    started = time.monotonic()
    response = h.client.get("/messages", headers={"Authorization": f"Bearer {token}"})
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    page = response.get_json()
    assert elapsed < 3, elapsed
    assert page["partial"] is True
    (row,) = [r for r in page["entries"] if r["kind"] == "cuff-send"]
    assert row["messageLookup"] == "failed" and row["application"] is None


def test_status_parts_run_at_once_and_unanswered_parts_are_unknown(tmp_path) -> None:
    h = build_harness(tmp_path, request_budget=2)
    _patient(h)
    h.sender.routes["/status"] = slow_answer(10, 200, {"googleHealth": "connected"})
    h.fake.delays.append(("GET", r"/Observation$", 10))
    token = h.login()
    started = time.monotonic()
    body = h.client.get("/status", headers={"Authorization": f"Bearer {token}"}).get_json()
    elapsed = time.monotonic() - started
    assert elapsed < 3.5, elapsed  # not 10 + 3 x 10 s
    assert body["watchSync"]["state"] == "unknown"
    assert body["watchSync"]["reason"] == "health-sync-timeout"
    assert body["observationCounts"] == {"watch": None, "cuff": None}
    assert body["fhirStore"]["reachable"] is False


def test_status_parts_overlap_when_each_is_slow_but_in_budget(tmp_path) -> None:
    h = build_harness(tmp_path, request_budget=30)
    _patient(h)
    upstream: list = []
    h.sender.routes["/status"] = slow_answer(1, 200, {"googleHealth": "connected"}, upstream)
    h.fake.delays.append(("GET", r"/Observation$", 1))
    token = h.login()
    body = h.client.get("/status", headers={"Authorization": f"Bearer {token}"}).get_json()
    assert body["watchSync"]["state"] == "connected"
    assert body["observationCounts"] == {"watch": 0, "cuff": 0}
    # Event order, not wall time: health-sync's call and the store searches overlapped (each
    # began before the other ended), and the three searches overlapped each other.
    ((_, hs_start, hs_end),) = upstream
    searches = [(a, b) for p, a, b in h.fake.timeline if p.endswith("/Observation")]
    assert len(searches) == 3
    assert all(a < hs_end and hs_start < b for a, b in searches)
    assert max(a for a, _ in searches) < min(b for _, b in searches)


def test_status_answers_promptly_while_the_messages_pool_is_saturated(tmp_path) -> None:
    import threading

    from pulsebridge_fhir import Acknowledged, SendAttempt, build_send_attempt_audit_event

    h = build_harness(tmp_path, request_budget=30)
    _patient(h)
    for n in range(16):
        sent = datetime(2026, 1, 1, 14, n, tzinfo=UTC)
        name = f"{STORE_NAME}/messages/c2F0dXJhdGU{n:04d}"
        built = build_send_attempt_audit_event(
            SendAttempt(
                sent_at=sent,
                transport="https",
                control_id=f"MSG-S{n}",
                result=Acknowledged(code="AA", raw_ack=b"MSH|^~\\&|||||||ACK|X|P|2.5.1\rMSA|AA|X"),
                patient_id="PB-SYN-0001",
                agent="cuff-sim",
                destination=STORE_NAME,
                message_name=name,
            ),
            recorded=sent,
        )
        h.fake.put(AUDIT_STORE, built.resource)
    h.fake.delays.append(("GET", r"/messages/c2F0dXJh", 1))
    h.sender.answer("/status", 200, {"googleHealth": "connected"})
    token = h.login()
    auth = {"Authorization": f"Bearer {token}"}
    threads = [
        threading.Thread(target=lambda: h.app.test_client().get("/messages?count=16", headers=auth))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    time.sleep(0.3)  # the shared pool is now busy with 8 x 16 one-second lookups
    started = time.monotonic()
    response = h.app.test_client().get("/status", headers=auth)
    elapsed = time.monotonic() - started
    for t in threads:
        t.join(timeout=60)
    assert response.status_code == 200
    assert elapsed < 1.0, elapsed


def test_failed_lookups_mark_the_page_partial(tmp_path) -> None:
    from pulsebridge_gcp import HttpResponse

    h = build_harness(tmp_path)
    _patient(h)
    h.fake.put(AUDIT_STORE, example("auditevent-send-accepted.json"))
    h.fake.fail("GET", r"/messages/synthetic", HttpResponse(503, b"{}"))
    page = h.get("/messages").get_json()
    assert page["partial"] is True
    (row,) = [r for r in page["entries"] if r["kind"] == "cuff-send"]
    assert row["messageLookup"] == "failed"


def test_an_inner_status_search_unfinished_at_the_deadline_is_unknown(tmp_path) -> None:
    h = build_harness(tmp_path, request_budget=1)
    _patient(h)
    h.sender.answer("/status", 200, {"googleHealth": "connected"})
    h.fake.delays.append(("GET", r"/Observation$", 5))
    token = h.login()
    response = h.client.get("/status", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["watchSync"]["state"] == "connected"  # health-sync answered in time
    assert body["observationCounts"] == {"watch": None, "cuff": None}
    assert body["watchSync"]["lastSyncAt"] is None
    assert body["fhirStore"]["reachable"] is False


def test_every_route_has_the_budget(tmp_path) -> None:
    h = build_harness(tmp_path, request_budget=1)
    _patient(h)
    h.fake.delays.append(("GET", r"/Observation/", 10))
    token = h.login()
    auth = {"Authorization": f"Bearer {token}"}
    started = time.monotonic()
    assert h.client.get("/trace/heart-rate/abc", headers=auth).status_code == 504
    assert time.monotonic() - started < 2


def test_no_call_starts_once_the_budget_is_spent(tmp_path) -> None:
    h = build_harness(tmp_path, request_budget=1)
    _patient(h)
    h.fake.put(
        "main",
        {
            "resourceType": "Observation",
            "id": "hr-1",
            "subject": {"reference": "Patient/pat-1"},
            "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
            "device": {"reference": "Device/dev-1"},
        },
    )
    token = h.login()
    h.get("/fhir/metadata", headers={"Authorization": f"Bearer {token}"})
    h.fake.delays.append(("GET", r"/Observation/hr-1$", 0.98))  # uses up the whole budget
    before = len(h.fake.requests)
    response = h.client.get("/trace/heart-rate/hr-1", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 504
    later = [r[1] for r in h.fake.requests[before:]]
    assert not [p for p in later if "/Device/" in p or "/Provenance/" in p], later


def test_a_slow_cuff_sim_ends_at_the_forward_timeout(tmp_path) -> None:
    h = build_harness(tmp_path, cuff_forward_timeout=1, request_budget=30)
    h.sender.routes["/cuff/readings"] = slow_answer(10, 200, {})
    token = h.login()
    started = time.monotonic()
    response = h.client.post(
        "/cuff/readings",
        data=b"{}",
        content_type="application/json",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 504
    assert time.monotonic() - started < 2


def test_upstream_calls_get_at_most_the_time_left(tmp_path) -> None:
    h = build_harness(tmp_path, request_budget=2)
    h.sender.answer("/status", 200, {"googleHealth": "connected"})
    h.get("/status")
    (call,) = h.sender.calls
    # health-sync's own timeout is 25 s; the call may only use what is left of the 2 s budget.
    assert call["timeout"] <= 2


def test_an_inner_status_search_still_running_at_the_deadline_is_never_in_the_answer(
    tmp_path,
) -> None:
    h = build_harness(tmp_path, request_budget=1)
    _patient(h)
    h.sender.answer("/status", 200, {"googleHealth": "connected"})
    h.fake.stubborn_delays.append(("GET", r"/Observation$", 2.5))  # outlives the budget
    token = h.login()
    response = h.client.get("/status", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["observationCounts"] == {"watch": None, "cuff": None}
    assert body["fhirStore"]["reachable"] is False


def test_the_store_part_itself_never_returns_an_unfinished_search(tmp_path) -> None:
    """The inner guard on its own (the outer wait usually covers it, but the two deadlines can
    race): the store part, run directly under a short budget, gives unknown counts."""
    from fhir_proxy import deadline

    h = build_harness(tmp_path)
    _patient(h)
    status_service = h.app.extensions["pulsebridge_status"]
    status_service._cached = None
    h.fake.stubborn_delays.append(("GET", r"/Observation$", 1.5))
    import contextvars

    def run() -> dict:
        deadline.start(0.5)
        return status_service._from_store(datetime(2026, 1, 2, 12, tzinfo=UTC))

    result = contextvars.copy_context().run(run)
    assert result["observationCounts"] == {"watch": None, "cuff": None}
    assert result["fhirStore"]["reachable"] is False
