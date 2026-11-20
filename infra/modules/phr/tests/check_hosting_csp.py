"""The Content-Security-Policy Firebase Hosting sends must be the one the page was built and
tested under.

`make tf-test` runs this file (every tests/*.py). It reads three committed files and fails if
they disagree:

- infra/modules/phr/hosting/security-headers.json: the headers hosting.tf puts in firebase.json;
- web/vite.config.ts, `contentSecurityPolicy()`: the <meta> policy the build adds, for the
  same-origin build (VITE_FHIR_PROXY_URL unset, so `connect-src 'self'`), which is the build the
  Hosting rewrite serves;
- web/scripts/check-bundle.mjs: the directives the build check requires exactly.

The header must have every directive of the <meta> policy with the same value, plus
`frame-ancestors 'none'` (a <meta> policy cannot set it; web/README.md, "Security basics"), and
nothing else. No source may be `'unsafe-inline'`, `'unsafe-eval'`, `'unsafe-hashes'`,
`'strict-dynamic'`, a wildcard, a bare scheme (`data:`, `http:`, `https:`, `blob:`) or an
`http://` URL.

The only external sources are reCAPTCHA's (D16, T44), exactly as Google's CSP guidance lists them
for a page that does not use nonces (Fraud Defense FAQ, docs.cloud.google.com/recaptcha/docs/faq,
read 19 November 2026: "Alternatively, add the following values to the directives"):
script-src www.google.com/recaptcha/ and www.gstatic.com/recaptcha/;
frame-src www.google.com/recaptcha/ and
recaptcha.google.com/recaptcha/; connect-src www.google.com/recaptcha/. Each of those
directives must hold exactly these https:// sources (path-restricted, so the rest of google.com
stays blocked), and no other directive may hold any.
The other security headers must have their documented values (REQUIRED_HEADERS).

The page's two policies are not parsed by shape: the declarations that make them
(`contentSecurityPolicy()` and the constants it uses; check-bundle.mjs's `expected` object) are
cut out of the files and RUN with node, so the check compares values, however T45 writes the
code. Only TypeScript annotations are erased first (see _ERASE).

Usage: python3 infra/modules/phr/tests/check_hosting_csp.py   (standard library and node)
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
REPO = MODULE.parents[2]
HEADERS = MODULE / "hosting" / "security-headers.json"
VITE_CONFIG = REPO / "web" / "vite.config.ts"
CHECK_BUNDLE = REPO / "web" / "scripts" / "check-bundle.mjs"

# Only a header can set these; the <meta> policy does not have them.
HEADER_ONLY = {"frame-ancestors": "'none'"}
# The other security headers (README, "The headers"). HSTS without includeSubDomains: on a custom
# domain (T30) it must not pin the domain's other hosts.
REQUIRED_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Strict-Transport-Security": "max-age=31536000",
}
FORBIDDEN_SOURCES = re.compile(
    r"'unsafe-inline'|'unsafe-eval'|'unsafe-hashes'|'strict-dynamic'|\*|http://"
    r"|(^|\s)(data|https?|blob|wss?|filesystem|mediastream):(\s|$)",
)
# reCAPTCHA's sources per directive (the module docstring cites Google's guidance). The page loads
# https://www.google.com/recaptcha/enterprise.js, which loads its code from www.gstatic.com and
# runs the check in an iframe from www.google.com (or recaptcha.google.com).
RECAPTCHA_SOURCES = {
    "script-src": {"https://www.google.com/recaptcha/", "https://www.gstatic.com/recaptcha/"},
    "frame-src": {"https://www.google.com/recaptcha/", "https://recaptcha.google.com/recaptcha/"},
    "connect-src": {"https://www.google.com/recaptcha/"},
}


def parse_policy(policy: str) -> dict[str, str]:
    directives: dict[str, str] = {}
    for part in policy.split(";"):
        words = part.split()
        if not words:
            continue
        name, values = words[0].lower(), " ".join(words[1:])
        if name in directives:
            raise AssertionError(f"directive {name} appears twice")
        directives[name] = values
    return directives


def _block(text: str, start: str, where: Path) -> str:
    """The source text from the regex `start` to the brace that closes the first `{` after it
    (plus a trailing `;`). Braces inside the page's strings are balanced (template literals'
    `${...}`), so counting them is enough for these two files."""
    found = re.search(start, text, re.M)
    if found is None:
        raise AssertionError(f"{where.name}: {start!r} not found; the check reads that code")
    i = text.index("{", found.end() - 1 if text[found.end() - 1] == "{" else found.end())
    depth = 0
    for k in range(i, len(text)):
        depth += {"{": 1, "}": -1}.get(text[k], 0)
        if depth == 0:
            end = k + 1
            return text[found.start() : end + (1 if text[end : end + 1] == ";" else 0)]
    raise AssertionError(f"{where.name}: unbalanced braces after {start!r}")


# TypeScript that node cannot run as JavaScript, as it appears in these two declarations:
# `as const`, `export`, parameter types (`mode: string`, `extra: readonly string[]`, `a: string |
# undefined`) and return types (`): string {`). Erasing them leaves plain JavaScript; the page's
# own logic is then RUN, not re-implemented here, so any way T45 composes the policy is fine.
_TYPE = r"(?:readonly\s+)?[A-Za-z_]\w*(?:\[\])?(?:\s*\|\s*[A-Za-z_]\w*(?:\[\])?)*"
_ERASE = (
    (re.compile(r"\s+as\s+const\b"), ""),
    (re.compile(r"^export\s+", re.M), ""),
    (re.compile(r"\)\s*:\s*" + _TYPE + r"\s*(\{|=>)"), r") \1"),
    (re.compile(r"(\(|,\s*)(\w+)\??\s*:\s*" + _TYPE + r"(?=\s*[,)=])"), r"\1\2"),
)


def _run_node(js: str, what: str) -> str:
    node = shutil.which("node")
    if node is None:
        raise AssertionError("node is not on PATH; this check runs the page's own policy code")
    proc = subprocess.run(
        [node, "--input-type=module", "-e", js], capture_output=True, text=True, timeout=30
    )
    if proc.returncode != 0:
        raise AssertionError(f"could not evaluate {what} with node: {proc.stderr.strip()[:400]}")
    return proc.stdout.strip()


def meta_policy_for_same_origin_build() -> dict[str, str]:
    """The <meta> policy web/vite.config.ts's contentSecurityPolicy() returns for the build the
    Hosting rewrite serves (an HTTP build, same origin, no proxy URL): the function and the
    constants it uses are taken from the file and run with node."""
    text = VITE_CONFIG.read_text(encoding="utf-8")
    parts = []
    # Module-level constants the function may use (T45: RECAPTCHA_CSP), then the function.
    for const in re.finditer(r"^(?:export\s+)?const\s+([A-Z][A-Z0-9_]*)\s*=\s*\{", text, re.M):
        parts.append(_block(text, re.escape(const.group(0)), VITE_CONFIG))
    parts.append(_block(text, r"^(?:export\s+)?function\s+contentSecurityPolicy\s*\(", VITE_CONFIG))
    js = "\n".join(parts)
    for pattern, replacement in _ERASE:
        js = pattern.sub(replacement, js)
    # 'production' is Vite's mode for `npm run build` (anything but 'mock' is an HTTP build);
    # the second argument, which older versions took (the proxy URL), is unset there.
    js += "\nconsole.log(contentSecurityPolicy('production', undefined));\n"
    return parse_policy(_run_node(js, "vite.config.ts contentSecurityPolicy()"))


def check_bundle_requirements() -> dict[str, str]:
    """The directives web/scripts/check-bundle.mjs requires of an HTTP build, from its own
    `expected` object (and the constants declared just before it), run with node."""
    text = CHECK_BUNDLE.read_text(encoding="utf-8")
    expected = re.search(r"^\s*const expected = \{", text, re.M)
    if expected is None:
        raise AssertionError(f"the expected directives were not found in {CHECK_BUNDLE}")
    # The object constants declared before `expected` that it refers to (T45: `google`).
    body = _block(text, re.escape(expected.group(0)), CHECK_BUNDLE)
    head = text[: expected.start()]
    parts = [
        _block(text, re.escape(m.group(0)), CHECK_BUNDLE)
        for m in re.finditer(r"^\s*const (\w+) = \{", head, re.M)
        if re.search(rf"\b{m.group(1)}\.", body)
    ]
    parts.append(body)
    js = (
        "const mode = 'http'; const proxyOrigin = null;\n"
        + "\n".join(p.strip() for p in parts)
        + "\nconsole.log(JSON.stringify(expected));\n"
    )
    values = json.loads(_run_node(js, "check-bundle.mjs expected"))
    return {k: v for k, v in values.items() if isinstance(v, str)}


def main() -> int:
    headers = json.loads(HEADERS.read_text(encoding="utf-8"))
    problems: list[str] = []

    csp_names = [k for k in headers if k.lower().startswith("content-security-policy")]
    if csp_names != ["Content-Security-Policy"]:
        problems.append(f"expected exactly one Content-Security-Policy header, got {csp_names}")
    header = parse_policy(headers.get("Content-Security-Policy", ""))

    for name, value in header.items():
        if FORBIDDEN_SOURCES.search(value):
            problems.append(f"{name} allows a forbidden source: {value!r}")
        external = {src for src in value.split() if src.startswith("https://")}
        wanted_external = RECAPTCHA_SOURCES.get(name, set())
        if external != wanted_external:
            problems.append(
                f"{name} external sources {sorted(external)} are not exactly reCAPTCHA's "
                f"{sorted(wanted_external)}"
            )
    for name in RECAPTCHA_SOURCES:
        if name not in header:
            problems.append(f"{name} is missing: reCAPTCHA needs {sorted(RECAPTCHA_SOURCES[name])}")

    meta = meta_policy_for_same_origin_build()
    wanted = {**meta, **HEADER_ONLY}
    if header != wanted:
        problems.append(
            f"header CSP {header} differs from the page's policy plus frame-ancestors {wanted}"
        )

    for name, value in check_bundle_requirements().items():
        if header.get(name) != value:
            problems.append(
                f"check-bundle.mjs requires {name} {value!r}, the header has {header.get(name)!r}"
            )

    for name, value in REQUIRED_HEADERS.items():
        if headers.get(name) != value:
            problems.append(f"{name} must be {value!r}, got {headers.get(name)!r}")

    for p in problems:
        print(f"FAIL: {p}")
    if not problems:
        print(
            "ok:   hosting CSP matches the page's policy plus frame-ancestors: "
            + headers["Content-Security-Policy"]
        )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
