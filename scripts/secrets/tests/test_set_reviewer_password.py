"""set-reviewer-password.sh: fhir-proxy's password rules, generate and show, no leaks."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "set-reviewer-password.sh"


def test_stores_exact_bytes_in_the_reviewer_secret(sandbox, fake, no_leak):
    value = fake(32)
    r = sandbox.run(SCRIPT, stdin=value.encode() + b"\n")
    assert r.rc == 0, r.err
    assert r.stored == [value.encode()]
    add = r.add_calls()[0]["argv"]
    assert add[:4] == ["secrets", "versions", "add", "pbt-reviewer-password"]
    assert "--data-file=-" in add
    assert "signed out" in r.err and "30 seconds" in r.err  # what picking it up means
    assert r.stdout == b""
    no_leak(sandbox, r, value)


@pytest.mark.parametrize(("length", "rc"), [(15, 2), (16, 0), (128, 0), (129, 2)])
def test_length_rule_16_to_128(sandbox, length, rc):
    r = sandbox.run(SCRIPT, stdin=b"r" * length)
    assert r.rc == rc, r.err
    if rc:
        assert r.add_calls() == []
        assert ("at least 16" if length < 16 else "at most 128") in r.err
    else:
        assert r.stored == [b"r" * length]


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"long-enough-password\nmore-text",
        b"long-enough-password\r",
        b"long-enough-pass\x00x",
        "long-enough-pässword".encode(),
    ],
    ids=["empty", "newline", "cr", "nul", "non-ascii"],
)
def test_bad_passwords_refused(sandbox, raw):
    r = sandbox.run(SCRIPT, stdin=raw)
    assert r.rc == 2 and r.add_calls() == []


def test_generate_on_a_terminal_shows_it_once_after_storing(sandbox):
    r, shown = sandbox.run_pty(SCRIPT, "--generate", answers=[])
    assert r.rc == 0, shown
    stored = r.stored[0]
    assert re.fullmatch(rb"[A-Za-z0-9]{24}", stored), stored
    assert shown.count(stored) == 1
    # shown after gcloud reported success, not before
    assert shown.index(b"Created version") < shown.index(stored)


def test_generate_refused_when_stdout_is_not_a_terminal(sandbox):
    r = sandbox.run(SCRIPT, "--generate")
    assert r.rc == 2
    assert "--show" in r.err
    assert r.calls == []  # refused before anything is written


def test_generate_with_show_prints_to_a_pipe(sandbox):
    r = sandbox.run(SCRIPT, "--generate", "--show")
    assert r.rc == 0, r.err
    stored = r.stored[0]
    assert re.fullmatch(rb"[A-Za-z0-9]{24}", stored)
    assert r.stdout == stored + b"\n"
    assert stored not in r.stderr


def test_generated_passwords_differ(sandbox):
    a = sandbox.run(SCRIPT, "--generate", "--show")
    b = sandbox.run(SCRIPT, "--generate", "--show")
    assert a.rc == b.rc == 0
    assert a.stdout != b.stdout


def test_generate_reads_dev_urandom(sandbox, strace, tmp_path):
    log = tmp_path / "strace.log"
    r = sandbox.run(
        SCRIPT,
        "--generate",
        "--show",
        prefix=(strace, "-f", "-e", "trace=open,openat", "-o", str(log)),
    )
    assert r.rc == 0, r.err
    assert '"/dev/urandom"' in log.read_text()


def test_generated_password_not_shown_when_storing_fails(sandbox):
    sandbox.set("add_rc", "1")
    r = sandbox.run(SCRIPT, "--generate", "--show")
    assert r.rc == 1
    assert r.stdout == b""
    assert r.stored[0] not in r.stderr


def test_show_without_generate_refused(sandbox, fake):
    r = sandbox.run(SCRIPT, "--show", stdin=fake(20).encode())
    assert r.rc == 2 and "only applies to --generate" in r.err and r.calls == []


def test_argument_refused_and_not_repeated(sandbox, fake):
    value = fake(24).strip()
    r = sandbox.run(SCRIPT, value)
    assert r.rc == 2 and value not in r.err and r.calls == []


@pytest.mark.parametrize("flag", ["--log-http", "--verbosity=debug"])
def test_gcloud_logging_flags_refused(sandbox, fake, flag):
    r = sandbox.run(SCRIPT, flag, stdin=fake(20).encode())
    assert r.rc == 2 and r.calls == []
    assert "is refused" in r.err and "HTTP log" in r.err


def test_missing_secret_points_to_terraform_apply(sandbox, fake):
    sandbox.set("describe_rc", "1")
    r = sandbox.run(SCRIPT, stdin=fake(20).encode())
    assert r.rc == 1 and "terraform apply" in r.err and r.add_calls() == []


def test_gcloud_failure(sandbox, fake, no_leak):
    sandbox.set("add_rc", "2")
    value = fake(30)
    r = sandbox.run(SCRIPT, stdin=value.encode())
    assert r.rc == 1 and "could not add the version" in r.err
    no_leak(sandbox, r, value)


def test_xtrace_refused(sandbox, fake):
    value = fake(30)
    r = sandbox.run(SCRIPT, stdin=value.encode(), bash_flags=("-x",))
    assert r.rc == 2 and "xtrace" in r.err and r.calls == []
    assert value.encode() not in r.stderr


def test_value_in_no_exec_argv_or_environment(sandbox, fake, strace, tmp_path):
    value = fake(30)
    log = tmp_path / "strace.log"
    r = sandbox.run(
        SCRIPT,
        stdin=value.encode(),
        prefix=(strace, "-f", "-v", "-s", "100000", "-e", "trace=execve", "-o", str(log)),
    )
    assert r.rc == 0, r.err
    trace = log.read_text()
    assert "gcloud" in trace and value.strip() not in trace


def test_prompt_twice_hidden(sandbox, fake, no_leak):
    value = fake(24)
    r, shown = sandbox.run_pty(SCRIPT, answers=[value, value])
    assert r.rc == 0, shown
    assert r.stored == [value.encode()]
    assert value.strip().encode() not in shown
    no_leak(sandbox, r, value)


def test_prompt_mismatch_refused(sandbox, fake):
    r, shown = sandbox.run_pty(SCRIPT, answers=[fake(24), fake(24)])
    assert r.rc == 2 and b"differ" in shown and r.add_calls() == []


def test_prompt_short_password_refused_after_confirmation(sandbox):
    r, shown = sandbox.run_pty(SCRIPT, answers=["short-pass", "short-pass"])
    assert r.rc == 2 and b"at least 16" in shown and r.add_calls() == []
