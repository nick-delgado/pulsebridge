"""Secret scanning: the pre-commit hook and the history scan CI runs both catch a key.

The hook tests build a throwaway git repository in a temporary directory, install this
repository's .pre-commit-config.yaml as its git hook, stage a file and try `git commit`. The
history tests commit with hooks bypassed and run `make secret-scan` over the commits. The fake
key is generated at test time from random bytes and written only into a temporary directory:
it is not a real key, it is never committed here, and the real credentials in the environment
are never read.
"""

import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / ".pre-commit-config.yaml"

# The PEM armour is assembled at runtime so that this source file does not itself contain a
# private-key header, which the very scanners under test would (rightly) reject.
PEM_LABEL = "PRIVATE" + " KEY"


def fake_service_account_key() -> str:
    """Return a synthetic Google service account key file as JSON text.

    Same shape as a key downloaded from IAM: a JSON object whose "private_key" field holds a PEM
    private key. The key body is random bytes, so it is not a usable key, and every identifier
    is an obvious placeholder.
    """
    body = base64.b64encode(secrets.token_bytes(1200)).decode()
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    pem = f"-----BEGIN {PEM_LABEL}-----\n" + "\n".join(lines) + f"\n-----END {PEM_LABEL}-----\n"
    return json.dumps(
        {
            "type": "service_account",
            "project_id": "synthetic-test-project",
            "private_key_id": secrets.token_hex(20),
            "private_key": pem,
            "client_email": "synthetic-test@synthetic-test-project.iam.gserviceaccount.com",
            "client_id": "000000000000000000000",
            "token_uri": "https://oauth2.googleapis.com/token",
        },
        indent=2,
    )


def git_env(home: Path) -> dict[str, str]:
    # Strip GIT_* variables (set when this suite runs inside a git hook or worktree) so git acts
    # on the scratch repository only, and give it a synthetic identity and an empty global config.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=str(home / "gitconfig"),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
    )
    return env


@pytest.fixture
def scratch_repo(tmp_path: Path):
    """A new git repository with this repo's pre-commit config installed as its hook."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = git_env(tmp_path)

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=repo, env=env, capture_output=True, text=True, timeout=900)

    assert run("git", "init", "--quiet", "--initial-branch=main").returncode == 0
    shutil.copy(CONFIG, repo / ".pre-commit-config.yaml")
    installed = run(sys.executable, "-m", "pre_commit", "install")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    run("git", "add", ".pre-commit-config.yaml")
    return repo, run


def has_commit(run) -> bool:
    return run("git", "rev-parse", "--verify", "--quiet", "HEAD").returncode == 0


def test_commit_with_fake_service_account_key_is_rejected(scratch_repo) -> None:
    repo, run = scratch_repo
    # A name that .gitignore does not cover: the scanner must catch the content, not the name.
    key_file = repo / "config" / "backup.json"
    key_file.parent.mkdir()
    key_file.write_text(fake_service_account_key() + "\n")
    run("git", "add", "config/backup.json")

    result = run("git", "commit", "-m", "add config")
    output = result.stdout + result.stderr

    assert result.returncode != 0, output
    assert not has_commit(run), "the commit must not have been created"
    # Both scanners must flag it independently; one silently missing is a regression.
    # pre-commit prints "- hook id: <id>" only under a hook that failed.
    assert "- hook id: gitleaks\n" in output, output
    assert "- hook id: detect-private-key\n" in output, output
    # gitleaks runs with --redact: the key material must not be echoed into the hook output.
    key_body_sample = json.loads(key_file.read_text())["private_key"].splitlines()[1]
    assert key_body_sample not in output


def test_commit_without_secrets_is_accepted(scratch_repo) -> None:
    # Control case: the same hooks let an ordinary file through, so the rejection above is
    # caused by the key, not by a broken or always-failing hook setup.
    repo, run = scratch_repo
    settings = repo / "config" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"window_hours": 24, "unit": "/min"}, indent=2) + "\n")
    run("git", "add", "config/settings.json")

    result = run("git", "commit", "-m", "add settings")

    assert result.returncode == 0, result.stdout + result.stderr
    assert has_commit(run)


# --- committed history: `make secret-scan`, the command CI runs ------------------------------
#
# The hook above only sees staged changes. A commit made with `git commit --no-verify`, or from a
# clone where `make hooks` was never run, skips it entirely. These tests commit a synthetic key
# with hooks bypassed and check that the history scan CI runs still finds it.


def run_secret_scan(repo: Path, scan_range: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL"}}
    return subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-C",
            str(REPO_ROOT),
            "secret-scan",
            f"SECRET_SCAN_REPO={repo}",
            f"SECRET_SCAN_RANGE={scan_range}",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )


@pytest.fixture
def history_repo(tmp_path: Path):
    """A scratch repository with one clean base commit and no hooks installed."""
    repo = tmp_path / "history"
    repo.mkdir()
    env = git_env(tmp_path)

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=repo, env=env, capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()

    def commit_file(name: str, text: str) -> str:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        git("add", name)
        git("commit", "--quiet", "--no-verify", "-m", f"add {name}")
        return git("rev-parse", "HEAD")

    git("init", "--quiet", "--initial-branch=main")
    base = commit_file("README.md", "Synthetic test repository.\n")
    return repo, base, commit_file


def test_history_scan_finds_key_committed_with_hooks_bypassed(history_repo) -> None:
    # Pull request case: CI scans <base>..<head>, the commits the pull request adds.
    repo, base, commit_file = history_repo
    commit_file("docs/notes.md", "Meeting notes.\n")
    key = fake_service_account_key()
    head = commit_file("config/backup.json", key + "\n")

    result = run_secret_scan(repo, f"{base}..{head}")
    output = result.stdout + result.stderr

    assert result.returncode != 0, output
    assert "leaks found: 1" in output, output
    assert "config/backup.json" in output
    key_body_sample = json.loads(key)["private_key"].splitlines()[1]
    assert key_body_sample not in output  # --redact


def test_history_scan_passes_on_clean_commits(history_repo) -> None:
    repo, base, commit_file = history_repo
    commit_file("config/settings.json", json.dumps({"window_hours": 24}) + "\n")
    head = commit_file("docs/notes.md", "Meeting notes.\n")

    result = run_secret_scan(repo, f"{base}..{head}")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "no leaks found" in result.stdout + result.stderr


def test_full_history_scan_finds_key_in_an_older_commit(history_repo) -> None:
    # Push-to-main case: CI scans the whole history ("HEAD"), so a key removed in a later commit
    # is still found; deleting the file does not remove it from git history.
    repo, _base, commit_file = history_repo
    commit_file("config/backup.json", fake_service_account_key() + "\n")
    commit_file("config/backup.json", "{}\n")  # "removes" the key in a later commit

    result = run_secret_scan(repo, "HEAD")

    assert result.returncode != 0, result.stdout + result.stderr
    assert "leaks found: 1" in result.stdout + result.stderr
