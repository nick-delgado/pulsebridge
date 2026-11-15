"""Checks for the top-level documentation: README.md and docs/setup.md (task T28).

These two files are what a stranger reads first, so they must not rot silently. Each rule below
is something a reader would trip over:

1. Every relative link and image resolves, and every `#anchor` names a heading that exists (in
   the same file or in the linked Markdown file), using GitHub's heading-slug rules.
2. Every repository path written in code (inline `code` or a fenced block) exists, apart from an
   explicit allow-list of paths that other tasks are still building or that a deploy generates.
3. Every `make <target>` written in code names a target the Makefile defines.
4. Nothing author-specific: e-mail addresses, host names, project IDs in commands and resource
   paths, project numbers and bucket names must be placeholders (the same allow-list approach
   as infra/envs/_stack/tests/check_env_roots.py), and GitHub links may only point at the
   third-party projects listed below.
5. Every fenced code block carries a language tag (`bash`, `text`, ...), so shell commands are
   marked as such and render correctly.
6. The README has a Mermaid architecture diagram naming each component of the data flows.
7. Images: each file under docs/images/ is used, and each is described as mock or synthetic
   (CLAUDE.md: images never show real health data).
8. The setup guide carries what the acceptance criteria require it to say (T25, T28).

Out of scope, on purpose: repository paths and `make` targets written in plain prose (outside
code) are not checked, because prose names things loosely ("the `web/` directory", "make the
change"); every path a reader is meant to type or open is written as code or as a link. Shortcut
reference links (`[name]` with no second bracket) are not checked either; this file's two docs
use inline links, and full or collapsed reference links (`[text][ref]`, `[ref][]`) are checked.

Pure text checks: no network, no credentials.
"""

from __future__ import annotations

import glob
import re
import warnings
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
README = REPO / "README.md"
SETUP = REPO / "docs" / "setup.md"
# Exactly these two files. A test below asserts it, so an emptied tuple cannot silently skip the
# parametrized tests.
DOCS = (README, SETUP)
IMAGES = REPO / "docs" / "images"

# ---- Markdown helpers ----------------------------------------------------------------------------

FENCE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>[^\n`]*)$")


def fenced_blocks(text: str) -> list[tuple[int, str, str]]:
    """(line number of the opening fence, info string, body) for each fenced code block."""
    blocks = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        m = FENCE.match(lines[i])
        if m:
            fence = m.group("fence")
            start = i
            body = []
            i += 1
            while i < len(lines):
                close = FENCE.match(lines[i])
                if (
                    close
                    and close.group("fence")[0] == fence[0]
                    and len(close.group("fence")) >= len(fence)
                    and not close.group("info").strip()
                ):
                    break
                body.append(lines[i])
                i += 1
            else:
                pytest.fail(f"unclosed code fence opened on line {start + 1}")
            blocks.append((start + 1, m.group("info").strip(), "\n".join(body)))
        i += 1
    return blocks


def prose(text: str) -> str:
    """The text with fenced blocks blanked out (line count kept), for links and headings."""
    lines = text.split("\n")
    for start, _info, body in fenced_blocks(text):
        for k in range(start - 1, start + body.count("\n") + 2):
            if k < len(lines):
                lines[k] = ""
    return "\n".join(lines)


def inline_code(text: str) -> list[str]:
    """Contents of inline `code` spans in the prose (single or double backticks). A span may
    wrap onto the next line, as Markdown allows inside a paragraph; the line break reads as a
    space."""
    span = re.compile(r"(`+)((?:(?!\n[ \t]*\n).)+?)\1", re.S)
    return [re.sub(r"\s*\n\s*", " ", m.group(2)) for m in span.finditer(prose(text))]


def code_text(text: str) -> str:
    """Everything a reader would copy: inline code spans and fenced block bodies."""
    return "\n".join(inline_code(text) + [body for _l, _i, body in fenced_blocks(text)])


def github_slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation removed, spaces to hyphens."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # [text](url) -> text
    text = text.replace("`", "").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)  # keeps letters, digits, _, -, space
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    """Every anchor a Markdown file defines: heading slugs (with -1, -2 for repeats) and
    explicit <a name="..."> / id="..." anchors."""
    text = path.read_text(encoding="utf-8")
    seen: dict[str, int] = {}
    found = set()
    for m in re.finditer(r"^#{1,6}[ \t]+(.+?)[ \t#]*$", prose(text), re.M):
        slug = github_slug(m.group(1))
        n = seen.get(slug, 0)
        found.add(slug if n == 0 else f"{slug}-{n}")
        seen[slug] = n + 1
    found.update(re.findall(r"<a\s+(?:name|id)=\"([^\"]+)\"", text))
    return found


LINK = re.compile(r"!?\[(?P<text>[^\]]*)\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")


def links(path: Path) -> list[tuple[str, str]]:
    """(link text, target) of every Markdown link and image outside code."""
    text = prose(path.read_text(encoding="utf-8"))
    text = re.sub(r"(`+)(?:(?!\n[ \t]*\n).)+?\1", "", text, flags=re.S)  # code is not a link
    found = [(m.group("text"), m.group("target")) for m in LINK.finditer(text)]
    # Reference-style links: [text][ref] and [ref][] use a definition "[ref]: target".
    definitions = {
        m.group(1).lower(): m.group(2)
        for m in re.finditer(
            r"^[ ]{0,3}\[([^\]]+)\]:[ \t]*<?(\S+?)>?(?:[ \t]+\"[^\"]*\")?[ \t]*$", text, re.M
        )
    }
    for m in re.finditer(r"\[([^\]]+)\]\[([^\]]*)\]", text):
        ref = (m.group(2) or m.group(1)).lower()
        if ref not in definitions:
            found.append((m.group(1), f"#undefined-reference:{ref}"))
    # Each definition's target is checked once, under its reference name.
    found.extend((ref, target) for ref, target in definitions.items())
    return found


# ---- 1. links and anchors ------------------------------------------------------------------------


def link_problems(doc: Path) -> list[str]:
    problems = []
    for _text, target in links(doc):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target):  # https:, mailto:, ...
            continue
        if target.startswith("#undefined-reference:"):
            problems.append(f"reference link [{target.split(':', 1)[1]}] has no definition")
            continue
        path_part, _, anchor = target.partition("#")
        dest = (doc.parent / path_part).resolve() if path_part else doc
        if not dest.exists():
            problems.append(f"{target}: {path_part} does not exist")
            continue
        if path_part and REPO not in dest.parents and dest != REPO:
            problems.append(f"{target}: points outside the repository")
            continue
        if anchor:
            if dest.is_dir() or dest.suffix != ".md":
                problems.append(f"{target}: an anchor on something that is not a Markdown file")
            elif anchor not in anchors(dest):
                problems.append(f"{target}: no heading with anchor #{anchor} in {dest.name}")
    return problems


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_relative_link_and_anchor_resolves(doc: Path) -> None:
    assert links(doc), f"{doc.name} has no links at all; the parser is broken"
    assert link_problems(doc) == []


def test_the_checked_documents_are_the_two_top_level_docs() -> None:
    assert DOCS == (README, SETUP)
    assert all(doc.is_file() for doc in DOCS)


def test_reference_links_are_checked(tmp_path: Path) -> None:
    """The reference-link parser's control: a defined reference resolves, an undefined one and a
    definition pointing nowhere are reported."""
    doc = tmp_path / "x.md"
    doc.write_text("# Top\n\nSee [ok][a], [b][] and [gone][c].\n\n[a]: #top\n[b]: missing.md\n")
    assert dict(links(doc))["a"] == "#top"
    problems = link_problems(doc)
    assert len(problems) == 2, problems
    assert any("[c] has no definition" in p for p in problems)
    assert any("missing.md does not exist" in p for p in problems)


def test_slug_rules_match_github() -> None:
    # The cases the guide relies on: digits and dots, apostrophes, parentheses, backticks.
    assert github_slug("3.8 The setup step: write the Patient (exactly one of the two)") == (
        "38-the-setup-step-write-the-patient-exactly-one-of-the-two"
    )
    assert github_slug("5. Secret versions (deployer; T27's scripts)") == (
        "5-secret-versions-deployer-t27s-scripts"
    )
    assert github_slug("`terraform destroy`, and what it leaves behind") == (
        "terraform-destroy-and-what-it-leaves-behind"
    )


# ---- 2. repository paths -------------------------------------------------------------------------

# Paths a doc may name although they do not exist in this checkout.
PENDING_PATHS = {
    # Being built by T27 (secret version scripts).
    "scripts/secrets/": "T27",
    "scripts/secrets/README.md": "T27",
    "scripts/secrets/set-secret.sh": "T27",
    "scripts/secrets/set-reviewer-password.sh": "T27",
    # Planned in T29 (end-to-end smoke test).
    "tests/e2e/": "T29",
}
GENERATED_PATHS = {
    # Written by the page deploy from Terraform's firebase_json output; git-ignored.
    "web/firebase.json",
    # The OpenEMR chart screenshot, captured on the first real deploy (README placeholder).
    "docs/images/openemr-chart-synthetic.png",
}

ROOT_FILES = (
    "Makefile",
    "cloudbuild.yaml",
    "pyproject.toml",
    "CLAUDE.md",
    "pulsebridge-projectbrief.md",
)
PATH = re.compile(
    r"(?<![\w./<$-])"
    r"((?:\.github|infra|services|libs|scripts|web|docs|tests)/[A-Za-z0-9_.*/-]*"
    r"|(?:" + "|".join(re.escape(f) for f in ROOT_FILES) + r")\b)"
)


def mentioned_paths(doc: Path) -> set[str]:
    found = set()
    for chunk in code_text(doc.read_text(encoding="utf-8")).split("\n"):
        for m in PATH.finditer(chunk):
            end = m.end()
            if end < len(chunk) and chunk[end] == "<":  # infra/envs/<env>: a placeholder
                continue
            found.add(m.group(1).rstrip(".,:;"))
    return found


def path_exists(rel: str) -> bool:
    if "*" in rel:
        return bool(glob.glob(str(REPO / rel)))
    return (REPO / rel).exists()


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_repository_path_mentioned_exists(doc: Path) -> None:
    paths = mentioned_paths(doc)
    assert paths, f"{doc.name}: no repository paths found; the scanner is broken"
    missing = sorted(
        p
        for p in paths
        if not path_exists(p) and p not in PENDING_PATHS and p not in GENERATED_PATHS
    )
    assert missing == [], f"{doc.name} names paths that do not exist: {missing}"


def test_pending_paths_are_still_pending() -> None:
    """The allow-list tightens as tasks merge: once a pending path exists it should be removed
    from PENDING_PATHS, so the normal existence check covers it. A warning, not a failure, so the
    merge of T26 or T27 does not break on this file."""
    arrived = sorted(p for p in PENDING_PATHS if path_exists(p))
    for p in arrived:
        warnings.warn(
            f"{p} ({PENDING_PATHS[p]}) now exists: remove it from PENDING_PATHS", stacklevel=1
        )


# ---- 3. make targets -----------------------------------------------------------------------------


def makefile_targets() -> set[str]:
    text = (REPO / "Makefile").read_text(encoding="utf-8")
    return set(re.findall(r"^([a-z][a-z0-9-]*):", text, re.M))


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_make_target_mentioned_exists(doc: Path) -> None:
    targets = set(
        re.findall(r"\bmake ([a-z][a-z0-9-]*)", code_text(doc.read_text(encoding="utf-8")))
    )
    if doc == README:
        assert targets, "README names no make targets; the scanner is broken"
    unknown = sorted(targets - makefile_targets())
    assert unknown == [], f"{doc.name} names make targets the Makefile lacks: {unknown}"


# ---- 4. nothing author-specific ------------------------------------------------------------------

ALLOWED_DOMAIN_SUFFIXES = (
    "example.com",
    "example.org",
    "googleapis.com",
    "google.com",
    "run.app",
    "web.app",
    "firebaseapp.com",
    "pkg.dev",
    "gcr.io",
    "gserviceaccount.com",
    "github.com",
    "githubusercontent.com",
    "terraform.io",
    "hashicorp.com",
    "hl7.org",
    "loinc.org",
    "unitsofmeasure.org",
)
# Placeholder project IDs (as in check_env_roots.py).
ALLOWED_PROJECTS = {
    "example-project",
    "your-project-id",
    "your-dev-project-id",
    "your-prod-project-id",
}
ALLOWED_NUMBERS = {"100000000001", "123456789012", "000000000000"}
# Third-party projects a doc may link to on GitHub. The project's own repository is never linked
# by owner name: a clone's owner is whoever cloned it.
ALLOWED_GITHUB_OWNERS = {"GoogleCloudPlatform", "google-github-actions", "openemr", "gitleaks"}

EMAIL = re.compile(r"[A-Za-z0-9._%+${}<>-]+@[A-Za-z0-9.${}<>-]+\.[a-z]{2,}")
HOST = re.compile(r"(?<![\w.@-])(?:[a-z0-9][a-z0-9-]*\.)+(?:com|org|net|io|app|dev|cloud|me|ai)\b")
PROJECT_PATH = re.compile(r"projects/([a-z][a-z0-9-]{4,28}[a-z0-9])\b")
PROJECT_FLAG = re.compile(r"--project[= ]\"?([a-z][a-z0-9-]{4,28}[a-z0-9])\b")
NUMBER = re.compile(r"(?<![\w.:-])\d{12}(?![\w-]|\.\d)")
BUCKET = re.compile(r"gs://([a-z0-9][a-z0-9._-]{2,})")
GITHUB_OWNER = re.compile(r"(?<![\w.])github\.com/([A-Za-z0-9-]+)/")
PROJECT_ID = r"([a-z][a-z0-9-]{4,28}[a-z0-9])"
# Other places a project ID shows up: a registry path, the gcloud default, the usual variables.
PROJECT_ELSEWHERE = (
    (re.compile(r"docker\.pkg\.dev/" + PROJECT_ID + r"/"), "registry path"),
    (re.compile(r"gcloud config set project[ \t]+" + PROJECT_ID + r"\b"), "gcloud config"),
    (
        re.compile(
            r"\b(?:CLOUDSDK_CORE_PROJECT|GOOGLE_CLOUD_PROJECT|GCP_PROJECT_ID)=[\"']?"
            + PROJECT_ID
            + r"\b"
        ),
        "variable assignment",
    ),
)
# Hosts whose first label is a project or site ID: only these exact hosts may appear.
PROJECT_HOST_SUFFIXES = ("web.app", "firebaseapp.com", "run.app")
ALLOWED_PROJECT_HOSTS = {"example-project.web.app", "example-project.firebaseapp.com"}
BILLING_ACCOUNT = re.compile(r"\b[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}\b")


def identity_problems(text: str) -> list[str]:
    problems = []
    for m in EMAIL.finditer(text):
        addr = m.group(0)
        domain = addr.split("@", 1)[1]
        if not (any(c in addr for c in "<${") or domain in ("example.com", "example.org")):
            problems.append(f"e-mail address {addr!r} is not a placeholder")
    for m in HOST.finditer(text):
        host = m.group(0)
        if not any(host == s or host.endswith("." + s) for s in ALLOWED_DOMAIN_SUFFIXES):
            problems.append(f"host {host!r} is not a placeholder or a known tool host")
        elif (
            any(host.endswith("." + s) for s in PROJECT_HOST_SUFFIXES)
            and host not in ALLOWED_PROJECT_HOSTS
        ):
            problems.append(f"host {host!r} names a project or site that is not a placeholder")
    for regex, what in (
        (PROJECT_PATH, "resource path"),
        (PROJECT_FLAG, "--project flag"),
        *PROJECT_ELSEWHERE,
    ):
        for m in regex.finditer(text):
            if m.group(1) not in ALLOWED_PROJECTS:
                problems.append(f"project {m.group(1)!r} in a {what} is not a placeholder")
    for m in NUMBER.finditer(text):
        if m.group(0) not in ALLOWED_NUMBERS:
            problems.append(f"12-digit number {m.group(0)} looks like a project number")
    for m in BUCKET.finditer(text):
        problems.append(f"bucket gs://{m.group(1)} is not a <placeholder>")
    for m in GITHUB_OWNER.finditer(text):
        if m.group(1) not in ALLOWED_GITHUB_OWNERS:
            problems.append(f"GitHub link to owner {m.group(1)!r}")
    for m in BILLING_ACCOUNT.finditer(text):
        if m.group(0) != "000000-AAAAAA-000000":
            problems.append(f"billing account {m.group(0)} is not the placeholder")
    return problems


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_no_author_specific_identifiers(doc: Path) -> None:
    assert identity_problems(doc.read_text(encoding="utf-8")) == []


def test_identity_scan_catches_the_obvious_cases() -> None:
    """The scan's own control: invented identifiers of each kind are reported."""
    sample = "\n".join(
        [
            "mail someone@personal-site.net",
            "see https://demo.someones-domain.com/",
            "projects/acme-health-42/locations",
            "gcloud run jobs list --project acme-health-42",
            "number 314159265358",
            "gs://acme-health-42_cloudbuild",
            "https://github.com/some-person/pulsebridge",
            "billing 0A1B2C-3D4E5F-6A7B8C",
            "project number 314159265359.",
            "https://acme-health-42.web.app",
            "us-central1-docker.pkg.dev/acme-health-42/pulsebridge-services/x",
            "gcloud config set project acme-health-42",
            "CLOUDSDK_CORE_PROJECT=acme-health-42 make test",
        ]
    )
    assert len(identity_problems(sample)) == 13, identity_problems(sample)
    clean = (
        "you@example.com <project-id> gs://<project-id>_cloudbuild https://<site>.web.app "
        "https://example-project.web.app CLOUDSDK_CORE_PROJECT=<project-id> "
        "https://api.github.com/repos/<owner>/<name> version 1.2.3"
    )
    assert identity_problems(clean) == []


# ---- 5. tagged code blocks -----------------------------------------------------------------------

LANGUAGES = {"bash", "text", "hcl", "json", "yaml", "python", "mermaid"}


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_fenced_block_is_tagged(doc: Path) -> None:
    blocks = fenced_blocks(doc.read_text(encoding="utf-8"))
    assert blocks, f"{doc.name} has no code blocks; the parser is broken"
    untagged = [line for line, info, _ in blocks if info.split()[:1] == []]
    unknown = [
        (line, info) for line, info, _ in blocks if info and info.split()[0] not in LANGUAGES
    ]
    assert untagged == [], f"{doc.name}: untagged code fences on lines {untagged}"
    assert unknown == [], f"{doc.name}: unexpected languages {unknown}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_shell_commands_are_in_bash_blocks(doc: Path) -> None:
    """A block that runs gcloud, terraform, npm, make or cd must be tagged bash."""
    command = re.compile(
        # A command word followed by a space and an argument (so a Mermaid node named "gh" or a
        # directory listing does not count), or a variable read, an editor call or a script.
        r"^\s*\(?\s*(?:(?:gcloud|terraform|npm|npx|make|cd|cp|source|chmod|export|git|gh)"
        r"[ \t]+[\w\"'$~./-]|IFS=|\"\$\{EDITOR|(?:\./)?scripts/[\w/-]+\.sh)",
        re.M,
    )
    wrong = [
        (line, info)
        for line, info, body in fenced_blocks(doc.read_text(encoding="utf-8"))
        # Mermaid node ids (gh, cd, ...) are not commands.
        if info != "mermaid" and command.search(body) and info.split()[0] != "bash"
    ]
    assert wrong == [], f"{doc.name}: shell commands in non-bash blocks at {wrong}"


# ---- 6. the architecture diagram -----------------------------------------------------------------

# Every component of the brief's data flows, as the diagram must name it.
DIAGRAM_NODES = (
    "Pixel Watch",
    "Google Health API",
    "health-sync",
    "Cloud Scheduler",
    "cuff-sim",
    "MLLP adapter",
    "HL7v2 store",
    "Pub/Sub",
    "v2-to-fhir",
    "FHIR R4 store",
    "Audit FHIR store",
    "Firebase Hosting",
    "fhir-proxy",
    "OpenEMR",
    "Google OAuth",
    "seed job",
)
# The data flows, as edges "from --> to" (the label between them is free text).
DIAGRAM_EDGES = (
    ("gh", "hs"),
    ("hs", "fhir"),
    ("cuff", "mllp"),
    ("mllp", "hl7"),
    ("hl7", "ps"),
    ("ps", "v2f"),
    ("v2f", "fhir"),
    ("browser", "hosting"),
    ("hosting", "proxy"),
    ("proxy", "fhir"),
    ("proxy", "cuff"),
    ("proxy", "emr"),
    ("proxy", "hs"),
    ("sched", "hs"),
    ("cuff", "audit"),
    ("v2f", "hl7"),
    ("proxy", "audit"),
    ("proxy", "hl7"),
    ("goauth", "hs"),
    ("sched", "seed"),
    ("seed", "fhir"),
)


def mermaid_blocks() -> list[str]:
    return [
        body
        for _l, info, body in fenced_blocks(README.read_text(encoding="utf-8"))
        if info == "mermaid"
    ]


def test_readme_has_a_mermaid_architecture_diagram() -> None:
    blocks = mermaid_blocks()
    assert len(blocks) == 1, "README needs exactly one Mermaid diagram"
    diagram = blocks[0]
    assert re.match(r"\s*flowchart\b", diagram)
    missing = [n for n in DIAGRAM_NODES if n not in diagram]
    assert missing == [], f"the diagram lacks {missing}"
    edges = set()
    for line in diagram.split("\n"):
        m = re.match(
            r"\s*(\w+)(?:\[[^\]]*\])?\s*(?:-->|-\.->)(?:\|[^|]*\|)?\s*(\w+)",
            line,
        )
        if m:
            edges.add((m.group(1), m.group(2)))
    absent = [e for e in DIAGRAM_EDGES if e not in edges]
    assert absent == [], f"the diagram lacks the flows {absent}"


# ---- 7. images -----------------------------------------------------------------------------------


def image_links() -> list[tuple[Path, str, str]]:
    found = []
    for doc in DOCS:
        text = prose(doc.read_text(encoding="utf-8"))
        for m in re.finditer(r"!\[(?P<alt>[^\]]*)\]\((?P<target>[^)\s]+)\)", text):
            found.append((doc, m.group("alt"), m.group("target")))
    return found


def test_every_image_is_used_and_described_as_mock_or_synthetic() -> None:
    files = sorted(p for p in IMAGES.iterdir() if p.is_file())
    assert files, "docs/images/ is empty"
    used = {(doc.parent / target).resolve() for doc, _alt, target in image_links()}
    unused = [p.name for p in files if p.resolve() not in used]
    assert unused == [], f"images not referenced from README.md or docs/setup.md: {unused}"
    for p in files:
        assert re.search(r"mock|synthetic", p.name), f"{p.name}: name must say mock or synthetic"
    for doc, alt, target in image_links():
        assert re.search(r"\b(mock|synthetic)\b", alt, re.I), (
            f"{doc.name}: image {target} must be described as mock or synthetic in its alt text"
        )


# ---- 8. what the guide must say ------------------------------------------------------------------

# (pattern, why). Case-insensitive regular expressions over docs/setup.md.
SETUP_MUST_SAY = (
    (r"Desktop app", "T28 AC: the OAuth client type"),
    (r"add yourself as a test user", "T28 AC: test users"),
    (r"only listed test users", "T28 AC: Testing status admits test users only"),
    (r"expires? after 7 days", "T28 AC: the refresh token in Testing"),
    (r"In production", "T28 AC: how to leave Testing"),
    (r"Publish app", "T28 AC: the console action that publishes"),
    (r"shell history", "T28 AC: keep demographics out of it"),
    (r"run the connect script again", "T28 AC: what to do when the token expires"),
    (r"Never run the Google Health connect script against a synthetic patient ID", "T25 AC"),
    (r"pb-google-health-client-id", "T28 AC: secrets named as module identity names them"),
    (r"pb-google-health-refresh-token", "T28 AC: secrets named as module identity names them"),
    (r"know \*\*only\s+these names\*\*", "T28 AC: other names are not read"),
    (r"one to two hours", "T25 AC: the serverless subnet's deletion delay"),
    (r"last page", "T25 AC: the default Hosting site stays up after destroy"),
    (
        r"-replace='module\.stack\.module\.edge_vm\.google_compute_instance\.this'",
        "T25 AC: patching",
    ),
    (r"not yet verified on a real project", "honest status"),
    (r"e2-micro", "cost: the always-on VM"),
    (r"free tier", "cost: the free-tier caveat"),
)


def test_setup_guide_carries_what_the_acceptance_criteria_require() -> None:
    text = SETUP.read_text(encoding="utf-8")
    missing = [
        why + f" ({pattern})"
        for pattern, why in SETUP_MUST_SAY
        if not re.search(pattern, text, re.I)
    ]
    assert missing == []


def test_setup_guide_steps_link_to_the_runbook_steps_in_order() -> None:
    """Each deploy step links to its runbook step, in the runbook's order (1 to 14)."""
    runbook = REPO / "infra" / "envs" / "README.md"
    order = []
    for _text, target in links(SETUP):
        path_part, _, anchor = target.partition("#")
        if path_part.endswith("infra/envs/README.md") and re.match(r"\d+-", anchor):
            n = int(anchor.split("-", 1)[0])
            if not order or order[-1] != n:
                order.append(n)
    assert order, "the guide links to no runbook step"
    steps = sorted(
        int(s) for s in re.findall(r"^### (\d+)\. ", runbook.read_text(encoding="utf-8"), re.M)
    )
    assert sorted(set(order)) == steps, (
        f"runbook steps not linked: {sorted(set(steps) - set(order))}"
    )
    assert order == sorted(order), f"runbook steps linked out of order: {order}"
