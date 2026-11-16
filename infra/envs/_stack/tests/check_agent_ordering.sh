#!/usr/bin/env bash
# T38: the order of module healthcare's service agent grants in ONE full apply on a new project,
# read from `terraform graph` (which `terraform test` cannot see). Needs no credentials: run
# `terraform init -backend=false` once in infra/envs/_stack first (make tf-test does).
#
# Required ("A must be created after B": B is reachable from A in the graph):
#   - healthcare's two grants to the Pub/Sub agent come after module identity creates that agent
#     (google_project_service_identity.pubsub). Without it a single apply could reach them first
#     and fail with "Service account ... does not exist" (dev, 16 November).
#   - healthcare's grant to the Healthcare agent comes after healthcare creates that agent.
#   - the dataset, the topics and the Healthcare agent come after the APIs (project-base).
# Forbidden ("A must NOT depend on B"):
#   - healthcare's resources on project-base resources other than the APIs. That edge is what a
#     module-level `depends_on = [module.project_base]` adds, and it deferred healthcare's data
#     source and replaced the agent grants whenever project-base changed.
#
# Usage: tests/check_agent_ordering.sh [stack-dir]   (default: the directory above this script)
set -euo pipefail

stack_dir="${1:-$(cd "$(dirname "$0")/.." && pwd)}"

required='
module.healthcare.google_pubsub_topic_iam_member.pubsub_agent_dead_letter_publisher module.identity.google_project_service_identity.pubsub
module.healthcare.google_pubsub_subscription_iam_member.pubsub_agent_ingest_subscriber module.identity.google_project_service_identity.pubsub
module.healthcare.google_pubsub_topic_iam_member.healthcare_agent_publisher module.healthcare.google_project_service_identity.healthcare
module.healthcare.google_project_service_identity.healthcare module.project_base.google_project_service.this
module.healthcare.google_healthcare_dataset.this module.project_base.google_project_service.this
module.healthcare.google_pubsub_topic.ingest module.project_base.google_project_service.this
module.healthcare.google_pubsub_topic.dead_letter module.project_base.google_project_service.this
'

forbidden='
module.healthcare.google_pubsub_topic_iam_member.healthcare_agent_publisher module.project_base.google_compute_firewall.allow
module.healthcare.google_pubsub_topic_iam_member.pubsub_agent_dead_letter_publisher module.project_base.google_compute_network.this
module.healthcare.google_healthcare_dataset.this module.project_base.google_compute_firewall.allow
'

graph="$(terraform -chdir="$stack_dir" graph)"

GRAPH="$graph" REQUIRED="$required" FORBIDDEN="$forbidden" python3 - <<'EOF'
import os, re, sys
from collections import defaultdict

edges = defaultdict(set)
nodes = set()
for a, b in re.findall(r'"([^"]+)"\s*->\s*"([^"]+)"', os.environ["GRAPH"]):
    edges[a].add(b)
    nodes.update((a, b))

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
for kind, text, want in (("REQUIRED", os.environ["REQUIRED"], True), ("FORBIDDEN", os.environ["FORBIDDEN"], False)):
    for line in text.strip().splitlines():
        a, b = line.split()
        if a not in nodes or b not in nodes:
            # A renamed resource would otherwise make a forbidden edge pass vacuously.
            print(f"FAIL {a} or {b} is not in the graph")
            failed += 1
            continue
        ok = reaches(a, b) == want
        verb = "depends on" if want else "does not depend on"
        print(f"{'ok  ' if ok else 'FAIL'} {a} {verb} {b}")
        failed += not ok
if failed:
    print(f"{failed} ordering check(s) failed", file=sys.stderr)
    sys.exit(1)
EOF
