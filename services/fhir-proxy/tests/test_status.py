"""``GET /status``: health-sync's state (always 200 here too), counts, newest write."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fhir_proxy.testing import HEALTH_URL, build_harness
from fhir_proxy.upstream import UpstreamError, UpstreamTimeout
from pulsebridge_gcp import HttpResponse

CONNECTED = {
    "googleHealth": "connected",
    "reason": None,
    "notes": ["backfill-needed"],
    "strandedPreliminary": 12,
    "checkedAt": "2026-01-02T11:59:30Z",
}


def _status(harness) -> dict:  # noqa: ANN001
    response = harness.get("/status")
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def test_health_sync_status_is_passed_on_with_state_renamed(harness, seed_heart_rate) -> None:
    seed_heart_rate(datetime(2026, 1, 1, 5, 0, tzinfo=UTC), 4)
    harness.sender.answer("/status", 200, CONNECTED)
    body = _status(harness)
    assert body["watchSync"] == {
        "state": "connected",
        "reason": None,
        "notes": ["backfill-needed"],
        "strandedPreliminary": 12,
        "checkedAt": "2026-01-02T11:59:30Z",
        "lastSyncAt": body["watchSync"]["lastSyncAt"],
    }
    assert "googleHealth" not in body["watchSync"]
    assert body["observationCounts"] == {"watch": 4, "cuff": 0}
    assert body["fhirStore"]["reachable"] is True
    # The share extension (T15) is installed; with no OpenEMR settings its probe says so.
    assert body["openemr"] == {
        "reachable": False,
        "checkedAt": "2026-01-02T12:00:00Z",
        "detail": "not-configured",
    }
    # health-sync was called with the proxy's identity token, audience = its URL.
    (call,) = harness.sender.calls
    assert call["url"] == f"{HEALTH_URL}/status"
    assert (
        call["headers"]["Authorization"]
        == "Bearer fake-identity-token-for-health-sync.example.test"
    )
    assert harness.tokens.audiences == [HEALTH_URL]


def test_last_sync_at_is_the_newest_heart_rate_write(harness, seed_heart_rate) -> None:
    harness.sender.answer("/status", 200, CONNECTED)
    assert _status(harness)["watchSync"]["lastSyncAt"] is None  # no Patient, no data yet
    seed_heart_rate(datetime(2026, 1, 1, 5, 0, tzinfo=UTC), 3)
    harness.clock.advance(61)  # past the store cache
    newest = max(
        r["meta"]["lastUpdated"]
        for (t, _), r in harness.fake.fhir["main"].items()
        if t == "Observation"
    )
    expected = datetime.fromisoformat(newest).astimezone(UTC)
    got = datetime.fromisoformat(_status(harness)["watchSync"]["lastSyncAt"])
    assert got == expected
    newest_searches = [
        c for c in harness.fake.calls("GET", "/Observation$") if ("_sort", "-_lastUpdated") in c[2]
    ]
    assert newest_searches and ("_count", "1") in newest_searches[-1][2]


def test_store_part_is_cached_for_a_short_time(harness, seed_heart_rate) -> None:
    seed_heart_rate(datetime(2026, 1, 1, 5, 0, tzinfo=UTC), 2)
    harness.sender.answer("/status", 200, CONNECTED)
    _status(harness)
    before = len(harness.fake.requests)
    _status(harness)
    assert len(harness.fake.requests) == before
    harness.clock.advance(61)
    _status(harness)
    assert len(harness.fake.requests) > before


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (UpstreamTimeout(), "health-sync-timeout"),
        (UpstreamError("unreachable"), "health-sync-unreachable"),
        (UpstreamError("identity-token-unavailable"), "health-sync-identity-token-unavailable"),
        ((503, {"content-type": "text/html"}, b"<html>down</html>"), "health-sync-error"),
        (
            (500, {"content-type": "application/json"}, b'{"googleHealth": "connected"}'),
            "health-sync-error",
        ),
        ((200, {"content-type": "application/json"}, b"not json"), "health-sync-bad-answer"),
        ((200, {"content-type": "application/json"}, b"null"), "health-sync-bad-answer"),
        (
            (200, {"content-type": "application/json"}, b'{"googleHealth": "ok"}'),
            "health-sync-bad-answer",
        ),
        ((200, {"content-type": "application/json"}, b"[1, 2]"), "health-sync-bad-answer"),
    ],
)
def test_health_sync_down_slow_or_garbled_is_unknown_never_a_5xx(
    harness, answer: object, reason: str
) -> None:
    harness.sender.routes["/status"] = answer
    body = _status(harness)
    assert body["watchSync"]["state"] == "unknown"
    assert body["watchSync"]["reason"] == reason
    assert body["watchSync"]["strandedPreliminary"] is None


def test_health_sync_fields_are_checked_one_by_one(harness) -> None:
    harness.sender.answer(
        "/status",
        200,
        {
            "googleHealth": "reconnect-needed",
            "reason": "refresh-token-invalid",
            "notes": ["backfill-needed", "made-up-note", 7],
            "strandedPreliminary": 2.5,
            "checkedAt": "yesterday",
        },
    )
    watch = _status(harness)["watchSync"]
    assert watch["state"] == "reconnect-needed"
    assert watch["reason"] == "refresh-token-invalid"
    assert watch["notes"] == ["backfill-needed"]
    assert watch["strandedPreliminary"] is None
    assert watch["checkedAt"] == "2026-01-02T12:00:00Z"


def test_unknown_from_health_sync_is_kept_as_a_state(harness) -> None:
    harness.sender.answer(
        "/status", 200, {"googleHealth": "unknown", "reason": "token-endpoint-unavailable"}
    )
    watch = _status(harness)["watchSync"]
    assert watch["state"] == "unknown" and watch["reason"] == "token-endpoint-unavailable"


def test_health_sync_not_deployed(tmp_path) -> None:
    h = build_harness(tmp_path, health_sync_url=None)
    watch = h.get("/status").get_json()["watchSync"]
    assert watch["state"] == "unknown" and watch["reason"] == "health-sync-not-deployed"
    assert h.sender.calls == []


def test_fhir_store_unreachable_is_reported_not_raised(harness, patient) -> None:  # noqa: ARG001
    harness.sender.answer("/status", 200, CONNECTED)
    harness.fake.fail("GET", "/Observation$", HttpResponse(403, b"{}"))
    body = _status(harness)
    assert body["fhirStore"] == {
        "reachable": False,
        "checkedAt": "2026-01-02T12:00:00Z",
        "detail": "permission-denied",
    }
    # Not known, never 0, when the store could not be asked.
    assert body["observationCounts"] == {"watch": None, "cuff": None}
    assert body["watchSync"]["lastSyncAt"] is None
    assert body["watchSync"]["state"] == "connected"


@pytest.mark.parametrize("error", [ValueError("x"), RuntimeError("y"), KeyError("z")])
def test_any_exception_from_health_sync_is_unknown_never_500(harness, error) -> None:
    harness.sender.routes["/status"] = error
    response = harness.get("/status")
    assert response.status_code == 200
    watch = response.get_json()["watchSync"]
    assert watch["state"] == "unknown" and watch["reason"] == "health-sync-error"
