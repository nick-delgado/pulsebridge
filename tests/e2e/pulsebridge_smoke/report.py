"""Check results, the run's trail, and the summary.

Every check ends in one of four states, and the summary lists every one of them, so nothing
passes silently:

- ``PASS``: checked against the deployed stack and as expected;
- ``FAIL``: checked and wrong, or could not be checked although it should have been;
- ``SKIP``: deliberately not run, with the reason (for example health-sync's first-sync checks
  on a synthetic-data stack, where health-sync is not deployed);
- ``WARN``: checked, not as hoped, but not an acceptance criterion (shown, never fails the run).

The **trail** is what this run added to the stack (attempt ids, control IDs, Observation ids,
the OpenEMR Observation id) so a later reader can find exactly these records.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class Status(StrEnum):
    PASS = "PASS"  # noqa: S105 - a check state, not a password
    FAIL = "FAIL"
    SKIP = "SKIP"
    WARN = "WARN"


@dataclass(frozen=True)
class Result:
    check_id: str
    title: str
    status: Status
    detail: str = ""


class Stop(Exception):  # noqa: N818 - control flow: the rest of a part cannot run
    """Raised after a failed check that later checks depend on."""


# Exit codes of the command (README, "Exit status").
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_SKIPPED = 3  # only with --fail-on-skip


@dataclass
class Report:
    out: Callable[[str], None]
    # Applied to every detail and trail value BEFORE it is stored, so neither a printed line
    # nor the JSON report (which escapes text again) can carry a secret a server echoed.
    redact: Callable[[str], str] = str
    results: list[Result] = field(default_factory=list)
    trail: list[tuple[str, str]] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)

    def record(self, check_id: str, title: str, status: Status, detail: str = "") -> Result:
        result = Result(check_id, self.redact(title), status, self.redact(detail))
        self.results.append(result)
        line = f"{status.value:<4}  {check_id:<4} {result.title}"
        self.out(f"{line}: {result.detail}" if result.detail else line)
        return result

    def check(self, check_id: str, title: str, problem: str | None, ok: str = "") -> bool:
        """PASS when ``problem`` is None, else FAIL with the problem as the detail."""
        if problem is None:
            self.record(check_id, title, Status.PASS, ok)
            return True
        self.record(check_id, title, Status.FAIL, problem)
        return False

    def require(self, check_id: str, title: str, problem: str | None, ok: str = "") -> None:
        """Like :meth:`check`, but a failure stops the part: later checks need this one."""
        if not self.check(check_id, title, problem, ok):
            raise Stop(check_id)

    def skip(self, check_id: str, title: str, reason: str) -> None:
        self.record(check_id, title, Status.SKIP, reason)

    def warn(self, check_id: str, title: str, detail: str) -> None:
        self.record(check_id, title, Status.WARN, detail)

    def note(self, key: str, value: str) -> None:
        value = self.redact(value)
        self.trail.append((key, value))
        self.out(f"      trail: {key} = {value}")

    def stop(self, part: str, after: str) -> None:
        message = f"{part} stopped after {after} failed; its remaining checks did not run"
        self.stopped.append(message)
        self.out(f"FAIL  {message}")

    def count(self, status: Status) -> int:
        return sum(1 for r in self.results if r.status is status)

    def exit_code(self, *, fail_on_skip: bool = False) -> int:
        if self.count(Status.FAIL) or self.stopped or not self.results:
            return EXIT_FAILED
        if fail_on_skip and self.count(Status.SKIP):
            return EXIT_SKIPPED
        return EXIT_OK

    def summary(self) -> list[str]:
        lines = ["", "Summary"]
        for status in (Status.FAIL, Status.SKIP, Status.WARN):
            for r in self.results:
                if r.status is status:
                    lines.append(f"  {status.value:<4} {r.check_id:<4} {r.title}: {r.detail}")
        lines.extend(f"  {message}" for message in self.stopped)
        if self.trail:
            lines.append("What this run added to the stack (synthetic values):")
            lines.extend(f"  {key}: {value}" for key, value in self.trail)
        verdict = "FAILED" if self.count(Status.FAIL) or self.stopped else "PASSED"
        if verdict == "PASSED" and self.count(Status.SKIP):
            verdict = f"PASSED WITH {self.count(Status.SKIP)} SKIPPED"
        lines.append(
            f"SMOKE RESULT: {verdict} (pass={self.count(Status.PASS)} "
            f"fail={self.count(Status.FAIL)} skip={self.count(Status.SKIP)} "
            f"warn={self.count(Status.WARN)})"
        )
        return lines

    def as_json(self) -> dict:
        return {
            "results": [
                {"id": r.check_id, "title": r.title, "status": r.status.value, "detail": r.detail}
                for r in self.results
            ],
            "stopped": list(self.stopped),
            "trail": [{"key": k, "value": v} for k, v in self.trail],
            "counts": {s.value.lower(): self.count(s) for s in Status},
        }
