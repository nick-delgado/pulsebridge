---
name: review-work
description: Independent review of a finished PulseBridge task in its git worktree - checks the changes and logic flows, validates against the task's acceptance criteria, and proves the tests are not superficial. Runs in a separate reviewer subagent. Use after a builder agent reports a task complete.
argument-hint: "[task-id] [base-branch]"
context: fork
agent: work-reviewer
model: claude-opus-5-5
effort: medium
---

# Review a finished task

Arguments: `$ARGUMENTS` (task ID, then the base branch the task branched from; if the base branch
is missing, use the branch checked out in the main checkout).

## 1. Locate the work

- Find the worktree: `git worktree list`, entry under `.worktrees/` starting with the task ID.
- Read the task in `docs/workplan.md` (description, AC, `Owns`), `CLAUDE.md`, and the parts of
  `pulsebridge-projectbrief.md` it touches.
- Get the change set: `git -C <worktree> diff <base-branch>...HEAD` and the commit list.
- If the worktree is missing or has uncommitted changes, report that and stop.
- Keep your own scratch files in a subdirectory of the scratchpad named `review-<task-id>`.
  Other agents share the scratchpad: never run a script you did not write.

## 2. Review the changes and logic

Read every changed file in full, not only the diff hunks.

- Trace the main flow and each failure path. Look for: unhandled errors, wrong branching,
  off-by-one and time-zone mistakes, non-idempotent writes, resource leaks, race conditions.
- Standards correctness. FHIR R4: required elements, LOINC and UCUM codes, references,
  conditional create or update keys, Bundle structure. HL7v2: segment and field positions,
  separators and escapes, ACK codes, MLLP framing. Terraform: pinned versions, variable
  descriptions and validation, least-privilege IAM, nothing secret in state, clean destroy.
  Check against the specification, not against the builder's comments.
- Project rules: no secrets or real health data, nothing author-specific hardcoded, changes stay
  inside the task's `Owns` paths, idle cost near zero.

## 3. Validate against the acceptance criteria

For each AC decide **met**, **partly met** or **not met**, with evidence (file and line, or a
command you ran and its output). An AC is met only if the behaviour exists and you verified it;
a test with a matching name is not evidence on its own. Note any behaviour built that no AC asked for.

## 4. Check the tests

1. Run the test suite and linters in the worktree. Report the real result.
   Run every `terraform` command with Google credentials removed from the environment (unset
   `GOOGLE_APPLICATION_CREDENTIALS`, `GCP_SA_KEY_B64`, `CLOUDSDK_CORE_PROJECT`, `GOOGLE_CLOUD_PROJECT`
   and `GOOGLE_PROJECT`). A mocked provider is not a guarantee: `mock_provider` has failed to apply
   to child modules, and the real provider then ran with the session's credentials.
2. Read each test. Flag tests that cannot fail or prove nothing: assertions on mocks the test
   configured itself, tests with no meaningful assertion, assertions that copy the implementation,
   over-mocking that bypasses the code under test, snapshots of unreviewed output.
3. **Break-check, independently of the builder's.** Choose the behaviours that matter most
   (at least one per AC, and every branch that handles an error). For each: break the
   implementation, run the tests, record whether a test failed, then restore the file.
   A break that no test catches is a finding.
4. List behaviours and failure paths with no test at all.

Before finishing, confirm the worktree is exactly as you found it: `git -C <worktree> status`
is clean and `HEAD` has not moved.

## 5. Report

Do not fix anything. Report:

- **Verdict:** `approve`, `approve with minor fixes`, or `changes required`.
- **Findings**, most severe first. Each has: severity (blocker / major / minor), file and line,
  what is wrong, a concrete failing scenario, and a suggested fix.
- **AC table:** AC, status, evidence.
- **Test assessment:** suite result, superficial tests, break-check table (what was broken,
  caught or not, by which test), untested behaviour.
- **Not verified:** anything you could not check and why.
