#!/usr/bin/env bash
# Validate PulseBridge's example FHIR resources with the official HL7 FHIR Validator.
#
# The HL7 FHIR Validator (validator_cli.jar, from github.com/hapifhir/org.hl7.fhir.core releases)
# is the reference implementation of FHIR validation. This script runs it over every JSON file in
# an examples directory (default: libs/pulsebridge_fhir/examples/) as FHIR R4 (4.0.1), against
# the base resource definitions plus any profile a resource declares in meta.profile (the
# examples declare the R4 core vital-signs profiles `heartrate` and `bp`).
#
#   scripts/fhir-validate.sh [options] [EXAMPLES_DIR]
#
# Run `scripts/fhir-validate.sh --help` for the options and exit codes. This header is the
# script's documentation: what it decides, and why.
#
# Decisions (each explained in the task summary for T08):
#
# * Terminology. By default codes are checked by the public HL7 terminology server
#   (https://tx.fhir.org): LOINC codes and displays, UCUM units, DICOM and HL7 code systems, and
#   whether a code is in the value set an element is bound to. That is the interesting half of
#   validation for this project, so it is the default, in CI too. `--offline` runs `-tx n/a`:
#   structure, cardinality and invariants only, no network for codes, and the output says loudly
#   that codes were NOT checked.
#   PRIVACY: the validator sends every code (and the text around it) to the terminology server.
#   That is acceptable for these SYNTHETIC examples. Never point this script at real patient data
#   while the public terminology server is enabled.
# * Terminology server unreachable. The run must not pass silently without code checks: the
#   server is probed first (a bounded retry), the validator's own connection failure is detected,
#   and any issue saying a code could not be checked for lack of a server fails the run (exit 3).
# * Severity. ERROR and FATAL always fail. WARNINGs are listed and counted; a warning that matches
#   an entry in the expected-warnings file (each entry carries its reason) does not fail. A NEW
#   warning (one not on that list) fails the run, unless --allow-new-warnings is given: otherwise
#   a new warning would scroll past in a green CI log and never be read. INFORMATION issues are
#   listed and never fail.
# * The validator is pinned: VALIDATOR_VERSION and its SHA-256. The jar is downloaded once into a
#   cache directory and verified against the SHA-256 on every run, so a corrupted or swapped jar
#   in the cache is refused, not used. To update the pin, see `--help`.
#
# Exit codes: 0 pass; 1 validation failed (an ERROR/FATAL issue, or a new warning);
# 2 usage or setup error (bad argument, no files, checksum mismatch, Java missing, the validator
# crashed or did not report on every file); 3 terminology server unreachable, codes not checked.
#
# The functions below can be sourced by the tooling tests (tests/tooling/test_fhir_validate.py):
# `main` only runs when the file is executed, not when it is sourced.

set -euo pipefail

# ---- Pinned validator ----------------------------------------------------------------------------
# To update: pick a release at https://github.com/hapifhir/org.hl7.fhir.core/releases (prefer one
# that is at least two weeks old), download its validator_cli.jar, compute `sha256sum` of it,
# put both values here, run `make fhir-validate`, and review any change in the issues reported.
# The GitHub release page shows each asset's SHA-256 digest; compare it with yours.
readonly VALIDATOR_VERSION="7.0.4"
readonly VALIDATOR_SHA256="43e29fa8822d670740a909c395ca5b70a3d07fbaa8936f8dedc6a98b007f555b"
VALIDATOR_URL="https://github.com/hapifhir/org.hl7.fhir.core/releases/download/${VALIDATOR_VERSION}/validator_cli.jar"

readonly FHIR_VERSION="4.0.1"
readonly DEFAULT_TX="https://tx.fhir.org"
readonly MIN_JAVA_MAJOR=17 # the jar's manifest says Java-Version: 17

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DEFAULT_EXAMPLES_DIR="$REPO_ROOT/libs/pulsebridge_fhir/examples"
DEFAULT_EXPECTED_FILE="$SCRIPT_DIR/fhir-validate-expected-warnings.json"

# Terminology probe: attempts and the delay before the first retry (doubled each time).
TX_ATTEMPTS="${FHIR_TX_ATTEMPTS:-3}"
TX_RETRY_SECONDS="${FHIR_TX_RETRY_SECONDS:-10}"

usage() {
  cat <<EOF
Usage: scripts/fhir-validate.sh [options] [EXAMPLES_DIR]

Validates every *.json file under EXAMPLES_DIR (default: libs/pulsebridge_fhir/examples) with the
HL7 FHIR Validator ${VALIDATOR_VERSION}, as FHIR R4 (${FHIR_VERSION}), base profiles plus the
profiles each resource declares in meta.profile. Displays of coded values are checked too.

Options:
  --offline              No terminology server (-tx n/a): structure only; codes, units and value
                         set membership are NOT checked. Use when there is no network.
  --tx URL               Terminology server, laid out like tx.fhir.org (R4 under URL/r4).
                         Default: ${DEFAULT_TX}. It receives every code in the files: never use
                         a public server with real patient data.
  --expected FILE        Expected-warnings file (default: scripts/fhir-validate-expected-warnings.json).
  --allow-new-warnings   List warnings that are not in the expected file, but do not fail on them.
  --cache-dir DIR        Where the jar and the FHIR package cache live (default: \$FHIR_VALIDATOR_CACHE,
                         else \${XDG_CACHE_HOME:-\$HOME/.cache}/pulsebridge/fhir-validator).
  --save-report FILE     Also write the validator's raw report (an OperationOutcome Bundle) to FILE.
  --report-only FILE     Do not run the validator: evaluate a report saved earlier with --save-report
                         against EXAMPLES_DIR. Needs neither Java nor the network.
  --validator-version    Print the pinned validator version and SHA-256, then exit.
  -h, --help             Show this help.

Exit codes:
  0  every file passed (no ERROR or FATAL, no new warning)
  1  validation failed: an ERROR or FATAL issue, or a warning not in the expected file
  2  usage or setup error: bad argument, no files, checksum mismatch, Java missing, validator crash
  3  terminology server unreachable or codes left unchecked (never a silent pass)

Updating the validator pin: edit VALIDATOR_VERSION and VALIDATOR_SHA256 at the top of this
script (the comment there says how), then run it and review the issues it reports.
EOF
}

log() { printf '%s\n' "$*" >&2; }
die() {
  local code="$1"
  shift
  log "fhir-validate: $*"
  exit "$code"
}

# sha256_of FILE: print the file's SHA-256 (GNU coreutils or macOS shasum).
sha256_of() {
  local file="$1" out
  if command -v sha256sum > /dev/null 2>&1; then
    out="$(sha256sum "$file")"
  elif command -v shasum > /dev/null 2>&1; then
    out="$(shasum -a 256 "$file")"
  else
    log "fhir-validate: neither sha256sum nor shasum is installed"
    return 2
  fi
  printf '%s\n' "${out%% *}"
}

# verify_sha256 FILE EXPECTED: succeed only if FILE's SHA-256 is EXPECTED.
verify_sha256() {
  local file="$1" expected="$2" actual
  actual="$(sha256_of "$file")" || return 2
  if [ "$actual" != "$expected" ]; then
    log "fhir-validate: checksum mismatch for $file"
    log "  expected SHA-256 $expected"
    log "  actual   SHA-256 $actual"
    return 1
  fi
}

# ensure_validator CACHE_DIR: make sure the pinned jar is in the cache and matches its SHA-256;
# print its path. A cached jar is re-verified on every run; a mismatch is refused, not replaced,
# so a tampered cache is noticed (delete the file to force a fresh download).
ensure_validator() {
  local cache="$1" jar tmp
  jar="$cache/validator_cli-${VALIDATOR_VERSION}.jar"
  mkdir -p "$cache"
  if [ ! -f "$jar" ]; then
    log "Downloading FHIR Validator ${VALIDATOR_VERSION} to $jar"
    tmp="$jar.part.$$"
    if ! curl -fsSL --retry 3 --retry-delay 5 -o "$tmp" "$VALIDATOR_URL"; then
      rm -f "$tmp"
      log "fhir-validate: could not download $VALIDATOR_URL"
      return 2
    fi
    if ! verify_sha256 "$tmp" "$VALIDATOR_SHA256"; then
      rm -f "$tmp"
      log "fhir-validate: the downloaded jar does not match the pinned SHA-256; refusing to use it"
      return 2
    fi
    mv "$tmp" "$jar"
  elif ! verify_sha256 "$jar" "$VALIDATOR_SHA256"; then
    log "fhir-validate: the cached jar does not match the pinned SHA-256; refusing to use it."
    log "  Delete $jar to download it again."
    return 2
  fi
  printf '%s\n' "$jar"
}

# check_java: the validator needs Java ${MIN_JAVA_MAJOR} or newer.
check_java() {
  local out version major
  command -v java > /dev/null 2>&1 || {
    log "fhir-validate: java is not installed (Java ${MIN_JAVA_MAJOR}+ is needed)"
    return 2
  }
  out="$(java -version 2>&1)" || {
    log "fhir-validate: 'java -version' failed"
    return 2
  }
  # The line looks like: openjdk version "21.0.12" 2026-08-18 (old JDKs: "1.8.0_402").
  version="$(printf '%s\n' "$out" | sed -n 's/.* version "\([^"]*\)".*/\1/p' | head -n 1)"
  major="${version%%.*}"
  if [ "$major" = "1" ]; then
    major="$(printf '%s' "$version" | cut -d. -f2)"
  fi
  case "$major" in
    '' | *[!0-9]*)
      log "fhir-validate: could not read the Java version from 'java -version'"
      return 2
      ;;
  esac
  if [ "$major" -lt "$MIN_JAVA_MAJOR" ]; then
    log "fhir-validate: Java $version is too old; the validator needs Java ${MIN_JAVA_MAJOR}+"
    return 2
  fi
}

# probe_tx URL: is the terminology server answering? Bounded retry with a doubling delay.
probe_tx() {
  local tx="${1%/}" attempt=1 delay="$TX_RETRY_SECONDS"
  while :; do
    if curl -fsS -o /dev/null --max-time 30 -H 'Accept: application/fhir+json' "$tx/r4/metadata"; then
      return 0
    fi
    if [ "$attempt" -ge "$TX_ATTEMPTS" ]; then
      log "fhir-validate: terminology server $tx is unreachable after $attempt attempts."
      log "  Codes cannot be checked, so the run fails rather than passing without them."
      log "  Try again later, or run with --offline for a structure-only check (and say so)."
      return 3
    fi
    log "Terminology server $tx did not answer (attempt $attempt of $TX_ATTEMPTS); retrying in ${delay}s"
    sleep "$delay"
    attempt=$((attempt + 1))
    delay=$((delay * 2))
  done
}

# tx_failure_in_log LOG: did the validator stop because it could not reach the terminology server?
# These are the validator's own messages when the capability statement cannot be fetched.
tx_failure_in_log() {
  grep -q -E "Unable to connect to terminology server|Error fetching the server's capability statement|TerminologyServiceException" "$1"
}

# list_json_files DIR: every *.json file under DIR, sorted, one per line.
list_json_files() {
  local dir="$1" found
  found="$(find "$dir" -type f -name '*.json')"
  [ -n "$found" ] || return 0
  printf '%s\n' "$found" | LC_ALL=C sort
}

# evaluate_report REPORT EXPECTED MODE TX ALLOW_NEW ROOT FILE...: read the validator's report,
# apply the expected-warnings list and print the result; its exit status is the script's.
# MODE is "terminology" or "offline". Python's standard library only, run isolated (-I).
evaluate_report() {
  python3 -I - "$@" <<'PY'
"""Evaluate an HL7 FHIR Validator report (an OperationOutcome per file, in a Bundle)."""

import json
import os
import re
import sys

# Issues that mean "a code was NOT checked because no terminology server answered". With a
# server they mean the server failed during the run: exit 3, never a silent pass. Offline they
# are expected and only counted. Message ids are the validator's (Messages.properties); the text
# patterns catch the few such issues that carry no id.
TX_NOT_CHECKED_IDS = {
    "Attempt_to_use_Terminology_server_when_no_Terminology_server_is_available",
    "Error_validating_code_running_without_terminology_services",
    "TERMINOLOGY_TX_NOSVC_BOUND_EXT",
    "TERMINOLOGY_TX_NOSVC_BOUND_REQ",
    "Terminology_TX_Binding_NoServer",
    "Terminology_TX_Error_CodeableConcept",
    "Terminology_TX_Error_CodeableConcept_Max",
    "Terminology_TX_Error_Coding1",
    "Terminology_TX_Error_Coding2",
    "Terminology_TX_NoValid_15",
    "Terminology_TX_NoValid_9",
    "TX_SERVER_BATCH_RESPONSE_MISMATCH",
    "TX_SERVER_BATCH_RESPONSE_SIZE",
    "Unable_to_connect_to_terminology_server",
    "Unable_to_validate_code_without_using_server",
    "VALUESET_INC_NO_SERVER",
}
TX_NOT_CHECKED_TEXT = re.compile(
    r"without (using )?(the )?terminology serv|absence of a terminology server"
    r"|Unable to validate code without using server|Unable to connect to terminology server"
    r"|no Terminology server is available|because there is no terminology service",
    re.IGNORECASE,
)

FILE_EXT = "http://hl7.org/fhir/StructureDefinition/operationoutcome-file"
ID_EXT = "http://hl7.org/fhir/StructureDefinition/operationoutcome-message-id"
LINE_EXT = "http://hl7.org/fhir/StructureDefinition/operationoutcome-issue-line"
COL_EXT = "http://hl7.org/fhir/StructureDefinition/operationoutcome-issue-col"

ENTRY_KEYS = {"id", "message_id", "text_contains", "location_contains", "when", "status", "reason"}
WHEN_VALUES = {"always", "terminology"}  # "terminology": expected only when a server is used


class SetupError(Exception):
    """Exit 2: the inputs to the evaluation are wrong, not the resources."""


def ext_value(element, url):
    for ext in element.get("extension", []):
        if ext.get("url") == url:
            for key, value in ext.items():
                if key.startswith("value"):
                    return value
    return None


def load_expected(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise SetupError(f"cannot read the expected-warnings file {path}: {exc}") from exc
    entries = data.get("expected_warnings") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise SetupError(f"{path}: needs a top-level 'expected_warnings' list")
    seen = set()
    for n, entry in enumerate(entries):
        where = f"{path}: entry {n}"
        if not isinstance(entry, dict):
            raise SetupError(f"{where} is not an object")
        unknown = set(entry) - ENTRY_KEYS
        if unknown:
            # Includes "severity": only warnings can be expected. An ERROR is never allow-listed.
            raise SetupError(f"{where} has unknown keys {sorted(unknown)} (allowed: {sorted(ENTRY_KEYS)})")
        if not isinstance(entry.get("id"), str) or not entry["id"]:
            raise SetupError(f"{where} needs an 'id'")
        if entry["id"] in seen:
            raise SetupError(f"{where}: duplicate id {entry['id']!r}")
        seen.add(entry["id"])
        if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
            raise SetupError(f"{where} ({entry['id']}) needs a 'reason': why the warning is expected")
        contains = entry.get("text_contains", [])
        if not isinstance(contains, list) or not all(isinstance(s, str) and s for s in contains):
            raise SetupError(f"{where} ({entry['id']}): 'text_contains' must be a list of strings")
        if not entry.get("message_id") and not contains:
            raise SetupError(f"{where} ({entry['id']}) needs 'message_id' or 'text_contains'")
        if entry.get("when", "always") not in WHEN_VALUES:
            raise SetupError(f"{where} ({entry['id']}): 'when' must be one of {sorted(WHEN_VALUES)}")
    return entries


def matches(entry, issue):
    if entry.get("message_id") and entry["message_id"] != issue["message_id"]:
        return False
    if not all(s in issue["text"] for s in entry.get("text_contains", [])):
        return False
    loc = entry.get("location_contains")
    return not loc or loc in issue["location"]


def outcomes_in(report):
    if report.get("resourceType") == "OperationOutcome":
        return [report]
    if report.get("resourceType") == "Bundle":
        return [e.get("resource", {}) for e in report.get("entry", [])]
    raise SetupError("the report is neither an OperationOutcome nor a Bundle of them")


def gh_escape(text):
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(argv):
    report_path, expected_path, mode, tx, allow_new, root, *files = argv
    allow_new = allow_new == "1"
    on_github = os.environ.get("GITHUB_ACTIONS") == "true"
    try:
        expected = load_expected(expected_path)
        try:
            with open(report_path, encoding="utf-8") as fh:
                report = json.load(fh)
        except (OSError, ValueError) as exc:
            raise SetupError(f"cannot read the validator report {report_path}: {exc}") from exc
        by_file = {}
        for oo in outcomes_in(report):
            name = ext_value(oo, FILE_EXT)
            if not name or oo.get("resourceType") != "OperationOutcome":
                raise SetupError("the report has an entry that is not an OperationOutcome for a file")
            by_file[os.path.realpath(name)] = oo
        wanted = [os.path.realpath(f) for f in files]
        missing = [f for f in wanted if f not in by_file]
        if missing:
            # A file the validator did not report on must not count as a pass.
            raise SetupError("the validator did not report on: " + ", ".join(missing))
    except SetupError as exc:
        print(f"fhir-validate: {exc}", file=sys.stderr)
        return 2

    applicable = [e for e in expected if e.get("when", "always") == "always" or mode == "terminology"]
    used = {e["id"]: 0 for e in applicable}
    totals = {"error": 0, "warning": 0, "expected": 0, "new": 0, "information": 0, "tx": 0}

    if mode == "terminology":
        print(f"FHIR R4 ({FHIR}) validation, terminology checked by {tx}")
    else:
        print(f"FHIR R4 ({FHIR}) validation, OFFLINE: codes, units and value sets NOT checked")
    for path in wanted:
        oo = by_file[path]
        shown = os.path.relpath(path, root) if path.startswith(root + os.sep) else path
        counts = {"error": 0, "warning": 0, "information": 0}
        lines = []
        for raw in oo.get("issue", []):
            sev = raw.get("severity", "error")
            issue = {
                "message_id": ext_value(raw, ID_EXT) or "",
                "text": (raw.get("details") or {}).get("text") or raw.get("diagnostics") or "",
                "location": ", ".join(raw.get("expression") or raw.get("location") or []),
            }
            line, col = ext_value(raw, LINE_EXT), ext_value(raw, COL_EXT)
            pos = f"{shown}:{line}:{col}" if line else shown
            not_checked = issue["message_id"] in TX_NOT_CHECKED_IDS or bool(
                TX_NOT_CHECKED_TEXT.search(issue["text"])
            )
            if sev in ("fatal", "error"):
                counts["error"] += 1
                totals["error"] += 1
                tag = sev.upper()
                annotate = "error"
            elif not_checked:
                totals["tx"] += 1
                counts["warning" if sev == "warning" else "information"] += 1
                tag = "NOT CHECKED (offline)" if mode == "offline" else "NOT CHECKED: terminology failed"
                annotate = "error" if mode == "terminology" else None
            elif sev == "warning":
                counts["warning"] += 1
                totals["warning"] += 1
                hit = next((e for e in applicable if matches(e, issue)), None)
                if hit:
                    used[hit["id"]] += 1
                    totals["expected"] += 1
                    tag = f"warning, expected ({hit['id']})"
                    annotate = None
                else:
                    totals["new"] += 1
                    tag = "WARNING, NEW (not in the expected list)"
                    annotate = "warning" if allow_new else "error"
            else:
                counts["information"] += 1
                totals["information"] += 1
                tag = "information"
                annotate = None
            mid = f" [{issue['message_id']}]" if issue["message_id"] else ""
            lines.append(f"  {tag}: {pos} {issue['location']}{mid}\n      {issue['text']}")
            if on_github and annotate:
                gh_line = f",line={line}" if line else ""
                print(f"::{annotate} file={shown}{gh_line}::{gh_escape(tag + ': ' + issue['text'])}")
        status = "FAIL" if counts["error"] else "ok"
        print(
            f"{status:4} {shown}: {counts['error']} error(s), {counts['warning']} warning(s), "
            f"{counts['information']} information"
        )
        for text in lines:
            print(text)

    print()
    print(
        f"Summary: {len(wanted)} file(s); {totals['error']} error/fatal; {totals['warning']} warning(s) "
        f"({totals['expected']} expected, {totals['new']} new); {totals['information']} information"
    )
    for entry in applicable:
        if used[entry["id"]]:
            status = entry.get("status", "")
            print(f"  expected {entry['id']}: {used[entry['id']]} time(s){' [' + status + ']' if status else ''}")
    for entry in applicable:
        if not used[entry["id"]]:
            print(f"  note: expected warning {entry['id']!r} did not occur; if its cause is fixed, remove it")

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as fh:
            fh.write(
                f"### FHIR validation (R4 {FHIR})\n\n"
                f"Terminology: {tx if mode == 'terminology' else 'NOT checked (offline)'}\n\n"
                f"| Files | Errors | Warnings (expected / new) | Information | Not checked |\n"
                f"| --- | --- | --- | --- | --- |\n"
                f"| {len(wanted)} | {totals['error']} | {totals['warning']} "
                f"({totals['expected']} / {totals['new']}) | {totals['information']} | {totals['tx']} |\n"
            )

    if totals["error"]:
        print(f"Result: FAIL, {totals['error']} ERROR/FATAL issue(s)")
        return 1
    if mode == "terminology" and totals["tx"]:
        print(
            f"Result: FAIL, {totals['tx']} code(s) could not be checked: the terminology server "
            "failed during the run. Not a pass; try again later."
        )
        return 3
    if mode == "offline":
        print(f"Offline: {totals['tx']} code check(s) skipped. This run did NOT check terminology.")
    if totals["new"] and not allow_new:
        print(
            f"Result: FAIL, {totals['new']} new warning(s). Fix the resource, or, if the warning is "
            "expected, add it with its reason to the expected-warnings file."
        )
        return 1
    result = "Result: PASS"
    if mode == "offline":
        result += " (structure only: terminology NOT checked)"
    if totals["new"]:
        result += f" ({totals['new']} new warning(s) allowed)"
    print(result)
    return 0


FHIR = "4.0.1"
sys.exit(main(sys.argv[1:]))
PY
}

main() {
  local dir="" offline=0 tx="$DEFAULT_TX" expected="$DEFAULT_EXPECTED_FILE" allow_new=0
  local cache="${FHIR_VALIDATOR_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/pulsebridge/fhir-validator}"
  local save_report="" report_only=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      -h | --help)
        usage
        return 0
        ;;
      --validator-version)
        printf '%s %s\n' "$VALIDATOR_VERSION" "$VALIDATOR_SHA256"
        return 0
        ;;
      --offline) offline=1 ;;
      --allow-new-warnings) allow_new=1 ;;
      --tx | --expected | --cache-dir | --save-report | --report-only)
        if [ "$#" -lt 2 ] || [ -z "$2" ]; then
          die 2 "$1 needs a value (see --help)"
        fi
        case "$1" in
          --tx) tx="$2" ;;
          --expected) expected="$2" ;;
          --cache-dir) cache="$2" ;;
          --save-report) save_report="$2" ;;
          --report-only) report_only="$2" ;;
        esac
        shift
        ;;
      --) shift && break ;;
      -*) die 2 "unknown option $1 (see --help)" ;;
      *)
        [ -z "$dir" ] || die 2 "only one examples directory may be given (got '$dir' and '$1')"
        dir="$1"
        ;;
    esac
    shift
  done
  if [ "$#" -gt 0 ]; then
    if [ -n "$dir" ] || [ "$#" -ne 1 ]; then
      die 2 "only one examples directory may be given"
    fi
    dir="$1"
  fi
  dir="${dir:-$DEFAULT_EXAMPLES_DIR}"
  [ -d "$dir" ] || die 2 "examples directory not found: $dir"
  [ -f "$expected" ] || die 2 "expected-warnings file not found: $expected"
  if [ "$offline" -eq 1 ] && [ "$tx" != "$DEFAULT_TX" ]; then
    die 2 "--offline and --tx contradict each other"
  fi
  case "$tx" in
    http://* | https://*) ;;
    *) die 2 "--tx must be an http(s) URL, got '$tx' (use --offline for no server)" ;;
  esac
  command -v python3 > /dev/null 2>&1 || die 2 "python3 is needed to read the validator's report"

  local file_list
  file_list="$(list_json_files "$dir")"
  [ -n "$file_list" ] || die 2 "no *.json files under $dir"
  local -a files=()
  local f
  while IFS= read -r f; do files+=("$f"); done <<< "$file_list"

  local mode="terminology" root
  [ "$offline" -eq 0 ] || mode="offline"
  root="$(cd "$REPO_ROOT" && pwd -P)"

  if [ -n "$report_only" ]; then
    [ -f "$report_only" ] || die 2 "report not found: $report_only"
    evaluate_report "$report_only" "$expected" "$mode" "$tx" "$allow_new" "$root" "${files[@]}"
    return
  fi

  command -v curl > /dev/null 2>&1 || die 2 "curl is needed to download the validator"
  check_java || exit 2
  if [ "$mode" = "terminology" ]; then
    probe_tx "$tx" || exit 3
  fi
  local jar
  jar="$(ensure_validator "$cache")" || exit 2

  local work rc=0
  work="$(mktemp -d "${TMPDIR:-/tmp}/fhir-validate.XXXXXX")"
  # shellcheck disable=SC2064 # expand $work now: it is local to this function
  trap "rm -rf '$work'" EXIT

  # -Duser.home points the validator's FHIR package cache (~/.fhir/packages) into our cache
  # directory, so CI can cache it next to the jar and a developer's own ~/.fhir is not touched.
  # A fresh -txCache per run: every run really asks the terminology server, so a change on the
  # server (a LOINC release, a fixed value set) is seen instead of a stale cached answer.
  # -check-display Check: a Coding's display must match the code system's (LOINC, UCUM, DICOM).
  # R4 says a display SHOULD match, not SHALL, so -display-issues-are-warnings reports a mismatch
  # as a warning; it is still a NEW warning, and new warnings fail the run.
  local -a args=(-version "$FHIR_VERSION" -output "$work/report.json"
    -check-display Check -display-issues-are-warnings)
  if [ "$mode" = "terminology" ]; then
    args+=(-tx "$tx" -txCache "$work/txcache")
  else
    args+=(-tx n/a)
  fi
  log "Running FHIR Validator ${VALIDATOR_VERSION} on ${#files[@]} file(s) (log: validator output follows on failure)"
  java -Duser.home="$cache/home" -jar "$jar" "${args[@]}" "${files[@]}" > "$work/validator.log" 2>&1 || rc=$?

  if [ ! -s "$work/report.json" ]; then
    tail -n 40 "$work/validator.log" >&2
    if [ "$mode" = "terminology" ] && tx_failure_in_log "$work/validator.log"; then
      die 3 "the validator could not reach the terminology server $tx; codes were not checked"
    fi
    die 2 "the validator exited with status $rc and wrote no report"
  fi
  if [ -n "$save_report" ]; then
    cp "$work/report.json" "$save_report"
  fi

  local verdict=0
  evaluate_report "$work/report.json" "$expected" "$mode" "$tx" "$allow_new" "$root" "${files[@]}" || verdict=$?
  if [ "$verdict" -eq 0 ] && [ "$rc" -ne 0 ]; then
    # The validator itself failed, yet its report shows nothing wrong: do not trust a pass.
    tail -n 40 "$work/validator.log" >&2
    die 2 "the validator exited with status $rc although its report has no errors"
  fi
  return "$verdict"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
