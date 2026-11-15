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
`'strict-dynamic'`, a wildcard, or a scheme (`data:`, `http:`, `https:`, `blob:`).
The other security headers must have their documented values (REQUIRED_HEADERS).

Usage: python3 infra/modules/phr/tests/check_hosting_csp.py   (standard library only)
"""

from __future__ import annotations

import json
import re
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
    r"'unsafe-inline'|'unsafe-eval'|'unsafe-hashes'|'strict-dynamic'|\*|\b(data|https?|blob):",
)


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


def meta_policy_for_same_origin_build() -> dict[str, str]:
    """The directives of web/vite.config.ts's contentSecurityPolicy() for an HTTP build whose
    proxy URL is the same-origin default, read from its array literal."""
    text = VITE_CONFIG.read_text(encoding="utf-8")
    body = re.search(r"export function contentSecurityPolicy\([^)]*\)[^{]*\{(.*?)\n\}", text, re.S)
    if body is None:
        raise AssertionError(f"contentSecurityPolicy() not found in {VITE_CONFIG}")
    default_connect = re.search(r"let connect = \"([^\"]+)\";", body.group(1))
    array = re.search(r"return \[(.*?)\]\.join\('; '\)", body.group(1), re.S)
    if default_connect is None or array is None:
        raise AssertionError("contentSecurityPolicy() no longer has the shape this check reads")
    items = re.findall(r"\"([^\"]+)\"|`([^`]+)`", array.group(1))
    parts = [
        (plain or template).replace("${connect}", default_connect.group(1))
        for plain, template in items
    ]
    if any("${" in p for p in parts):
        raise AssertionError(f"unexpected template in the meta policy: {parts}")
    return parse_policy("; ".join(parts))


def check_bundle_requirements() -> dict[str, str]:
    """The `expected` map in check-bundle.mjs, with its same-origin HTTP connect-src."""
    text = CHECK_BUNDLE.read_text(encoding="utf-8")
    block = re.search(r"const expected = \{(.*?)\n  \};", text, re.S)
    if block is None:
        raise AssertionError(f"the expected directives were not found in {CHECK_BUNDLE}")
    expected = dict(re.findall(r"'([a-z-]+)': \"([^\"]+)\"", block.group(1)))
    connect = re.search(r"'connect-src':.*: \"('self')\",", block.group(1))
    if connect is None:
        raise AssertionError(
            "check-bundle.mjs no longer requires connect-src 'self' for a same-origin build"
        )
    expected["connect-src"] = connect.group(1)
    return expected


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
