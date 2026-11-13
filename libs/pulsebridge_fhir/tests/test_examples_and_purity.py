"""Committed examples for T08 (T06 AC 7) and the no-network rule."""

import json
import subprocess
import sys
from pathlib import Path

from pulsebridge_fhir.examples import build_examples, render

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
EXPECTED = {
    "patient.json",
    "device-watch.json",
    "observation-heart-rate.json",
    "observation-blood-pressure.json",
    "provenance-heart-rate.json",
    "bundle-heart-rate-transaction.json",
    "bundle-blood-pressure-transaction.json",
}


def test_examples_exist_for_every_resource_type():
    assert {p.name for p in EXAMPLES.glob("*.json")} == EXPECTED
    types = {json.loads((EXAMPLES / n).read_text())["resourceType"] for n in EXPECTED}
    assert types == {"Patient", "Device", "Observation", "Provenance", "Bundle"}


def test_committed_examples_match_the_code():
    """If this fails, run `python -m pulsebridge_fhir.examples` and commit the result."""
    for name, resource in build_examples().items():
        assert (EXAMPLES / name).read_text(encoding="utf-8") == render(resource), name


def test_examples_are_synthetic():
    text = "".join((EXAMPLES / n).read_text() for n in EXPECTED)
    assert "PB-SYN-" in text
    assert "synthetic-health-user-" in text
    # Only synthetic patient ids appear.
    for name in EXPECTED:
        resource = json.loads((EXAMPLES / name).read_text())
        entries = [e["resource"] for e in resource.get("entry", [])] or [resource]
        for r in entries:
            if r["resourceType"] == "Patient":
                assert all(
                    i["value"].startswith(("PB-SYN-", "synthetic-")) for i in r["identifier"]
                )


def test_library_imports_no_network_or_cloud_modules():
    code = (
        "import sys, pulsebridge_fhir, pulsebridge_fhir.examples\n"
        "bad = [m for m in sys.modules if m.split('.')[0] in "
        "{'socket', 'ssl', 'http', 'requests', 'google', 'urllib3', 'httpx'}"
        " or m == 'urllib.request']\n"
        "print(','.join(sorted(bad)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == ""
