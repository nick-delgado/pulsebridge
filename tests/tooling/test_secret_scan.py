"""The pre-commit secret scan blocks a commit that stages a service account key.

Each test builds a throwaway git repository in a temporary directory, installs this repository's
.pre-commit-config.yaml as its git hook, stages a file and tries `git commit`. The fake key is
generated at test time from random bytes and written only into that temporary directory: it is
not a real key, it is never committed here, and the real credentials in the environment are
never read.
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
