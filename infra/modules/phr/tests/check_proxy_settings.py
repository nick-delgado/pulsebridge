#!/usr/bin/env python3
"""Module phr's demo settings agree with fhir-proxy's own configuration (T48).

Module phr passes ten settings of the public demo to fhir-proxy as environment variables (D16:
T44's four bot-check settings, T46's five rate limits, T47's HISTORY_DAYS). fhir-proxy checks
each again when it starts (`services/fhir-proxy/fhir_proxy/config.py`): its default in
`VARIABLES` and its range in `ServiceConfig.from_env`'s `_number(...)` calls. If the module
accepted a value the proxy refuses, the apply would succeed and the new revision would never
start; if the defaults differed, "unset" would mean two different things. So, read from the
source text of both (no Terraform, no Python packages needed, so `make tf-test` can run it with
the system python3):

1. every setting's module default equals the proxy's default;
2. T46's five limits and HISTORY_DAYS (passed through since T48) have exactly the proxy's range,
   as the work plan asks; T44's four may be narrower, never wider (the module keeps the session
   at least 5 minutes and the daily cap at most 3000 on purpose);
3. each is a whole number in both places where the proxy wants one;
4. the page's privacy note (web/privacy.html) states the defaults it depends on: sessions of
   24 hours (SESSION_TTL_SECONDS) and the last 7 days (HISTORY_DAYS).

Run from anywhere: `python3 infra/modules/phr/tests/check_proxy_settings.py`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
REPO = MODULE.parents[2]
CONFIG = REPO / "services" / "fhir-proxy" / "fhir_proxy" / "config.py"
VARIABLES_TF = MODULE / "variables.tf"
PRIVACY = REPO / "web" / "privacy.html"

# Exactly the proxy's range (T48's settings) or within it (T44's).
EXACT = {
    "REQUESTS_PER_ADDRESS_PER_MINUTE",
    "READS_PER_SESSION_PER_MINUTE",
    "CUFF_SENDS_PER_SESSION_PER_10_MINUTES",
    "SHARES_PER_SESSION_PER_10_MINUTES",
    "SHARES_PER_INSTANCE_PER_HOUR",
    "HISTORY_DAYS",
}
WITHIN = {
    "RECAPTCHA_MIN_SCORE",
    "SESSION_TTL_SECONDS",
    "SESSIONS_PER_ADDRESS_PER_HOUR",
    "BOT_CHECKS_PER_DAY",
}


def proxy_settings(text: str) -> dict[str, dict]:
    """{NAME: {default, low, high, whole}} from config.py's VARIABLES and _number calls."""
    found: dict[str, dict] = {}
    for name in EXACT | WITHIN:
        # "NAME": Variable(<description strings>, "<default>"), the default being the last
        # string literal before the closing parenthesis.
        m = re.search(rf'"{name}": Variable\((.*?)\n?\s*\),?\n', text, re.S)
        if not m:
            raise SystemExit(f"config.py: no VARIABLES entry for {name}")
        default = re.findall(r'"([^"]*)"', m.group(1))[-1]
        r = re.search(rf'_number\("{name}", values, ([0-9.]+), ([0-9.]+)(, whole=True)?\)', text)
        if not r:
            raise SystemExit(f"config.py: no _number(...) range check for {name}")
        found[name] = {
            "default": float(default),
            "low": float(r.group(1)),
            "high": float(r.group(2)),
            "whole": bool(r.group(3)),
        }
    return found


def module_settings(text: str) -> dict[str, dict]:
    """{NAME: {default, low, high, whole, low_strict}} from variables.tf."""
    found: dict[str, dict] = {}
    for name in EXACT | WITHIN:
        var = name.lower()
        m = re.search(rf'variable "{var}" \{{(.*?)\n\}}', text, re.S)
        if not m:
            raise SystemExit(f"variables.tf: no variable {var!r} (the pass-through of {name})")
        body = m.group(1)
        default = re.search(r"^\s*default\s*=\s*([0-9.]+)\s*$", body, re.M)
        cond = re.search(r"condition\s*=\s*(.*)", body)
        if not default or not cond:
            raise SystemExit(f"variables.tf: {var} needs a numeric default and a validation")
        low = re.search(rf"var\.{var} (>=|>) ([0-9.]+)", cond.group(1))
        high = re.search(rf"var\.{var} <= ([0-9.]+)", cond.group(1))
        if not low or not high:
            raise SystemExit(f"variables.tf: {var}'s validation must bound it on both sides")
        found[name] = {
            "default": float(default.group(1)),
            "low": float(low.group(2)),
            "low_strict": low.group(1) == ">",
            "high": float(high.group(1)),
            "whole": f"floor(var.{var}) == var.{var}" in cond.group(1),
        }
    return found


def problems(proxy: dict[str, dict], module: dict[str, dict], privacy: str) -> list[str]:
    out = []
    for name in sorted(EXACT | WITHIN):
        p, m = proxy[name], module[name]
        if p["default"] != m["default"]:
            out.append(f"{name}: module default {m['default']:g}, proxy default {p['default']:g}")
        if p["whole"] and not m["whole"]:
            out.append(f"{name}: the proxy wants a whole number; the module allows fractions")
        if name in EXACT:
            if (m["low"], m["high"], m["low_strict"]) != (p["low"], p["high"], False):
                out.append(
                    f"{name}: module range {m['low']:g} to {m['high']:g}, proxy range "
                    f"{p['low']:g} to {p['high']:g} (they must be the same)"
                )
        elif m["low"] < p["low"] or m["high"] > p["high"]:
            out.append(
                f"{name}: module range {m['low']:g} to {m['high']:g} is wider than the "
                f"proxy's {p['low']:g} to {p['high']:g}"
            )
    hours = module["SESSION_TTL_SECONDS"]["default"] / 3600
    days = module["HISTORY_DAYS"]["default"]
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", privacy))
    if not re.search(rf"\b{hours:g} hours\b", text):
        out.append(f"web/privacy.html does not say sessions last {hours:g} hours")
    if not re.search(rf"\blast {days:g} days\b", text):
        out.append(f"web/privacy.html does not say only the last {days:g} days are shown")
    return out


def main() -> int:
    proxy = proxy_settings(CONFIG.read_text(encoding="utf-8"))
    module = module_settings(VARIABLES_TF.read_text(encoding="utf-8"))
    found = problems(proxy, module, PRIVACY.read_text(encoding="utf-8"))
    # Real drift first, so it is always reported as what it is.
    if found:
        print("check_proxy_settings: module phr and fhir-proxy disagree:")
        for p in found:
            print(f"  - {p}")
        return 1
    # The check's own control, independent of the real module: a module that copies the proxy
    # exactly passes, and a copy drifted in each way is reported exactly once per drift.
    baseline = {k: {**v, "low_strict": False} for k, v in proxy.items()}
    good_note = "sessions last 24 hours; only the last 7 days"
    drifted = {k: dict(v) for k, v in baseline.items()}
    drifted["HISTORY_DAYS"]["high"] = 31
    drifted["SHARES_PER_INSTANCE_PER_HOUR"]["default"] = 15
    drifted["BOT_CHECKS_PER_DAY"]["high"] = 200000
    clean = problems(proxy, baseline, good_note)
    control = problems(proxy, drifted, "sessions last 1 hours; the last 2 days")
    if clean or len(control) != 5:
        print(f"check_proxy_settings: the control found {clean} and {control}; the check is broken")
        return 1
    print(f"check_proxy_settings: {len(module)} settings agree with fhir-proxy's config.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
