"""pulsebridge_fhir.stored: the "read once, write only what changed" decisions (T35).

Shared by health-sync and the seeder. The service suites test the same rules end to end
through their runs; these tests pin the decision table itself, with literal expectations, and
the pure reading of a search page. All data is synthetic.
"""

import subprocess
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from pulsebridge_fhir.stored import (
    ELEMENTS,
    Plan,
    StoredReadError,
    StoredWindow,
    page_windows,
    parse_stored_window,
    plan_writes,
    search_instant,
    search_token,
    stored_window_search_params,
)

END = datetime(2026, 1, 15, 13, 5, tzinfo=UTC)
OTHER_END = datetime(2026, 1, 15, 13, 4, tzinfo=UTC)
VALUES = (60, 64.5, 71)


def item(key: str, *, status: str = "final", end: datetime = END, values=VALUES) -> dict:
    """A window as the caller sees it; key_of and status_of below read it."""
    return {"key": key, "end": end, "values": values, "status": status}


def key_of(i: dict):
    return i["key"], i["end"], i["values"]


def status_of(i: dict) -> str:
    return i["status"]


def plan(items, stored) -> Plan:
    return plan_writes(items, stored, key_of=key_of, status_of=status_of)


def window(status: str, *, end: datetime | None = END, values=VALUES) -> StoredWindow:
    return StoredWindow(status=status, end=end, values=values)


def written(p: Plan) -> list[str]:
    return [i["key"] for i in p.write]


# --- the decision table ---------------------------------------------------------------------


def test_not_stored_is_written_new():
    p = plan([item("a")], {})
    assert written(p) == ["a"]
    assert (p.new, p.unchanged, p.changed, p.finalised, p.amended) == (1, 0, 0, 0, 0)


def test_failed_read_writes_everything_as_new():
    # stored=None: the read failed or was skipped. Even a key that would be "unchanged" is written.
    items = [item("a"), item("b", status="preliminary")]
    p = plan(items, None)
    assert written(p) == ["a", "b"]
    assert p == Plan(write=items, new=2)


def test_preliminary_now_due_final_is_written_finalised():
    p = plan([item("a", status="final")], {"a": window("preliminary")})
    assert written(p) == ["a"]
    assert (p.finalised, p.amend_keys) == (1, frozenset())


def test_unchanged_final_is_skipped():
    p = plan([item("a", status="final")], {"a": window("final")})
    assert written(p) == []
    assert p.unchanged == 1


def test_unchanged_preliminary_still_preliminary_is_skipped():
    p = plan([item("a", status="preliminary")], {"a": window("preliminary")})
    assert (written(p), p.unchanged) == ([], 1)


def test_unchanged_final_that_would_be_preliminary_now_is_skipped():
    # Already final with the same values: never rewritten, whatever the cutoff says now.
    p = plan([item("a", status="preliminary")], {"a": window("final")})
    assert (written(p), p.unchanged, p.held_final) == ([], 1, 0)


def test_changed_final_complete_now_is_written_amended():
    p = plan([item("a", values=(60, 65.0, 71))], {"a": window("final")})
    assert written(p) == ["a"]
    assert (p.amended, p.changed, p.amend_keys) == (1, 0, frozenset({"a"}))


def test_changed_final_that_would_be_preliminary_now_is_held():
    p = plan([item("a", status="preliminary", values=(60, 65.0, 71))], {"a": window("final")})
    assert written(p) == []
    assert (p.held_final, p.amended, p.amend_keys) == (1, 0, frozenset())


def test_amended_unchanged_is_skipped():
    p = plan([item("a", status="final")], {"a": window("amended")})
    assert (written(p), p.unchanged) == ([], 1)


@pytest.mark.parametrize("status", ["amended", "corrected"])
def test_amended_or_corrected_and_changed_is_written_amended(status):
    p = plan([item("a", values=(61, 64.5, 71))], {"a": window(status)})
    assert (written(p), p.amend_keys) == (["a"], frozenset({"a"}))


@pytest.mark.parametrize("status", ["entered-in-error", "cancelled"])
def test_withdrawn_is_kept_whatever_the_values(status):
    p = plan(
        [item("a"), item("b", values=(1, 2, 3))],
        {"a": window(status), "b": window(status)},
    )
    assert (written(p), p.withdrawn_kept, p.unchanged) == ([], 2, 0)


def test_differing_end_is_written():
    # Same values, another end: a preliminary copy is "changed", a final one "amended".
    p = plan(
        [item("p", status="final"), item("f", status="final")],
        {"p": window("preliminary", end=OTHER_END), "f": window("final", end=OTHER_END)},
    )
    assert written(p) == ["p", "f"]
    assert (p.changed, p.amended, p.finalised, p.amend_keys) == (1, 1, 0, frozenset({"f"}))


def test_unreadable_stored_end_is_written():
    p = plan([item("a")], {"a": window("final", end=None)})
    assert (written(p), p.amended) == (["a"], 1)


def test_preliminary_with_new_values_is_written_changed():
    p = plan([item("a", status="preliminary", values=(60, 66.0, 71))], {"a": window("preliminary")})
    assert (written(p), p.changed) == (["a"], 1)


def test_order_is_kept_and_every_counter_is_its_own():
    items = [
        item("new"),
        item("same"),
        item("fin"),
        item("amd", values=(1, 2, 3)),
        item("chg", status="preliminary", values=(1, 2, 3)),
        item("held", status="preliminary", values=(1, 2, 3)),
        item("gone"),
    ]
    stored = {
        "same": window("final"),
        "fin": window("preliminary"),
        "amd": window("final"),
        "chg": window("preliminary"),
        "held": window("final"),
        "gone": window("entered-in-error"),
    }
    p = plan(items, stored)
    assert written(p) == ["new", "fin", "amd", "chg"]
    assert p == Plan(
        write=[items[0], items[2], items[3], items[4]],
        unchanged=1,
        new=1,
        changed=1,
        finalised=1,
        amended=1,
        withdrawn_kept=1,
        held_final=1,
        amend_keys=frozenset({"amd"}),
    )


def test_is_final():
    assert [window(s).is_final for s in ("final", "amended", "corrected")] == [True] * 3
    assert [window(s).is_final for s in ("preliminary", "registered", "entered-in-error")] == [
        False
    ] * 3


# --- reading a stored Observation and a search page -----------------------------------------


def observation(key: str = "k1", **overrides) -> dict:
    """A stored heart-rate Observation as `_elements` returns it (synthetic values)."""
    resource = {
        "resourceType": "Observation",
        "identifier": [{"system": "urn:pulsebridge:observation", "value": key}],
        "status": "final",
        "effectivePeriod": {
            "start": "2026-01-15T08:00:00-05:00",
            "end": "2026-01-15T08:05:00-05:00",
        },
        "valueQuantity": {"value": 64.5, "unit": "/min"},
        "component": [
            {
                "code": {"coding": [{"system": "http://loinc.org", "code": "96935-2"}]},
                "valueQuantity": {"value": 60},
            },
            {
                "code": {"coding": [{"system": "http://loinc.org", "code": "55426-1"}]},
                "valueQuantity": {"value": 71},
            },
        ],
    }
    resource.update(overrides)
    return resource


def test_parse_reads_key_status_end_and_min_avg_max():
    assert parse_stored_window(observation()) == (
        "k1",
        StoredWindow(status="final", end=END, values=(60, 64.5, 71)),
    )


def test_parse_skips_observations_that_are_not_ours():
    assert (
        parse_stored_window(observation(identifier=[{"system": "urn:other", "value": "k"}])) is None
    )
    assert parse_stored_window(observation(status=None)) is None


def test_parse_turns_unreadable_parts_into_none():
    resource = observation(
        effectivePeriod={"end": "not a time"},
        valueQuantity={"value": True},  # a boolean is not a number
        component=["not an object", {"code": {"coding": ["nor this"]}}],
    )
    assert parse_stored_window(resource) == (
        "k1",
        StoredWindow(status="final", end=None, values=(None, None, None)),
    )


def test_page_windows_in_page_order_with_repeats_and_foreign_entries_skipped():
    page = {
        "resourceType": "Bundle",
        "entry": [
            {"resource": observation("k2")},
            {"resource": {"resourceType": "OperationOutcome"}},
            "not an entry",
            {"resource": observation("k1", status="preliminary")},
            {"resource": observation("k2", status="amended")},
        ],
    }
    assert [(k, w.status) for k, w in page_windows(page)] == [
        ("k2", "final"),
        ("k1", "preliminary"),
        ("k2", "amended"),
    ]
    assert page_windows({"resourceType": "Bundle"}) == []


@pytest.mark.parametrize(
    "page",
    [
        {"resourceType": "OperationOutcome"},
        [],
        None,
        {"resourceType": "Bundle", "entry": {"resource": {}}},
        {"resourceType": "Bundle", "entry": None},
        {"resourceType": "Bundle", "entry": 5},
    ],
)
def test_an_unusable_page_is_a_read_error(page):
    with pytest.raises(StoredReadError):
        page_windows(page)


@pytest.mark.parametrize(
    ("change", "shape"),
    [
        ({"component": [{"code": "8867-4"}]}, "a component code that is a string"),
        ({"component": 5}, "component as a number"),
        ({"identifier": 5}, "identifier as a number"),
        ({"component": [{"code": {"coding": 5}}]}, "coding as a number"),
        ({"component": {"code": {}}}, "component as an object"),
    ],
)
def test_an_observation_with_a_wrong_shape_is_a_read_error_not_a_crash(change, shape):
    page = {"resourceType": "Bundle", "entry": [{"resource": observation(**change)}]}
    with pytest.raises(StoredReadError):
        page_windows(page)


def test_search_parameters_are_the_r4_query():
    berlin = ZoneInfo("Europe/Berlin")
    params = stored_window_search_params(
        "Patient/p-1",
        datetime(2026, 1, 15, 12, 5, tzinfo=UTC),
        datetime(2026, 1, 15, 16, 5, tzinfo=berlin),  # 15:05 UTC
    )
    assert params == [
        ("subject", "Patient/p-1"),
        ("code", "http://loinc.org|8867-4"),
        ("date", "ge2026-01-15T12:05:00Z"),
        ("date", "lt2026-01-15T15:05:00Z"),
        ("_elements", "identifier,status,effectivePeriod,valueQuantity,component"),
        ("_count", "1000"),
    ]
    assert params[4][1] == ELEMENTS


def test_search_values_are_utc_instants_and_escaped_tokens():
    local = datetime(2026, 1, 15, 8, 5, 30, tzinfo=ZoneInfo("America/New_York"))
    assert search_instant(local) == "2026-01-15T13:05:30Z"
    assert search_token("urn:pulsebridge:patient", "SYN-A|B,C") == (
        r"urn:pulsebridge:patient|SYN-A\|B\,C"
    )


def test_the_module_imports_no_network_or_cloud_modules():
    # pulsebridge_fhir stays pure: the search requests are pulsebridge_gcp.stored's.
    code = (
        "import sys, pulsebridge_fhir.stored\n"
        "bad = {m.split('.')[0] for m in sys.modules} & "
        "{'socket', 'ssl', 'http', 'requests', 'google', 'urllib3', 'httpx', 'pulsebridge_gcp'}\n"
        "print(sorted(bad))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
