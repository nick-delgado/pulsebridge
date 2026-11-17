"""Run the smoke command in-process against an ``e2e_fake_stack.Stack`` and read its output."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from e2e_fake_stack import Stack
from pulsebridge_smoke.cli import main
from pulsebridge_smoke.http import Response

BASE = "https://dashboard.example.test/api"
STATUSES = ("PASS", "FAIL", "SKIP", "WARN")


@dataclass
class Run:
    code: int
    lines: list[str]
    sleeps: list[float] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def line_of(self, check_id: str) -> str:
        for line in self.lines:
            parts = line.split()
            if len(parts) >= 2 and parts[0] in STATUSES and parts[1] == check_id:
                return line
        raise AssertionError(f"{check_id} was not reported:\n{self.text}")

    def status_of(self, check_id: str) -> str:
        return self.line_of(check_id).split()[0]

    def reported(self, check_id: str) -> bool:
        try:
            self.line_of(check_id)
        except AssertionError:
            return False
        return True


def run(
    stack: Stack | None,
    password: str,
    *args: str,
    env: dict | None = None,
    transport: object | None = None,
    **kwargs: object,
) -> Run:
    lines: list[str] = []
    sleeps: list[float] = []
    environment = {"PULSEBRIDGE_BASE_URL": BASE, "PULSEBRIDGE_REVIEWER_PASSWORD": password}
    environment.update(env or {})
    code = main(
        ["--poll-timeout", "0", "--poll-interval", "0", *args],
        env=environment,
        transport=transport if transport is not None else stack.transport,  # type: ignore[union-attr]
        out=lines.append,
        sleep=sleeps.append,
        **kwargs,  # type: ignore[arg-type]
    )
    return Run(code, lines, sleeps)


def edit_json(edit: Callable[[dict], None]) -> Callable[[Response], Response]:
    def fn(response: Response) -> Response:
        body = response.json()
        edit(body)
        return Response(response.status, json.dumps(body).encode(), response.headers)

    return fn


def nth_only(n: int, fn: Callable[[Response], Response]) -> Callable[[Response], Response]:
    """Apply ``fn`` to the n-th matching answer only (1-based)."""
    seen = [0]

    def wrapped(response: Response) -> Response:
        seen[0] += 1
        return fn(response) if seen[0] == n else response

    return wrapped


def bp_component(observation: dict, loinc: str) -> dict:
    return next(c for c in observation["component"] if c["code"]["coding"][0]["code"] == loinc)
