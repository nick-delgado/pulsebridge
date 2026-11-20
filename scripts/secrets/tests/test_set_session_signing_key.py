"""set-session-signing-key.sh (T44, D16): 32 random bytes into the signing-key secret, never shown.

The stub gcloud (conftest.py) saves what `secrets versions add` reads on standard input, so the
stored key is visible to the test and nowhere else.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "set-session-signing-key.sh"
SECRET = "pbt-session-signing-key"  # noqa: S105 - a secret NAME, not a value
EXISTING = f"projects/100000000001/secrets/{SECRET}/versions/1\n"


def forms(key: bytes) -> list[bytes]:
    """The key as it could leak into text: raw, hex (both cases), base64 (both alphabets)."""
    return [
        key,
        key.hex().encode(),
        key.hex().upper().encode(),
        base64.b64encode(key).rstrip(b"="),
        base64.urlsafe_b64encode(key).rstrip(b"="),
    ]


def assert_key_not_leaked(sandbox, r, key: bytes) -> None:
    for form in forms(key):
        assert form not in r.stdout, "key on stdout"
        assert form not in r.stderr, "key on stderr"
        text = form.decode("latin-1")
        for call in r.calls:
            assert text not in " ".join(call["argv"]), "key in gcloud's argv"
            assert all(text not in v for v in call["env"].values()), "key in gcloud's environment"
    for path in sandbox.root.rglob("*"):
        if path.is_file() and not path.name.startswith("stdin-"):
            data = path.read_bytes()
            assert all(form not in data for form in forms(key)), f"key in {path.name}"


def test_stores_32_random_bytes_in_the_signing_key_secret(sandbox):
    r = sandbox.run(SCRIPT)
    assert r.rc == 0, r.err
    assert len(r.stored) == 1
    key = r.stored[0]
    assert len(key) == 32  # exactly the key: no newline, no encoding
    add = r.add_calls()[0]["argv"]
    assert add[:4] == ["secrets", "versions", "add", SECRET]
    assert "--data-file=-" in add and "--project=test-project-1" in add
    assert r.stdout == b""
    assert "First key" in r.err and "not shown" in r.err
    assert_key_not_leaked(sandbox, r, key)


def test_two_keys_differ(sandbox):
    a = sandbox.run(SCRIPT).stored[0]
    b = sandbox.run(SCRIPT).stored[1]
    assert a != b
    # 32 random bytes: a run of one repeated byte, or a printable-only key, would be a broken
    # source (tr-filtered letters, a constant), not chance.
    assert len(set(a)) > 8 and len(set(b)) > 8


def test_order_existence_check_then_versions_check_then_add(sandbox):
    r = sandbox.run(SCRIPT)
    assert r.rc == 0, r.err
    assert r.subcommands() == [
        ["secrets", "describe", SECRET],
        ["secrets", "versions", "list"],
        ["secrets", "versions", "add"],
    ]
    # describe and list never get the key on their standard input.
    assert r.precheck_stdin == [b"", b""]


def test_an_existing_key_is_kept_without_rotate(sandbox):
    sandbox.set("versions", EXISTING)
    r = sandbox.run(SCRIPT)
    assert r.rc == 0, r.err
    assert r.add_calls() == []
    assert "already has an enabled key" in r.err and "--rotate" in r.err


def test_rotate_adds_a_new_key_and_says_sessions_end(sandbox):
    sandbox.set("versions", EXISTING)
    r = sandbox.run(SCRIPT, "--rotate")
    assert r.rc == 0, r.err
    assert len(r.stored) == 1 and len(r.stored[0]) == 32
    assert "Rotated" in r.err and "refused (401)" in r.err
    assert_key_not_leaked(sandbox, r, r.stored[0])


def test_missing_secret_writes_nothing(sandbox):
    sandbox.set("describe_rc", "1")
    r = sandbox.run(SCRIPT)
    assert r.rc == 1
    assert r.add_calls() == []
    assert "terraform apply" in r.err


def test_unreadable_versions_list_writes_nothing(sandbox):
    # "could not tell" must not be read as "no key yet": that would rotate by accident.
    sandbox.set("list_rc", "1")
    r = sandbox.run(SCRIPT)
    assert r.rc == 1
    assert r.add_calls() == []


def test_failed_add_is_reported(sandbox):
    sandbox.set("add_rc", "1")
    r = sandbox.run(SCRIPT)
    assert r.rc == 1
    assert "could not store the key" in r.err
    assert_key_not_leaked(sandbox, r, r.stored[0])


def test_argument_refused_and_not_repeated(sandbox):
    r = sandbox.run(SCRIPT, "Hunter2-looks-like-a-key")
    assert r.rc == 2 and "Hunter2" not in r.err and r.calls == []


@pytest.mark.parametrize("flag", ["--log-http", "--verbosity=debug", "--verbosity"])
def test_gcloud_logging_flags_refused(sandbox, flag):
    r = sandbox.run(SCRIPT, flag)
    assert r.rc == 2 and "is refused" in r.err and r.calls == []


def test_unknown_option_refused(sandbox):
    r = sandbox.run(SCRIPT, "--generate")
    assert r.rc == 2 and "unknown option" in r.err and r.calls == []


def test_xtrace_refused(sandbox):
    r = sandbox.run(SCRIPT, bash_flags=("-x",))
    assert r.rc == 2 and "xtrace" in r.err and r.calls == []


@pytest.mark.parametrize("args", [("--project=",), ("--prefix=",)])
def test_explicitly_empty_project_or_prefix_refused(sandbox, args):
    r = sandbox.run(SCRIPT, *args)
    assert r.rc == 2 and "empty value" in r.err and r.calls == []


def test_setup_env_names_accepted(sandbox):
    env = sandbox.env(
        CLOUDSDK_CORE_PROJECT=None,
        SECRET_NAME_PREFIX=None,
        GCP_PROJECT_ID="setup-env-proj",
        PULSEBRIDGE_NAME_PREFIX="pbx",
    )
    r = sandbox.run(SCRIPT, env=env)
    assert r.rc == 0, r.err
    add = r.add_calls()[0]["argv"]
    assert add[3] == "pbx-session-signing-key" and "--project=setup-env-proj" in add


def test_gcloud_runs_with_http_logging_off(sandbox):
    r = sandbox.run(SCRIPT)
    env = r.add_calls()[0]["env"]
    assert env["CLOUDSDK_CORE_LOG_HTTP"] == "false"
    assert env["CLOUDSDK_CORE_VERBOSITY"] == "warning"
    assert "--verbosity=warning" in r.add_calls()[0]["argv"]


def test_help(sandbox):
    r = sandbox.run(SCRIPT, "--help")
    assert r.rc == 0 and b"--rotate" in r.stdout and r.calls == []
