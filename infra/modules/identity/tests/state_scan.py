#!/usr/bin/env python3
"""Prove that the reviewer password never reaches Terraform state (task T04).

What it does, with no cloud credentials:

1. Checks statically that `reviewer_password` is declared `sensitive = true` and
   `ephemeral = true` in variables.tf. Ephemeral is what makes Terraform refuse to store the value
   anywhere except a write-only argument; without it a later edit could copy the value into any
   ordinary attribute.
2. Generates a fresh random dummy password (never a real one, never printed).
3. Runs `terraform test` on tests/state_scan/ with a mock Google provider and the dummy as
   `reviewer_password`, with every Google credential source removed from the environment (so
   even if the mock failed to apply, the real provider could not authenticate). Each run block
   applies the module; `-json -verbose` makes Terraform emit the resulting state as a
   `test_state` message, in the same JSON format as `terraform show -json`, which prints
   sensitive values in clear text.
4. Writes each run's state to a file and searches it for the dummy: verbatim, hex-encoded, and
   base64-encoded (standard and URL-safe, matched at any byte alignment, so a copy embedded in a
   longer encoded string is still found):
   - `defaults` and `dev_password` (the real module) must contain none of these forms;
   - `control` (a helper that stores the dummy verbatim through `secret_data` and base64-encoded
     in an annotation) must show both, which proves the search would find such a leak;
   - the real module may contain only the expected resource types, and in particular no
     `google_service_account_key`;
   - with the defaults, no secret version exists at all.

What it cannot prove: that no other transformation of the value (a hash, encryption, a substring)
reaches state, or what a real provider does; it checks the module's state under a mock provider.

`terraform test` keeps state in memory and offers no stable way to write it to disk (a `backend`
block in a run is experimental-only in Terraform 1.17), which is why the state is taken from the
JSON stream.

Usage: python3 tests/state_scan.py [--keep DIR]   (run from anywhere; exit code 0 = pass)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import shutil
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
# Forms the control run stores on purpose; the scan must detect each of them there.
CONTROL_MUST_FIND = {"verbatim", "base64"}

# Environment variables through which Google tools find credentials or a project. All are removed
# before Terraform runs, and the metadata server is pointed at an unreachable address so
# Application Default Credentials cannot fall back to it.
CREDENTIAL_VARS = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CREDENTIALS",
    "GOOGLE_CLOUD_KEYFILE_JSON",
    "GCLOUD_KEYFILE_JSON",
    "GOOGLE_OAUTH_ACCESS_TOKEN",
    "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_PROJECT",
    "GCP_SA_KEY_B64",
    "CLOUDSDK_CORE_PROJECT",
    "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
    "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
)


def iter_resources(module: dict):
    """Yield every resource in a `terraform show -json` style module, including child modules."""
    yield from module.get("resources", [])
    for child in module.get("child_modules", []):
        yield from iter_resources(child)


def variable_block(source: str, name: str) -> str | None:
    """Return the body of `variable "<name>" { ... }`, matching braces, or None."""
    match = re.search(r'^variable\s+"' + re.escape(name) + r'"\s*\{', source, re.MULTILINE)
    if not match:
        return None
    depth, i = 1, match.end()
    while i < len(source) and depth:
        depth += {"{": 1, "}": -1}.get(source[i], 0)
        i += 1
    return source[match.end() : i - 1]


def check_declaration() -> list[str]:
    """reviewer_password must be declared sensitive and ephemeral (top-level arguments)."""
    source = (MODULE_DIR / "variables.tf").read_text()
    body = variable_block(source, "reviewer_password")
    if body is None:
        return ['variables.tf: no variable "reviewer_password" block']
    # Drop comments and nested blocks (validation { ... }) so only top-level arguments count.
    body = re.sub(r"(#|//).*", "", body)
    while re.search(r"\{[^{}]*\}", body):
        body = re.sub(r"\{[^{}]*\}", "", body)
    failures = []
    for arg in ("sensitive", "ephemeral"):
        if not re.search(rf"^\s*{arg}\s*=\s*true\s*$", body, re.MULTILINE):
            failures.append(f"variables.tf: reviewer_password is not declared `{arg} = true`")
    return failures


def encoded_forms(value: str) -> dict[str, list[str]]:
    """Strings whose presence means `value` is in the text, per encoding.

    Base64 maps 3 bytes to 4 characters, so the encoding of a value embedded in a longer string
    depends on where it starts. For each of the 3 possible offsets, encode the value behind
    k filler bytes and keep only the characters that depend on the value alone (drop the first
    group, which mixes in the filler, and the last group, which may mix in what follows).
    """
    raw = value.encode()
    forms: dict[str, list[str]] = {
        "verbatim": [value],
        "hex": [raw.hex(), raw.hex().upper()],
        "base64": [],
    }
    for k in range(3):
        enc = base64.b64encode(b"\0" * k + raw).decode()
        start = 4 * ((k + 2) // 3)
        fragment = enc[start:-4]
        forms["base64"] += [fragment, fragment.replace("+", "-").replace("/", "_")]
    return forms


def found_forms(text: str, forms: dict[str, list[str]]) -> set[str]:
    return {name for name, needles in forms.items() if any(n and n in text for n in needles)}


def safe_env(dummy: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in CREDENTIAL_VARS}
    home = tempfile.mkdtemp(prefix="identity-state-scan-home-")
    env.update(
        TF_VAR_reviewer_password=dummy,
        TF_IN_AUTOMATION="1",
        HOME=home,
        CLOUDSDK_CONFIG=os.path.join(home, "gcloud"),
        GCE_METADATA_HOST="127.0.0.1:9",
        GCE_METADATA_IP="127.0.0.1:9",
        NO_GCE_CHECK="True",
    )
    return env


def run_terraform(dummy: str) -> list[dict]:
    env = safe_env(dummy)
    try:
        return _run_terraform(env)
    finally:
        shutil.rmtree(env["HOME"], ignore_errors=True)


def _run_terraform(env: dict[str, str]) -> list[dict]:
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

    failures: list[str] = check_declaration()
    if not failures:
        print("variables.tf: reviewer_password is declared sensitive and ephemeral")

    # Synthetic, random each time, at least 16 characters (the variable's minimum).
    dummy = "state-scan-dummy-" + secrets.token_hex(16)
    forms = encoded_forms(dummy)
    messages = run_terraform(dummy)

    states = {m["@testrun"]: m["test_state"] for m in messages if m.get("type") == "test_state"}
    missing = {*MODULE_RUNS, CONTROL_RUN} - states.keys()
    if missing:
        raise SystemExit(f"FAIL: no state captured for run(s): {sorted(missing)}")

    out_dir = args.keep or Path(tempfile.mkdtemp(prefix="identity-state-scan-"))
    out_dir.mkdir(parents=True, exist_ok=True)

    for run, state in states.items():
        path = out_dir / f"{run}.tfstate.json"
        path.write_text(json.dumps(state, indent=2))
        found = found_forms(path.read_text(), forms)
        if run == CONTROL_RUN:
            if found >= CONTROL_MUST_FIND:
                print(f"{run}: dummy found as {sorted(found)}, as expected (positive control)")
            else:
                failures.append(
                    f"control: dummy found only as {sorted(found)}; the search cannot detect "
                    f"{sorted(CONTROL_MUST_FIND - found)}"
                )
            continue

        if found:
            failures.append(f"{run}: the dummy password IS in state as {sorted(found)} ({path})")
        else:
            print(f"{run}: dummy password not found in state, in any form searched [{path}]")

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

    # Belt and braces: no form of the dummy anywhere in the module runs' output stream either.
    for m in messages:
        if m.get("@testrun") != CONTROL_RUN and found_forms(json.dumps(m), forms):
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
