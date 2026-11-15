"""Test harness for scripts/secrets (T27).

The scripts run against a stub ``gcloud`` written into each test's temporary directory. The stub
records every call (argv and its whole environment) as one JSON line, and for
``secrets versions add`` it saves what it read on standard input, byte for byte. Nothing reaches
Google: the stub is first on PATH, and the scripts run with an environment built from scratch
(no credentials, no project from the session).

Values are fake, generated per test with ``secrets.token_hex``, and padded with spaces so that a
test can see whether leading and trailing spaces survive.
"""

from __future__ import annotations

import json
import os
import pty
import secrets
import select
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
REPO = SCRIPTS.parent.parent
SET_SECRET = SCRIPTS / "set-secret.sh"
SET_REVIEWER = SCRIPTS / "set-reviewer-password.sh"

STUB = """#!{python}
import json, os, sys
state = os.environ["STUB_STATE"]
argv = sys.argv[1:]
with open(os.path.join(state, "calls.jsonl"), "a") as log:
    log.write(json.dumps({{"argv": argv, "env": dict(os.environ)}}) + "\\n")
def setting(name, default):
    path = os.path.join(state, name)
    return open(path).read() if os.path.exists(path) else default
words = [a for a in argv if not a.startswith("-")]
if words[:2] == ["secrets", "describe"]:
    sys.stderr.write(setting("describe_stderr", ""))
    sys.exit(int(setting("describe_rc", "0")))
if words[:3] == ["secrets", "versions", "list"]:
    sys.stdout.write(setting("versions", ""))
    sys.stderr.write(setting("list_stderr", ""))
    sys.exit(int(setting("list_rc", "0")))
if words[:3] == ["secrets", "versions", "add"]:
    data = sys.stdin.buffer.read()
    n = len([f for f in os.listdir(state) if f.startswith("stdin-")])
    with open(os.path.join(state, "stdin-%d.bin" % n), "wb") as out:
        out.write(data)
    rc = int(setting("add_rc", "0"))
    if rc == 0:
        sys.stderr.write("Created version [1] of the secret.\\n")
    else:
        sys.stderr.write("ERROR: (gcloud.secrets.versions.add) PERMISSION_DENIED (stub)\\n")
    sys.exit(rc)
sys.stderr.write("stub gcloud: unexpected call %r\\n" % (argv,))
sys.exit(99)
"""


# What the scripts run besides gcloud: bash (also through `env` in the shebang), dirname, tr,
# head, cat (the help and pick-up text).
TOOLS = ("bash", "env", "dirname", "tr", "head", "cat")


def fake_value(length: int = 40) -> str:
    """A fake secret: printable ASCII with a leading and a trailing space, exactly `length` long."""
    core = "fake-" + secrets.token_hex(length)
    return " " + core[: length - 2] + " "


@dataclass
class Result:
    rc: int
    stdout: bytes
    stderr: bytes
    calls: list[dict] = field(default_factory=list)
    stored: list[bytes] = field(default_factory=list)

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace")

    def subcommands(self) -> list[list[str]]:
        return [[a for a in c["argv"] if not a.startswith("-")][:3] for c in self.calls]

    def add_calls(self) -> list[dict]:
        return [c for c in self.calls if c["argv"][:3] == ["secrets", "versions", "add"]]


class Sandbox:
    """One test's temporary world: the stub gcloud, a HOME and a TMPDIR."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.state = root / "state"
        self.home = root / "home"
        self.tmp = root / "tmp"
        for d in (self.bin, self.state, self.home, self.tmp):
            d.mkdir()
        stub = self.bin / "gcloud"
        stub.write_text(STUB.format(python=sys.executable))
        stub.chmod(0o755)
        # PATH holds only this directory: the stub and links to the few tools the scripts use.
        # The real gcloud (often in /usr/bin) can never be reached, even if the stub is removed.
        for tool in TOOLS:
            found = shutil.which(tool, path="/usr/local/bin:/usr/bin:/bin")
            assert found, f"{tool} not found"
            (self.bin / tool).symlink_to(found)

    def set(self, name: str, value: str) -> None:
        (self.state / name).write_text(value)

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "PATH": str(self.bin),
            "CLOUDSDK_CONFIG": str(self.root / "no-gcloud-config"),
            "HOME": str(self.home),
            "TMPDIR": str(self.tmp),
            "STUB_STATE": str(self.state),
            "CLOUDSDK_CORE_PROJECT": "test-project-1",
            "SECRET_NAME_PREFIX": "pbt",
        }
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def collect(self, rc: int, stdout: bytes, stderr: bytes) -> Result:
        calls = []
        log = self.state / "calls.jsonl"
        if log.exists():
            calls = [json.loads(line) for line in log.read_text().splitlines()]
        stored = [
            p.read_bytes()
            for p in sorted(self.state.glob("stdin-*.bin"), key=lambda p: int(p.stem[6:]))
        ]
        return Result(rc, stdout, stderr, calls, stored)

    def run(
        self,
        script: Path,
        *args: str,
        stdin: bytes = b"",
        bash_flags: tuple[str, ...] = (),
        env: dict[str, str] | None = None,
        prefix: tuple[str, ...] = (),
    ) -> Result:
        cmd = [*prefix, "bash", *bash_flags, str(script), *args]
        proc = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            env=env if env is not None else self.env(),
            cwd=self.root,
            timeout=60,
        )
        return self.collect(proc.returncode, proc.stdout, proc.stderr)

    def run_pty(self, script: Path, *args: str, answers: list[str], tty_stdout: bool = True):
        """Run with standard input (and output) on a pseudo-terminal; type `answers` in order,
        each after a prompt. Returns the Result and everything the terminal showed."""
        master, slave = pty.openpty()
        stdout_target = slave if tty_stdout else subprocess.PIPE
        proc = subprocess.Popen(
            ["bash", str(script), *args],
            stdin=slave,
            stdout=stdout_target,
            stderr=slave,
            env=self.env(),
            cwd=self.root,
            start_new_session=True,
        )
        os.close(slave)
        shown = b""
        pending = list(answers)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:  # EIO: the other side closed
                    break
                if not chunk:
                    break
                shown += chunk
                # Answer once the prompt is on the screen and echo has been switched off by
                # read -s (the prompt is printed just before read runs; give it a moment).
                if pending and shown.rstrip().endswith(b":"):
                    time.sleep(0.3)
                    os.write(master, pending.pop(0).encode() + b"\n")
            elif proc.poll() is not None:
                break
        proc.wait(timeout=30)
        os.close(master)
        out = proc.stdout.read() if proc.stdout else b""
        return self.collect(proc.returncode, out, b""), shown


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


@pytest.fixture
def fake():
    """fake(length) -> a fresh fake secret value."""
    return fake_value


@pytest.fixture
def no_leak():
    """no_leak(sandbox, result, value): fails if the value leaked anywhere it must not be."""
    return assert_no_leak


@pytest.fixture
def strace():
    """The strace binary, or skip (strace is Linux only)."""
    if not strace_available():
        pytest.skip("strace not available or not permitted (Linux only)")
    return shutil.which("strace")


def assert_no_leak(sandbox: Sandbox, result: Result, value: str) -> None:
    """The value is in no call's argv or environment, not in the script's output, and in no file
    under the sandbox except the stub's stdin captures (where it is supposed to be)."""
    needle = value.encode()
    assert needle not in result.stdout, "value on stdout"
    assert needle not in result.stderr, "value on stderr"
    for call in result.calls:
        assert value not in " ".join(call["argv"]), f"value in argv of {call['argv'][:3]}"
        for k, v in call["env"].items():
            assert value not in v and value not in k, f"value in gcloud's environment ({k})"
    for path in sandbox.root.rglob("*"):
        if path.is_file() and not path.name.startswith("stdin-"):
            where = path.relative_to(sandbox.root)
            assert needle not in path.read_bytes(), f"value in file {where}"


def strace_available() -> bool:
    if not shutil.which("strace"):
        return False
    probe = subprocess.run(
        ["strace", "-f", "-o", os.devnull, "-e", "trace=execve", "true"], capture_output=True
    )
    return probe.returncode == 0
