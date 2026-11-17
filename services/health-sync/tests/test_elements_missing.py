"""T39: a stored-window read without the compared elements stops the run (fail closed).

On dev, ``_elements`` with typed choice names (``effectivePeriod,valueQuantity``) made Cloud
Healthcare leave both out; every stored window then looked changed and every run rewrote every
window. Now such a read ends the run as ``fhir-rejected`` / ``stored-elements-missing`` and
writes nothing, so withdrawn and amended windows are never overwritten. ``FhirFake`` applies
``_elements`` as the real store does, so putting the old value back reproduces dev. Synthetic
data only; the clock starts at 2026-01-15T15:07:30Z (health_sync.testing).
"""

from __future__ import annotations

import logging

import pulsebridge_fhir.stored
import pytest
from health_sync.sync import HTTP_STATUS, Outcome, run_sync, split_range, sync_range
from health_sync.testing import (
    FHIR_PATH,
    fhir_ok,
    formatted_logs,
    list_response,
    rollup_page,
    token_response,
    window,
)

OLD_ELEMENTS = "identifier,status,effectivePeriod,valueQuantity,component"
ITEMS = [window(f"2026-01-15T13:{m:02d}:00Z", 60, 61, 62) for m in (0, 5, 10)]


def run(world):
    config = world.config()
    return run_sync(
        config,
        world.deps(),
        ranges=split_range(sync_range(world.clock.now, config.lookback)),
        deadline_seconds=config.run_deadline,
    )


def google(world, items):
    world.google._routes = [r for r in world.google._routes if r.suffix != ":rollUp"]
    world.google.add("POST", "/token", token_response())
    world.google.add("POST", ":rollUp", rollup_page(items))
    world.google.add("GET", "/dataPoints", list_response())


def stored(world, withdrawn: str | None = None) -> None:
    """A first run stores the three windows; one may then be withdrawn by hand."""
    world.fhir_transport.auto_store = True
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    google(world, ITEMS)
    run(world)
    if withdrawn:
        world.fhir_transport.stored[0]["status"] = withdrawn


@pytest.mark.parametrize("withdrawn", [None, "entered-in-error", "cancelled"])
def test_typed_choice_names_stop_the_run_and_write_nothing(world, monkeypatch, caplog, withdrawn):
    stored(world, withdrawn)
    before = [dict(r) for r in world.fhir_transport.stored]
    sent = len(world.bundles())
    monkeypatch.setattr(pulsebridge_fhir.stored, "ELEMENTS", OLD_ELEMENTS)
    google(world, [window(item["startTime"], 50, 55, 58) for item in ITEMS])  # values changed
    with caplog.at_level(logging.WARNING):
        report = run(world)
    assert (report.outcome, report.reason) == (Outcome.FHIR_REJECTED, "stored-elements-missing")
    assert report.stored_read == "failed:elements-missing"
    assert HTTP_STATUS[report.outcome] == 500  # "needs a person": logged at ERROR, alerted on
    assert len(world.bundles()) == sent  # nothing written
    assert world.fhir_transport.stored == before  # the withdrawn window is untouched
    assert report.windows_written == 0
    assert "stored-elements-missing" in formatted_logs(caplog.records)


def test_with_the_base_names_the_same_store_reads_fine(world):
    stored(world, "entered-in-error")
    report = run(world)
    assert (report.outcome, report.stored_read) == (Outcome.SYNCED, "ok")
    assert (report.windows_withdrawn_kept, report.windows_unchanged) == (1, 2)
