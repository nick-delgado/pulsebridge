"""set-secret.sh: what it stores, what it refuses, and that the value leaks nowhere."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS / "set-secret.sh"
REPO = SCRIPTS.parent.parent
IDENTITY_MAIN = REPO / "infra" / "modules" / "identity" / "main.tf"

ALLOWED = ["db-root-password", "openemr-db-password", "openemr-admin-password"]
# Every punctuation character set-secret.sh accepts (README, "Value rules").
ALLOWED_PUNCTUATION = "!#$%&()*+,-./:;<>?@[]^_{|}~"


@pytest.fixture
def fake(fake_edge):
    """In this module every fake value is one set-secret.sh accepts."""
    return fake_edge


# ---- What is stored ----------------------------------------------------------------------------


@pytest.mark.parametrize("terminator", [b"", b"\n"], ids=["no-newline", "one-trailing-newline"])
def test_stores_exact_bytes_without_newline(sandbox, fake, no_leak, terminator):
    value = fake(40)
    r = sandbox.run(SCRIPT, "db-root-password", stdin=value.encode() + terminator)
    assert r.rc == 0, r.err
    assert r.stored == [value.encode()]
    add = r.add_calls()[0]
    assert add["argv"][:4] == ["secrets", "versions", "add", "pbt-db-root-password"]
    assert "--data-file=-" in add["argv"]
    assert "--project=test-project-1" in add["argv"]
    no_leak(sandbox, r, value)


@pytest.mark.parametrize("name", ALLOWED)
def test_each_allowed_name_writes_prefixed_secret(sandbox, fake, name):
    r = sandbox.run(SCRIPT, name, stdin=fake(20).encode())
    assert r.rc == 0, r.err
    assert r.add_calls()[0]["argv"][3] == f"pbt-{name}"


def test_order_existence_check_then_version_check_then_add(sandbox, fake):
    r = sandbox.run(SCRIPT, "openemr-db-password", stdin=fake(20).encode())
    assert r.rc == 0, r.err
    assert r.subcommands() == [
        ["secrets", "describe", "pbt-openemr-db-password"],
        ["secrets", "versions", "list"],
        ["secrets", "versions", "add"],
    ]


def test_gcloud_runs_without_http_log_or_debug_even_if_environment_asks(sandbox, fake):
    env = sandbox.env(CLOUDSDK_CORE_LOG_HTTP="true", CLOUDSDK_CORE_VERBOSITY="debug")
    r = sandbox.run(SCRIPT, "db-root-password", stdin=fake(20).encode(), env=env)
    assert r.rc == 0, r.err
    for call in r.calls:
        assert not any(a.startswith("--log-http") for a in call["argv"])
        assert "--verbosity=debug" not in call["argv"]
        assert call["argv"][-1] == "--verbosity=warning"
        assert call["env"]["CLOUDSDK_CORE_LOG_HTTP"] == "false"
        assert call["env"]["CLOUDSDK_CORE_VERBOSITY"] == "warning"
        assert call["env"]["CLOUDSDK_CORE_DISABLE_FILE_LOGGING"] == "true"


def test_value_in_no_exec_argv_or_environment(sandbox, fake, strace, tmp_path):
    """strace records every program started (execve) with its full argv and environment, so a
    leak through any helper (not only gcloud) shows here."""
    value = fake(40)
    log = tmp_path / "strace.log"
    r = sandbox.run(
        SCRIPT,
        "db-root-password",
        stdin=value.encode(),
        prefix=(strace, "-f", "-v", "-s", "100000", "-e", "trace=execve", "-o", str(log)),
    )
    assert r.rc == 0, r.err
    trace = log.read_text()
    assert "execve(" in trace and "gcloud" in trace  # the trace did see the calls
    assert value not in trace


# ---- Names -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "pointer"),
    [
        ("google-health-client-id", "connect-google-health/connect.sh"),
        ("google-health-client-secret", "connect-google-health/connect.sh"),
        ("google-health-refresh-token", "connect-google-health/connect.sh"),
        ("openemr-client-private-key", "written by the edge VM itself"),
        ("reviewer-password", "set-reviewer-password.sh"),
        ("pbt-db-root-password", "without the prefix, for example 'db-root-password'"),
        ("db-password", "unknown short name"),
        ("", "missing <short-name>"),
    ],
)
def test_refused_names_point_elsewhere_and_call_nothing(sandbox, fake, name, pointer):
    args = (name,) if name else ()
    r = sandbox.run(SCRIPT, *args, stdin=fake(20).encode())
    assert r.rc == 2
    assert pointer in r.err
    assert r.calls == []


def test_allowed_names_are_exactly_the_hand_set_secrets_of_module_identity(sandbox, fake):
    """Module identity records who writes each secret (`written_by`). The secrets written by
    "scripts/secrets (T27)" are the ones this script writes; every other identity secret is
    refused with a pointer. If identity gains a secret, this test makes someone decide."""
    text = IDENTITY_MAIN.read_text()
    entry = r'^\s*"([a-z-]+)" = \{\s*\n\s*readers\s*=.*\n\s*written_by\s*=\s*"([^"]*)"'
    entries = re.findall(entry, text, re.M)
    assert len(entries) == 8, entries
    hand_set = sorted(k for k, w in entries if w.startswith("scripts/secrets (T27)"))
    assert hand_set == sorted(ALLOWED)
    for key, _ in entries:
        r = sandbox.run(SCRIPT, key, stdin=fake(20).encode())
        if key in ALLOWED:
            assert r.rc == 0, (key, r.err)
        else:
            assert r.rc == 2 and "unknown" not in r.err, (key, r.err)


# ---- Arguments ---------------------------------------------------------------------------------


def test_value_given_as_argument_is_refused_and_not_repeated(sandbox, fake):
    value = fake(30)
    r = sandbox.run(SCRIPT, "db-root-password", value, stdin=b"")
    assert r.rc == 2
    assert "never taken from an argument" in r.err
    assert value not in r.err
    assert r.calls == []


@pytest.mark.parametrize("flag", ["--log-http", "--verbosity=debug", "--verbosity"])
def test_gcloud_logging_flags_refused(sandbox, fake, flag):
    r = sandbox.run(SCRIPT, flag, "db-root-password", stdin=fake(20).encode())
    assert r.rc == 2
    assert "is refused" in r.err
    assert r.calls == []


def test_unknown_option_refused(sandbox, fake):
    r = sandbox.run(SCRIPT, "--force", "db-root-password", stdin=fake(20).encode())
    assert r.rc == 2 and "unknown option" in r.err and r.calls == []


def test_project_and_prefix_from_flags_override_environment(sandbox, fake):
    r = sandbox.run(
        SCRIPT,
        "--project",
        "other-proj-2",
        "--prefix=zz",
        "db-root-password",
        stdin=fake(20).encode(),
    )
    assert r.rc == 0, r.err
    add = r.add_calls()[0]["argv"]
    assert add[3] == "zz-db-root-password" and "--project=other-proj-2" in add


@pytest.mark.parametrize(
    ("unset", "message"),
    [("CLOUDSDK_CORE_PROJECT", "no project"), ("SECRET_NAME_PREFIX", "no secret name prefix")],
)
def test_missing_project_or_prefix_refused(sandbox, fake, unset, message):
    env = sandbox.env(**{unset: None})
    r = sandbox.run(SCRIPT, "db-root-password", stdin=fake(20).encode(), env=env)
    assert r.rc == 2 and message in r.err and r.calls == []


@pytest.mark.parametrize(
    ("flag", "message"),
    [
        ("--project=Bad_Project", "not a valid project ID"),
        ("--prefix=-x", "not a valid secret name prefix"),
    ],
)
def test_invalid_project_or_prefix_refused(sandbox, fake, flag, message):
    r = sandbox.run(SCRIPT, flag, "db-root-password", stdin=fake(20).encode())
    assert r.rc == 2 and message in r.err and r.calls == []


# ---- Values ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "empty"),
        (b"\n", "empty"),
        (b"first-part-long-enough\nsecond-part-long", "newline"),
        (b"long-enough-value-here\n\n", "newline"),
        (b"long-enough-value\x00tail-part", "NUL"),
        (b"long-enough-value-here\r\n", "carriage return"),
        (b"long-enough\tvalue-here", "control character"),
        ("long-enough-välue-here".encode(), "outside printable ASCII"),
        (b"x" * 15, "at least 16"),
        (b"x" * 129, "at most 128"),
    ],
    ids=[
        "empty",
        "only-newline",
        "inner-newline",
        "two-newlines",
        "nul",
        "crlf",
        "tab",
        "non-ascii",
        "15-chars",
        "129-chars",
    ],
)
def test_bad_values_refused_without_showing_them(sandbox, raw, message):
    r = sandbox.run(SCRIPT, "db-root-password", stdin=raw)
    assert r.rc == 2, r.err
    assert message in r.err
    assert r.add_calls() == []
    visible = raw.replace(b"\x00", b"").strip()
    if len(visible) > 3:
        assert visible not in r.stderr and visible not in r.stdout


@pytest.mark.parametrize("length", [16, 128])
def test_length_boundaries_accepted(sandbox, length):
    value = ("b" * length).encode()
    r = sandbox.run(SCRIPT, "db-root-password", stdin=value)
    assert r.rc == 0, r.err
    assert r.stored == [value]


# ---- Preconditions and failures ----------------------------------------------------------------


def test_missing_secret_points_to_terraform_apply_and_reads_no_value(sandbox, fake):
    sandbox.set("describe_rc", "1")
    sandbox.set("describe_stderr", "ERROR: NOT_FOUND: Secret [pbt-db-root-password] not found.\n")
    r = sandbox.run(SCRIPT, "db-root-password", stdin=fake(20).encode())
    assert r.rc == 1
    assert "terraform apply -target=module.stack.module.identity" in r.err
    assert "NOT_FOUND" in r.err  # gcloud's own message is passed on
    assert r.add_calls() == []


def test_gcloud_missing_from_path(sandbox, fake):
    (sandbox.bin / "gcloud").unlink()
    r = sandbox.run(SCRIPT, "db-root-password", stdin=fake(20).encode())
    assert r.rc == 1 and "gcloud is not on PATH" in r.err


def test_existing_version_refused_without_replace(sandbox, fake):
    sandbox.set("versions", "projects/123/secrets/pbt-db-root-password/versions/1\n")
    r = sandbox.run(SCRIPT, "db-root-password", stdin=fake(20).encode())
    assert r.rc == 2
    assert "already has an enabled version (1)" in r.err and "--replace" in r.err
    assert r.add_calls() == []


def test_existing_version_with_replace_adds(sandbox, fake):
    sandbox.set("versions", "projects/123/secrets/pbt-db-root-password/versions/1\n")
    value = fake(20)
    r = sandbox.run(SCRIPT, "--replace", "db-root-password", stdin=value.encode())
    assert r.rc == 0, r.err
    assert r.stored == [value.encode()]


def test_version_list_failure_stops(sandbox, fake):
    sandbox.set("list_rc", "1")
    r = sandbox.run(SCRIPT, "db-root-password", stdin=fake(20).encode())
    assert r.rc == 1 and "could not list the versions" in r.err
    assert r.add_calls() == []


def test_gcloud_add_failure_exits_non_zero_without_the_value(sandbox, fake, no_leak):
    sandbox.set("add_rc", "3")
    value = fake(30)
    r = sandbox.run(SCRIPT, "db-root-password", stdin=value.encode())
    assert r.rc == 1
    assert "could not add the version (exit code 3)" in r.err
    assert "stored a new version" not in r.err
    no_leak(sandbox, r, value)


# ---- Generate ----------------------------------------------------------------------------------


def test_generate_stores_32_letters_and_digits_and_shows_nothing(sandbox):
    first = sandbox.run(SCRIPT, "--generate", "openemr-admin-password", stdin=b"ignored")
    assert first.rc == 0, first.err
    value = first.stored[0]
    assert re.fullmatch(rb"[A-Za-z0-9]{32}", value), value
    assert value not in first.stdout and value not in first.stderr
    second = sandbox.run(SCRIPT, "--generate", "openemr-admin-password", stdin=b"")
    assert second.stored[1] != value


def test_generate_reads_dev_urandom(sandbox, strace, tmp_path):
    log = tmp_path / "strace.log"
    r = sandbox.run(
        SCRIPT,
        "--generate",
        "db-root-password",
        prefix=(strace, "-f", "-e", "trace=open,openat", "-o", str(log)),
    )
    assert r.rc == 0, r.err
    assert '"/dev/urandom"' in log.read_text()


def test_generated_values_cover_the_alphabet(sandbox):
    """62 symbols, 20 x 32 = 640 draws: every class appears, and no symbol dominates."""
    seen = b""
    for _ in range(20):
        r = sandbox.run(SCRIPT, "--replace", "--generate", "db-root-password")
        assert r.rc == 0, r.err
    for v in sandbox.collect(0, b"", b"").stored:
        seen += v
    assert re.search(rb"[a-z]", seen) and re.search(rb"[A-Z]", seen) and re.search(rb"[0-9]", seen)
    assert max(seen.count(bytes([c])) for c in set(seen)) < 40  # expected about 10


# ---- Shell options -----------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["bash -x", "SHELLOPTS=xtrace"])
def test_xtrace_refused_before_anything_runs(sandbox, fake, how):
    value = fake(30)
    if how == "bash -x":
        r = sandbox.run(SCRIPT, "db-root-password", stdin=value.encode(), bash_flags=("-x",))
    else:
        env = sandbox.env(SHELLOPTS="xtrace")
        r = sandbox.run(SCRIPT, "db-root-password", stdin=value.encode(), env=env)
    assert r.rc == 2
    assert "refusing to run with xtrace" in r.err
    assert r.calls == []
    assert value.encode() not in r.stderr


def test_allexport_does_not_export_the_value(sandbox, fake, no_leak):
    value = fake(30)
    env = sandbox.env(SHELLOPTS="allexport")
    r = sandbox.run(SCRIPT, "db-root-password", stdin=value.encode(), env=env)
    assert r.rc == 0, r.err
    no_leak(sandbox, r, value)


def test_inherited_variable_of_the_same_name_does_not_carry_the_value(sandbox, fake, no_leak):
    value = fake(30)
    env = sandbox.env(sv_value="decoy", sv_confirm="decoy")
    r = sandbox.run(SCRIPT, "db-root-password", stdin=value.encode(), env=env)
    assert r.rc == 0, r.err
    no_leak(sandbox, r, value)


# ---- Terminal prompt ---------------------------------------------------------------------------


def test_prompt_hides_input_and_stores_exact_bytes(sandbox, fake, no_leak):
    value = fake(30)
    r, shown = sandbox.run_pty(SCRIPT, "db-root-password", answers=[value, value])
    assert r.rc == 0, shown
    assert b"(input hidden)" in shown and b"again" in shown
    assert value.encode() not in shown, "the typed value was echoed"
    assert r.stored == [value.encode()]
    no_leak(sandbox, r, value)


def test_prompt_mismatch_refused(sandbox, fake):
    a, b = fake(30), fake(30)
    r, shown = sandbox.run_pty(SCRIPT, "db-root-password", answers=[a, b])
    assert r.rc == 2
    assert b"the two entries differ" in shown
    assert r.add_calls() == []
    assert a.encode() not in shown and b.encode() not in shown


def test_help_exits_zero_and_calls_nothing(sandbox):
    r = sandbox.run(SCRIPT, "--help")
    assert r.rc == 0 and b"Usage:" in r.stdout and r.calls == []


def test_runs_when_invoked_directly(sandbox, fake):
    """The shebang and the executable bit work (the README tells people to run it directly)."""
    proc = subprocess.run(
        [str(SCRIPT), "db-root-password"],
        input=fake(20).encode(),
        capture_output=True,
        env=sandbox.env(),
        cwd=sandbox.root,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


# ---- Edge-VM character rules (review F1) -------------------------------------------------------

REFUSED_CHARACTERS = [
    (" ", "a space"),
    ("=", "an equals sign (=)"),
    ("'", "a single quote (')"),
    ('"', 'a double quote (")'),
    ("\\", "a backslash (\\)"),
    ("`", "a backtick (`)"),
]


@pytest.mark.parametrize("name", ALLOWED)
@pytest.mark.parametrize(
    ("char", "message"),
    REFUSED_CHARACTERS,
    ids=["space", "equals", "squote", "dquote", "backslash", "backtick"],
)
def test_characters_openemr_mangles_are_refused(sandbox, fake, name, char, message):
    value = fake(20) + char + "tail"
    r = sandbox.run(SCRIPT, name, stdin=value.encode())
    assert r.rc == 2, r.err
    assert f"the value contains {message}" in r.err
    assert "Allowed: letters, digits and" in r.err and "--generate" in r.err
    assert r.add_calls() == []
    assert value.encode() not in r.stderr


@pytest.mark.parametrize("name", ALLOWED)
def test_leading_hyphen_refused(sandbox, fake, name):
    r = sandbox.run(SCRIPT, name, stdin=("-" + fake(20)).encode())
    assert r.rc == 2 and "starts with a hyphen" in r.err and r.add_calls() == []


@pytest.mark.parametrize("name", ALLOWED)
def test_every_allowed_punctuation_character_is_stored(sandbox, name):
    value = "Ab1" + ALLOWED_PUNCTUATION + "z9"
    r = sandbox.run(SCRIPT, name, stdin=value.encode())
    assert r.rc == 0, r.err
    assert r.stored == [value.encode()]


def test_backslash_in_piped_input_is_seen(sandbox, fake):
    """Without `read -r` the backslash would vanish and the value would be stored altered."""
    r = sandbox.run(SCRIPT, "db-root-password", stdin=(fake(20) + "\\q").encode())
    assert r.rc == 2 and "backslash" in r.err and r.add_calls() == []


def test_backslash_typed_at_the_prompt_is_seen(sandbox, fake):
    value = fake(20) + "\\q"
    r, shown = sandbox.run_pty(SCRIPT, "db-root-password", answers=[value, value])
    assert r.rc == 2, shown
    assert b"backslash" in shown and r.add_calls() == []


# ---- Arguments are shown only when they look like names (review F2) ----------------------------


@pytest.mark.parametrize(
    "args",
    [
        ("Hunter2!Secret#Value",),
        ("--project=Hunter2!Secret#Value", "db-root-password"),
        ("--prefix=Hunter2!Secret#Value", "db-root-password"),
        ("-XHunter2!Secret#Value", "db-root-password"),
        ("--prefix=pbt", "pbt-Hunter2!Secret#Value"),
    ],
    ids=["short-name", "project", "prefix", "option", "prefixed-name"],
)
def test_value_like_arguments_are_not_repeated(sandbox, args):
    r = sandbox.run(SCRIPT, *args)
    assert r.rc == 2, r.err
    assert "Hunter2" not in r.err and "Hunter2" not in r.stdout.decode()
    assert "not shown" in r.err
    assert r.calls == []


def test_name_like_arguments_are_repeated(sandbox):
    r = sandbox.run(SCRIPT, "db-password")
    assert r.rc == 2 and "unknown short name 'db-password'" in r.err


# ---- describe and versions list do not read the value (review F3) ------------------------------


def test_prechecks_get_no_standard_input_and_are_quiet(sandbox, fake):
    value = fake(30)
    r = sandbox.run(SCRIPT, "db-root-password", stdin=value.encode())
    assert r.rc == 0, r.err
    assert r.precheck_stdin == [b"", b""]  # describe, versions list
    assert r.stored == [value.encode()]
    for call in r.calls:
        assert "--quiet" in call["argv"], call["argv"][:3]


# ---- Where the project and prefix come from (review F5, F6) ------------------------------------


@pytest.mark.parametrize(
    ("env", "project", "prefix"),
    [
        (
            {"CLOUDSDK_CORE_PROJECT": None, "GCP_PROJECT_ID": "setup-env-proj"},
            "setup-env-proj",
            "pbt",
        ),
        ({"GCP_PROJECT_ID": "setup-env-proj"}, "test-project-1", "pbt"),
        ({"SECRET_NAME_PREFIX": None, "PULSEBRIDGE_NAME_PREFIX": "pbx"}, "test-project-1", "pbx"),
        ({"PULSEBRIDGE_NAME_PREFIX": "pbx"}, "test-project-1", "pbt"),
    ],
    ids=[
        "gcp-project-id-fallback",
        "cloudsdk-wins",
        "pulsebridge-prefix-fallback",
        "secret-prefix-wins",
    ],
)
def test_project_and_prefix_fallbacks(sandbox, fake, env, project, prefix):
    r = sandbox.run(SCRIPT, "db-root-password", stdin=fake(20).encode(), env=sandbox.env(**env))
    assert r.rc == 0, r.err
    add = r.add_calls()[0]["argv"]
    assert add[3] == f"{prefix}-db-root-password" and f"--project={project}" in add


@pytest.mark.parametrize(
    "args",
    [("--project=",), ("--prefix=",), ("--project", ""), ("--prefix", "")],
    ids=["project=", "prefix=", "project-empty-word", "prefix-empty-word"],
)
def test_explicitly_empty_project_or_prefix_refused(sandbox, fake, args):
    r = sandbox.run(SCRIPT, *args, "db-root-password", stdin=fake(20).encode())
    assert r.rc == 2 and "empty value" in r.err and r.calls == []


# ---- Generated admin password (review F7) ------------------------------------------------------


def test_generated_admin_password_says_how_to_read_it_back(sandbox):
    r = sandbox.run(SCRIPT, "--generate", "openemr-admin-password")
    assert r.rc == 0, r.err
    # The generic pick-up text names the file for every secret; this sentence is specific.
    assert "admin password was not shown" in r.err
    assert "Reading a generated admin password back" in r.err
    other = sandbox.run(SCRIPT, "--generate", "openemr-db-password")
    assert "admin password was not shown" not in other.err


# ---- Terminal end of input (review F8) ---------------------------------------------------------


def test_ctrl_d_at_the_first_prompt_stores_nothing(sandbox):
    r, shown = sandbox.run_pty(SCRIPT, "db-root-password", answers=["\x04", "\x04"])
    assert r.rc == 2, shown
    assert b"no input (end of input before Enter)" in shown
    assert r.add_calls() == []
