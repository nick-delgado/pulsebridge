---
name: feature-dev
description: Workflow for building one PulseBridge work-plan task in a git worktree - build, test (with break-checks proving tests can fail), self-review, then an explained summary published as an artifact. Use when assigned a task ID from docs/workplan.md.
argument-hint: "[task-id] [base-branch]"
---

# Feature development workflow

One task, one worktree, one branch. Work through the steps in order. Do not skip a step because
the task looks small.

## 0. Understand the task

1. Read the task in `docs/workplan.md`: description, acceptance criteria (AC), `Depends on`, `Owns`.
2. Read `CLAUDE.md` and the sections of `pulsebridge-projectbrief.md` the task touches.
3. Stop and report, without building, if: a dependency is not `done`; an AC is ambiguous in a way
   that changes the design; or the task needs changes outside its `Owns` paths.

## 1. Create the worktree

Use the repository root's absolute path (`git rev-parse --show-toplevel` from the main checkout).

```bash
git -C <repo> worktree add <repo>/.worktrees/<id>-<slug> -b task/<id>-<slug> <base-branch>
```

- `<id>` is the task ID in lower case (`t06`); `<slug>` is two or three words (`fhir-mapping`).
- Do all work inside that worktree, using absolute paths. Never edit the main checkout and never
  switch its branch.
- If the worktree or branch already exists, continue in it rather than recreating it.
- Do not edit `docs/workplan.md`; the orchestrator owns it.

## 2. Build

- Stay inside the task's `Owns` paths.
- Prefer readable, standards-correct code. Where the code implements part of FHIR, HL7v2 or a
  Terraform pattern, add a short comment naming the rule it follows (for example the LOINC code,
  the HL7v2 field such as `OBX-5`, or why a resource has a given lifecycle setting).
- Nothing specific to the author is hardcoded; secrets never appear in code, logs, fixtures or state.
- Commit in small logical commits on the task branch. Do not push unless your brief says to.

## 3. Test

Write tests that check behaviour the AC describes, then prove they can fail.

1. Every AC maps to at least one test (or, for Terraform, a `terraform validate` /
   `terraform test` check). If an AC cannot be tested automatically, say so and say how you
   verified it by hand.
2. Run the full test suite for the code you touched and the linters (`make lint test` once it exists).
3. **Break-check.** For each behaviour under test, deliberately break the implementation (not the
   test): flip a condition, change a code or unit, remove a field, return early. Run the tests.
   - At least one test must fail, and for the right reason.
   - If nothing fails, the test is superficial: strengthen or replace it, then repeat.
   - Restore the implementation after each break (`git checkout -- <file>` or `git stash`), and
     confirm the suite is green again.
4. Record each break-check: what you broke, which test failed. This table goes in the summary.

Do not write tests that cannot fail: no assertions on mocks you configured yourself, no tests that
only check a function runs, no assertions that restate the implementation, no snapshot of whatever
the code happens to produce.

For Terraform: run `terraform fmt -check` and `terraform validate`. Never run `terraform apply`,
enable an API or create cloud resources unless your brief explicitly says so.

## 4. Self-review

Read your full diff (`git diff <base-branch>...HEAD`) as if someone else wrote it.

- Walk each AC and point to the code and test that satisfy it.
- Trace the main logic flow and the failure paths. Look for unhandled errors, wrong units or codes,
  non-idempotent writes, and anything that would log or commit a secret.
- Check the diff for hardcoded project IDs, emails, domains, tokens and real health data.
- Fix what you find, re-run the tests, and commit.

An independent review (`review-work` skill) happens after you finish. Your self-review does not
replace it.

## 5. Summary and artifact

The user is learning FHIR, HL7v2 and Terraform through this project. The summary is how they
understand what was built, so explain, do not just list.

Write a summary with these sections:

1. **What was built**: the task, the branch, the files, in plain language.
2. **How it works**: the flow through the code, with short excerpts where they help.
3. **Standards and concepts explained**: for every FHIR, HL7v2 or Terraform element in this work,
   say what it is, why it is needed here, and what would go wrong without it. Define each term the
   first time it appears (for example "conditional update", "OBX segment", "remote state").
   Show a real example from the code: a FHIR resource, an HL7v2 message with its segments
   labelled, or a Terraform block.
4. **Decisions and why**: choices you made, alternatives you rejected.
5. **Acceptance criteria**: a table of AC, status (met / not met / partly), and evidence.
6. **Tests and break-checks**: what the tests cover, and the break-check table from step 3.
7. **Not done or uncertain**: anything skipped, failing, assumed or needing the user.

Then publish it as a Claude artifact so the user can read it:

- Load the `artifact-design` skill, write the page as HTML in the scratchpad or a temp directory
  outside the repository, and publish it with the Artifact tool.
- The summary and the HTML file are never committed.
- If the Artifact tool is not available to you, say so and return the full summary text in your
  final report so the orchestrator can publish it.

## 6. Final report

Reply with: task ID, branch, worktree path, commit list, AC table, test and break-check results
exactly as they ran (including failures), the artifact link, and open questions. Leave the
worktree in place for the reviewer.
