#!/usr/bin/env python3
"""Prove that the reviewer password never reaches Terraform state (task T04).

What it does, with no cloud credentials:

1. Generates a fresh random dummy password (never a real one, never printed).
2. Runs `terraform test` on tests/state_scan/ with a mock Google provider and the dummy as
   `reviewer_password`. Each run block applies the module; `-json -verbose` makes Terraform emit
   the resulting state as a `test_state` message, in the same JSON format as
   `terraform show -json`, which prints sensitive values in clear text.
3. Writes each run's state to a file and searches it:
   - `defaults` and `dev_password` (the real module) must not contain the dummy anywhere;
   - `control` (a helper that stores the dummy through `secret_data`, the in-state argument)
     must contain it, which proves the search would find a leak;
   - the real module may contain only the expected resource types, and in particular no
     `google_service_account_key`;
   - with the defaults, no secret version exists at all.

`terraform test` keeps state in memory and offers no stable way to write it to disk (a `backend`
block in a run is experimental-only in Terraform 1.17), which is why the state is taken from the
JSON stream.

Usage: python3 tests/state_scan.py [--keep DIR]   (run from anywhere; exit code 0 = pass)
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent.parent

# Resource types the identity module may create. Anything else (above all a
# google_service_account_key, or an authoritative *_iam_binding / *_iam_policy) fails the scan.
ALLOWED_TYPES = {
    "google_service_account",
    "google_project_iam_member",
    "google_service_account_iam_member",
    "google_secret_manager_secret",
    "google_secret_manager_secret_iam_member",
    "google_secret_manager_secret_version",
    "google_project",  # data source: project number for the Pub/Sub service agent
}
FORBIDDEN_TYPES = {"google_service_account_key"}

MODULE_RUNS = ("defaults", "dev_password")
CONTROL_RUN = "control"


def iter_resources(module: dict):
    """Yield every resource in a `terraform show -json` style module, including child modules."""
    yield from module.get("resources", [])
    for child in module.get("child_modules", []):
        yield from iter_resources(child)


def run_terraform(dummy: str) -> list[dict]:
    env = dict(os.environ, TF_VAR_reviewer_password=dummy, TF_IN_AUTOMATION="1")
    subprocess.run(
        [
            "terraform",
            f"-chdir={MODULE_DIR}",
            "init",
            "-backend=false",
            "-input=false",
            "-no-color",
            # Installs the helper module the control run uses.
            "-test-directory=tests/state_scan",
        ],
        check=True,
        env=env,
        stdout=subprocess.DEVNULL,
    )
    result = subprocess.run(
        [
            "terraform",
            f"-chdir={MODULE_DIR}",
            "test",
            "-test-directory=tests/state_scan",
            "-json",
            "-verbose",
            "-no-color",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    messages = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    summary = [m for m in messages if m.get("type") == "test_summary"]
    if result.returncode != 0 or not summary or summary[-1]["test_summary"]["status"] != "pass":
        for m in messages:
            if m.get("type") == "diagnostic" or m.get("@level") == "error":
                # Diagnostics never contain the dummy: it is sensitive and Terraform redacts it.
                print(m.get("@message", ""), file=sys.stderr)
        raise SystemExit("FAIL: terraform test did not pass; the state scan cannot run")
    return messages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", type=Path, help="write the state files here and keep them")
    args = parser.parse_args()

    # Synthetic, random each time, at least 16 characters (the variable's minimum).
    dummy = "state-scan-dummy-" + secrets.token_hex(16)
    messages = run_terraform(dummy)

    states = {m["@testrun"]: m["test_state"] for m in messages if m.get("type") == "test_state"}
    missing = {*MODULE_RUNS, CONTROL_RUN} - states.keys()
    if missing:
        raise SystemExit(f"FAIL: no state captured for run(s): {sorted(missing)}")

    out_dir = args.keep or Path(tempfile.mkdtemp(prefix="identity-state-scan-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    for run, state in states.items():
        path = out_dir / f"{run}.tfstate.json"
        path.write_text(json.dumps(state, indent=2))
        text = path.read_text()
        found = dummy in text
        if run == CONTROL_RUN:
            if found:
                print(f"{run}: dummy found in state as expected (positive control) [{path}]")
            else:
                failures.append("control: dummy NOT found, so the search cannot detect a leak")
            continue

        if found:
            failures.append(f"{run}: the dummy password IS in state ({path})")
        else:
            print(f"{run}: dummy password not found in state [{path}]")

        resources = list(iter_resources(state.get("root_module", {})))
        types = {r["type"] for r in resources}
        if types & FORBIDDEN_TYPES:
            failures.append(f"{run}: forbidden resource type(s) {sorted(types & FORBIDDEN_TYPES)}")
        if types - ALLOWED_TYPES - FORBIDDEN_TYPES:
            failures.append(f"{run}: unexpected resource type(s) {sorted(types - ALLOWED_TYPES)}")
        versions = [r for r in resources if r["type"] == "google_secret_manager_secret_version"]
        if run == "defaults" and versions:
            failures.append(f"defaults: {len(versions)} secret version(s) exist; expected none")
        if run == "dev_password":
            if len(versions) != 1:
                failures.append(f"dev_password: expected 1 secret version, found {len(versions)}")
            for v in versions:
                for attr in ("secret_data", "secret_data_wo"):
                    if v["values"].get(attr) is not None:
                        failures.append(f"dev_password: {v['address']}.{attr} is set in state")
        print(f"{run}: resource types {sorted(types)}")

    # Belt and braces: the dummy must not appear anywhere in the module runs' output stream either.
    for m in messages:
        if m.get("@testrun") != CONTROL_RUN and dummy in json.dumps(m):
            failures.append(f"output stream: dummy found in a {m.get('type')} message")

    if not args.keep:
        for f in out_dir.iterdir():
            f.unlink()
        out_dir.rmdir()

    if failures:
        for f in failures:
            print("FAIL:", f, file=sys.stderr)
        return 1
    print("PASS: the reviewer password is absent from state; no service account keys exist")
    return 0


if __name__ == "__main__":
    sys.exit(main())
