"""Static checks over the scripts: things a run cannot show.

- shellcheck is clean (also a pre-commit hook; here it runs only if shellcheck is installed).
- No construct that bash 3.2 (macOS's /bin/bash) lacks. bash 3.2 itself is not available to the
  test suite, so this is a list of the usual offenders, not a proof.
- No $RANDOM, no here-string (bash writes those to a temporary file), no `set -x`, no export of
  the value variables, and the value is only ever expanded where the README says it is.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
REPO = SCRIPTS.parent.parent
FILES = sorted(SCRIPTS.glob("*.sh"))


def code_lines(path: Path):
    """(number, line) for lines that are not comments."""
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.lstrip().startswith("#"):
            yield n, line


def test_the_three_files_exist():
    assert [p.name for p in FILES] == ["lib.sh", "set-reviewer-password.sh", "set-secret.sh"]


def test_shellcheck_clean():
    shellcheck = shutil.which("shellcheck")
    if not shellcheck:
        pytest.skip("shellcheck not installed (the pre-commit hook runs it)")
    proc = subprocess.run(
        [shellcheck, "-x", "-S", "style", *[str(p.relative_to(REPO)) for p in FILES]],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


BASH4_ONLY = {
    r"\bdeclare\s+-[a-zA-Z]*A": "associative arrays (bash 4)",
    r"\blocal\s+-[a-zA-Z]*n\b|\bdeclare\s+-[a-zA-Z]*n\b": "namerefs (bash 4.3)",
    r"\b(mapfile|readarray)\b": "mapfile (bash 4)",
    r"\$\{[A-Za-z_][A-Za-z0-9_]*(,,?|\^\^?)\}": "case conversion (bash 4)",
    r"\[\[\s+-v\b": "[[ -v ]] (bash 4.2)",
    r"\|&|&>>": "|& or &>> (bash 4)",
    r";;&|;&": "case fall-through (bash 4)",
    r"\bwait\s+-n\b": "wait -n (bash 4.3)",
    r"\bread\b[^\n]*\s-N\b|\bread\b[^\n]*\s-i\b": "read -N / -i (bash 4)",
    r"\$\{[A-Za-z_][A-Za-z0-9_]*@[QEPAa]\}": "${var@Q} (bash 4.4)",
    r"\bdeclare\s+-g\b": "declare -g (bash 4.2)",
    r"\bcoproc\b": "coproc (bash 4)",
}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_bash4_constructs(path):
    for n, line in code_lines(path):
        for pattern, what in BASH4_ONLY.items():
            assert not re.search(pattern, line), f"{path.name}:{n}: {what}: {line.strip()}"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_random_no_herestring_no_xtrace_no_export_of_value(path):
    for n, line in code_lines(path):
        where = f"{path.name}:{n}: {line.strip()}"
        assert "RANDOM" not in line, where
        assert "<<<" not in line, where
        assert not re.search(r"\bset\s+-[a-z]*x", line), where
        assert not re.search(r"\b(export|declare\s+-x)\b[^\n]*\bsv_(value|confirm)\b", line), where


def test_value_expanded_only_where_intended():
    """Every expansion of $sv_value / $sv_confirm, reviewed by hand once and pinned here: the
    printf builtin into gcloud's stdin, the printed generated password, and checks that do not
    output it. A new expansion fails this test and has to be looked at."""
    allowed = [
        r'builtin printf \'%s\' "\$sv_value" \| sv_gcloud secrets versions add',
        r'builtin printf \'%s\\n\' "\$sv_value"$',
        r'sv_value="\$\{sv_value%\$\'\\n\'\}"',
        r'if \[ "\$sv_value" != "\$sv_confirm" \]; then',
        r'if \[ -z "\$sv_value" \]; then',
        r'case "\$sv_value" in',
        r'if \[ "\$\{#sv_value\}" -(ne|lt|gt) ',
        r"sv_die .*\$\{#sv_value\}",
    ]
    for path in FILES:
        for n, line in code_lines(path):
            if re.search(r"\$\{?#?sv_(value|confirm)", line):
                assert any(re.search(a, line.strip()) for a in allowed), (
                    f"{path.name}:{n}: unreviewed expansion of the value: {line.strip()}"
                )


def test_pre_commit_runs_shellcheck_on_these_scripts():
    config = (REPO / ".pre-commit-config.yaml").read_text()
    assert "shellcheck-py/shellcheck-py" in config
    block = config[config.index("shellcheck-py/shellcheck-py") :]
    assert "scripts/secrets/" in block.split("- repo:")[0]


def test_generation_uses_only_tr_over_dev_urandom():
    """The one place a value is generated reads /dev/urandom through `tr -dc` and nothing else:
    no $RANDOM, no other device, no date or PID, no openssl, shuf or od fallback."""
    lib = (SCRIPTS / "lib.sh").read_text()
    body = lib[lib.index("sv_generate() {") :]
    body = body[: body.index("\n}\n")]
    sources = [n for n, line in code_lines_of(body) if "sv_value=" in line]
    assert len(sources) == 1, sources
    line = next(line for _, line in code_lines_of(body) if "sv_value=" in line)
    assert line.strip() == (
        'sv_value="$(LC_ALL=C tr -dc "$SV_GENERATE_ALPHABET" </dev/urandom 2>/dev/null'
        ' | head -c "$length")" || true'
    )
    for path in FILES:
        text = path.read_text()
        for word in ("/dev/random", "openssl", "shuf ", "od -", "$$", "date +", "SRANDOM"):
            assert word not in text, f"{path.name}: {word}"


def code_lines_of(text: str):
    for n, line in enumerate(text.splitlines(), 1):
        if not line.lstrip().startswith("#"):
            yield n, line
