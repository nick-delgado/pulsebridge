#!/usr/bin/env bash
# Static check `terraform test` cannot make: which store IAM resources this module declares.
#
# The .tftest.hcl files inspect the three role-table-driven resources (whole tables, store IDs
# included). A binding declared as a separate resource would not appear in those tables, so
# without this check it would pass every run. This script fails if any .tf file here:
#   1. declares a Healthcare IAM resource (on a dataset or any store, _iam_member, _iam_binding
#      or _iam_policy) other than the three allowed ones below;
#   2. declares one of the allowed resources with a different for_each or a different store;
#   3. declares any project-level IAM resource (a project or dataset role would reach every
#      store, the health-data store included);
#   4. calls a child module (where a binding could be declared out of sight of this check).
# It reads the source only: no credentials, no terraform init.
#
# Usage: tests/check_store_iam.sh [module-dir]   (default: the directory above this script)
set -euo pipefail

module_dir="${1:-$(cd "$(dirname "$0")/.." && pwd)}"

MODULE_DIR="$module_dir" python3 - <<'EOF'
import glob, os, re, sys

# type name -> (for_each expression, store-id argument and value). D15: cuff-sim only on the
# audit store; the role table decides who gets what on each.
ALLOWED = {
    ("google_healthcare_fhir_store_iam_member", "app"):
        ("local.fhir_store_bindings", "fhir_store_id", "google_healthcare_fhir_store.this.id"),
    ("google_healthcare_fhir_store_iam_member", "audit"):
        ("local.fhir_audit_store_bindings", "fhir_store_id", "google_healthcare_fhir_store.audit.id"),
    ("google_healthcare_hl7_v2_store_iam_member", "app"):
        ("local.hl7v2_store_bindings", "hl7_v2_store_id", "google_healthcare_hl7_v2_store.this.id"),
}
HEALTHCARE_IAM = re.compile(r"^google_healthcare_\w+_iam_(member|binding|policy)$")
PROJECT_IAM = re.compile(r"^google_(project|organization|folder)_iam_\w+$")


def strip_comments(text):
    # Good enough for this module's style: no '#' or '//' inside the strings it matches.
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(re.split(r"\s(?:#|//)|^(?:#|//)", line, maxsplit=1)[0] for line in text.splitlines())


def blocks(text):
    """Yield (kind, labels, body) for each top-level block, matching braces."""
    for m in re.finditer(r'^(resource|module|data)\s+((?:"[^"]*"\s*)+)\{', text, flags=re.M):
        depth, i = 1, m.end()
        while depth and i < len(text):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        yield m.group(1), re.findall(r'"([^"]*)"', m.group(2)), text[m.end():i - 1]


failures, seen = [], set()
files = sorted(glob.glob(os.path.join(os.environ["MODULE_DIR"], "*.tf")))
if not files:
    sys.exit(f"FAIL no .tf files in {os.environ['MODULE_DIR']}")
for path in files:
    name = os.path.basename(path)
    for kind, labels, body in blocks(strip_comments(open(path).read())):
        if kind == "module":
            failures.append(f"{name}: module \"{labels[0]}\" (no child modules allowed in this module)")
            continue
        if kind != "resource":
            continue
        rtype, rname = labels[0], labels[1]
        if PROJECT_IAM.match(rtype):
            failures.append(f"{name}: {rtype}.{rname} is project-level IAM")
        elif HEALTHCARE_IAM.match(rtype):
            key = (rtype, rname)
            if key not in ALLOWED:
                failures.append(f"{name}: {rtype}.{rname} is not one of the role-table-driven store bindings")
                continue
            seen.add(key)
            for_each, arg, store = ALLOWED[key]
            if not re.search(rf"^\s*for_each\s*=\s*{re.escape(for_each)}\s*$", body, flags=re.M):
                failures.append(f"{name}: {rtype}.{rname} must use for_each = {for_each}")
            if not re.search(rf"^\s*{arg}\s*=\s*{re.escape(store)}\s*$", body, flags=re.M):
                failures.append(f"{name}: {rtype}.{rname} must set {arg} = {store}")

for key in ALLOWED:
    if key not in seen:
        failures.append(f"{key[0]}.{key[1]} is missing")

for f in failures:
    print(f"FAIL {f}", file=sys.stderr)
if failures:
    sys.exit(1)
print("ok   store IAM is only the three role-table-driven resources; no project IAM, no child modules")
EOF
