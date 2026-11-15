#!/usr/bin/env bash
# Static checks `terraform test` cannot make:
#  1. Ownership: this module must not grant roles/iam.serviceAccountTokenCreator or manage any
#     service account's IAM. Module identity owns the Pub/Sub agent's token-creator grant on
#     the push account; two modules managing one IAM member delete each other's grant.
#  2. Ordering: the depends_on edges below exist in `terraform graph`.
#
# Each line below is "A must be created after B". The check passes when B is reachable from A
# in `terraform graph` (A depends on B directly or through other resources). It needs no
# credentials: run `terraform init -backend=false` once in the module directory first.
#
# Usage: tests/check_ordering.sh [module-dir]   (default: the directory above this script)
set -euo pipefail

module_dir="${1:-$(cd "$(dirname "$0")/.." && pwd)}"

required_edges='
google_healthcare_hl7_v2_store.this google_pubsub_topic_iam_member.healthcare_agent_publisher
google_pubsub_subscription.ingest google_pubsub_topic_iam_member.pubsub_agent_dead_letter_publisher
'

# 1. Ownership (comments are not matched: a role argument or a resource block is).
if grep -nE '^[[:space:]]*resource[[:space:]]+"google_service_account_iam_(member|binding|policy)"|^[[:space:]]*role[[:space:]]*=[[:space:]]*"roles/iam\.serviceAccountTokenCreator"' \
  "$module_dir"/*.tf; then
  echo "FAIL this module must not manage service account IAM or grant token creator (module identity owns it)" >&2
  exit 1
fi
echo "ok   no service account IAM or token-creator grant in this module"

# 2. Ordering.
graph="$(terraform -chdir="$module_dir" graph)"

GRAPH="$graph" REQUIRED="$required_edges" python3 - <<'EOF'
import os, re, sys
from collections import defaultdict

edges = defaultdict(set)
for a, b in re.findall(r'"([^"]+)"\s*->\s*"([^"]+)"', os.environ["GRAPH"]):
    edges[a].add(b)

def reaches(src, dst):
    seen, todo = set(), [src]
    while todo:
        node = todo.pop()
        if node == dst:
            return True
        if node not in seen:
            seen.add(node)
            todo.extend(edges[node])
    return False

failed = 0
for line in os.environ["REQUIRED"].strip().splitlines():
    a, b = line.split()
    ok = reaches(a, b)
    print(f"{'ok  ' if ok else 'FAIL'} {a} depends on {b}")
    failed += not ok
if failed:
    print(f"{failed} ordering edge(s) missing", file=sys.stderr)
    sys.exit(1)
EOF
