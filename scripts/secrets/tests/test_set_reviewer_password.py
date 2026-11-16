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


# ---- Review fixes ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "char",
    [" ", "=", "'", '"', "\\", "`", "$", "!", "#", "&", ";", "|", "<", ">", "*", "?"],
)
def test_reviewer_keeps_full_printable_ascii(sandbox, char):
    """Characters set-secret.sh refuses for the edge VM are fine for fhir-proxy."""
    value = "Reviewer" + char + "Password" + char + "x"
    r = sandbox.run(SCRIPT, stdin=value.encode())
    assert r.rc == 0, r.err
    assert r.stored == [value.encode()]


def test_leading_hyphen_accepted_for_the_reviewer(sandbox):
    r = sandbox.run(SCRIPT, stdin=b"-reviewer-password-x")
    assert r.rc == 0 and r.stored == [b"-reviewer-password-x"]


def test_only_spaces_refused(sandbox):
    r = sandbox.run(SCRIPT, stdin=b" " * 20)
    assert r.rc == 2 and "only spaces" in r.err and r.add_calls() == []


def test_backslashes_stored_exactly_from_a_pipe(sandbox):
    value = b"back\\slash\\\\path\\n-not-a-newline\\"
    r = sandbox.run(SCRIPT, stdin=value + b"\n")
    assert r.rc == 0, r.err
    assert r.stored == [value]


def test_backslashes_stored_exactly_from_the_prompt(sandbox):
    value = "back\\slash\\\\typed\\n\\"
    r, shown = sandbox.run_pty(SCRIPT, answers=[value, value])
    assert r.rc == 0, shown
    assert r.stored == [value.encode()]


def test_existence_checked_before_the_prompt(sandbox):
    sandbox.set("describe_rc", "1")
    r, shown = sandbox.run_pty(SCRIPT, answers=[])
    assert r.rc == 1, shown
    assert b"terraform apply" in shown
    assert b"Enter" not in shown  # never asked for a password that cannot be stored
    assert r.add_calls() == []


def test_order_existence_check_then_add(sandbox, fake):
    r = sandbox.run(SCRIPT, stdin=fake(20).encode())
    assert r.rc == 0, r.err
    assert r.subcommands() == [
        ["secrets", "describe", "pbt-reviewer-password"],
        ["secrets", "versions", "add"],
    ]
    assert r.precheck_stdin == [b""]
    assert "--quiet" in r.calls[0]["argv"]


def test_ctrl_d_at_the_first_prompt_stores_nothing(sandbox):
    r, shown = sandbox.run_pty(SCRIPT, answers=["\x04", "\x04"])
    assert r.rc == 2, shown
    assert b"no input (end of input before Enter)" in shown
    assert r.add_calls() == []


def test_value_like_option_not_repeated(sandbox):
    r = sandbox.run(SCRIPT, "--Hunter2!Secret")
    assert r.rc == 2 and "Hunter2" not in r.err and r.calls == []


@pytest.mark.parametrize("args", [("--project=",), ("--prefix=",)])
def test_explicitly_empty_project_or_prefix_refused(sandbox, fake, args):
    r = sandbox.run(SCRIPT, *args, stdin=fake(20).encode())
    assert r.rc == 2 and "empty value" in r.err and r.calls == []


def test_setup_env_names_accepted(sandbox, fake):
    env = sandbox.env(
        CLOUDSDK_CORE_PROJECT=None,
        SECRET_NAME_PREFIX=None,
        GCP_PROJECT_ID="setup-env-proj",
        PULSEBRIDGE_NAME_PREFIX="pbx",
    )
    r = sandbox.run(SCRIPT, stdin=fake(20).encode(), env=env)
    assert r.rc == 0, r.err
    add = r.add_calls()[0]["argv"]
    assert add[3] == "pbx-reviewer-password" and "--project=setup-env-proj" in add
