"""The bot check (D16): one Google Cloud Fraud Defense assessment per session request.

Google Cloud Fraud Defense is the current name of reCAPTCHA Enterprise; the API, its host and
its IAM names still say ``recaptchaenterprise``. Terms used here:

- **site key**: the ID of the score-based web key (``RECAPTCHA_SITE_KEY``). It is public: the
  page passes it to Google's script, which runs in the visitor's browser and returns a
  **token**, a one-time, short-lived proof of what the script observed.
- **action**: a name the page gives the check (``view_demo``). Google records it inside the
  token, so a token made for some other page action cannot be replayed here.
- **assessment**: what the server asks Google about a token (``projects.assessments.create``).
  The answer says whether the token is genuine (``tokenProperties.valid``, ``action``,
  ``hostname``) and gives a **score** (``riskAnalysis.score``) from 0.0 (very likely a bot) to
  1.0 (very likely a person). Only the server can ask: the browser could lie about its own
  result, so a passed check is never taken from the browser's word.

The request (REST, ``POST
https://recaptchaenterprise.googleapis.com/v1/projects/{project}/assessments``, OAuth scope
``cloud-platform``, the caller needs ``recaptchaenterprise.assessments.create``, which
``roles/recaptchaenterprise.agent`` grants)::

    {"event": {"token": "<token>", "siteKey": "<site key>", "expectedAction": "view_demo"}}

The parts of the answer read here (JSON field names; protobuf's JSON form leaves out a field
that holds its default, so a missing ``valid`` is false and a missing ``score`` is 0.0)::

    {"name": "projects/<number>/assessments/<id>",
     "tokenProperties": {"valid": true, "invalidReason": "INVALID_REASON_UNSPECIFIED",
                         "hostname": "<page host>", "action": "view_demo",
                         "createTime": "..."},
     "riskAnalysis": {"score": 0.9, "reasons": []}}

The check passes only when all four hold: the token is valid, its action is ``RECAPTCHA_ACTION``,
its hostname is the host of one of ``ALLOWED_ORIGINS`` (the token was made on our page, not on
a copy of it elsewhere using our public site key), and the score is at least
``RECAPTCHA_MIN_SCORE``. Which one failed is logged as a code; the caller is told only
``bot-check-failed``, so a bot learns nothing about what to change. The visitor's IP address
and user agent are not sent (``event.userIpAddress``, ``event.userAgent`` are optional): the
token already carries what the browser script saw, and less personal data leaves the proxy.

Sources (19 November 2026; README "The bot check" says what was read how): the REST reference
for ``projects.assessments.create``
(docs.cloud.google.com/recaptcha/docs/reference/rest/v1/projects.assessments/create) and
"Create assessments for websites" (docs.cloud.google.com/recaptcha/docs/create-assessment) for
the endpoint and fields, checked against the field definitions in Google's client library
``google-cloud-recaptcha-enterprise`` 1.34.0 (``Event``, ``TokenProperties``, ``RiskAnalysis``,
default host ``recaptchaenterprise.googleapis.com``); "Google Cloud Fraud Defense roles and
permissions" (docs.cloud.google.com/iam/docs/roles-permissions/recaptchaenterprise) for the role.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import quote

from pulsebridge_gcp import HealthcareApiError, TimedOutError

API_BASE = "https://recaptchaenterprise.googleapis.com/v1"
# A reCAPTCHA token is base64url-like text, typically one to two thousand characters. This is
# the cheap shape check made before any money is spent: anything else is refused without an
# assessment. The bounds are generous on purpose (a real token outside them would lock
# everyone out); the README lists confirming them on a real project.
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{20,8192}", re.ASCII)
MAX_ANSWER_BYTES = 256 * 1024
# An enum name as protobuf JSON writes it; anything else from the answer is not logged.
_ENUM = re.compile(r"[A-Z_]{1,40}", re.ASCII)


class BotCheckUnavailable(Exception):
    """No verdict: Fraud Defense did not answer, refused the request or answered something
    unusable. ``str()`` is a fixed code (``timeout``, ``unreachable``, ``http-<status>``,
    ``bad-answer``), never Google's text. The caller fails closed."""


@dataclass(frozen=True)
class Verdict:
    passed: bool
    # "passed", "invalid-token", "wrong-action", "wrong-hostname" or "low-score"
    reason: str
    # Google's invalidReason enum name (MALFORMED, EXPIRED, DUPE, ...) when the token is invalid.
    invalid_reason: str | None = None
    score: float | None = None


def token_has_shape(token: object) -> bool:
    return isinstance(token, str) and TOKEN_SHAPE.fullmatch(token) is not None


class FraudDefense:
    """Creates assessments over a ``pulsebridge_gcp`` transport (the app wraps it so every call
    honours the request's time budget)."""

    def __init__(
        self,
        transport: object,
        *,
        project: str,
        site_key: str,
        action: str,
        min_score: float,
        hostnames: Iterable[str],
        timeout: float,
    ) -> None:
        self._transport = transport
        self._url = f"{API_BASE}/projects/{quote(project, safe='')}/assessments"
        self._site_key = site_key
        self._action = action
        self._min_score = min_score
        self._hostnames = frozenset(h.lower() for h in hostnames)
        self._timeout = timeout

    def assess(self, token: str) -> Verdict:
        body = {
            "event": {"token": token, "siteKey": self._site_key, "expectedAction": self._action}
        }
        try:
            response = self._transport.request(  # type: ignore[attr-defined]
                "POST",
                self._url,
                params=None,
                body=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                timeout=self._timeout,
            )
        except HealthcareApiError as exc:
            # pulsebridge_gcp's transport errors: TimedOutError (and the budget's
            # BudgetExhausted) or RetryableError (no answer at all).
            code = "timeout" if isinstance(exc, TimedOutError) else "unreachable"
            raise BotCheckUnavailable(code) from None
        if response.status != 200:
            # 400 (a request we built wrongly, or a key that does not exist), 403 (the role is
            # missing or the API is off), 429 (quota), 5xx: none is the visitor's fault.
            raise BotCheckUnavailable(f"http-{response.status}")
        if len(response.body) > MAX_ANSWER_BYTES:
            raise BotCheckUnavailable("bad-answer")
        try:
            answer = response.json()
        except (UnicodeDecodeError, ValueError):
            raise BotCheckUnavailable("bad-answer") from None
        return self.judge(answer)

    def judge(self, answer: object) -> Verdict:
        """The verdict on one assessment answer (see the module docstring for the rule)."""
        if not isinstance(answer, dict):
            raise BotCheckUnavailable("bad-answer")
        props = answer.get("tokenProperties", {})
        risk = answer.get("riskAnalysis", {})
        if not isinstance(props, dict) or not isinstance(risk, dict):
            raise BotCheckUnavailable("bad-answer")
        valid = props.get("valid", False)
        score = risk.get("score", 0.0)
        action = props.get("action", "")
        hostname = props.get("hostname", "")
        if (
            not isinstance(valid, bool)
            or not isinstance(score, int | float)
            or isinstance(score, bool)
            or not isinstance(action, str)
            or not isinstance(hostname, str)
        ):
            raise BotCheckUnavailable("bad-answer")
        if not valid:
            reason = props.get("invalidReason")
            known = reason if isinstance(reason, str) and _ENUM.fullmatch(reason) else None
            return Verdict(False, "invalid-token", invalid_reason=known)
        if action != self._action:
            return Verdict(False, "wrong-action", score=float(score))
        if hostname.lower() not in self._hostnames:
            return Verdict(False, "wrong-hostname", score=float(score))
        if not float(score) >= self._min_score:
            return Verdict(False, "low-score", score=float(score))
        return Verdict(True, "passed", score=float(score))
