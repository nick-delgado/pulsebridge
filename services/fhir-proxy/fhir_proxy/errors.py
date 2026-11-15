"""Errors answered as a FHIR R4 ``OperationOutcome``, with fixed wording.

Every refusal the proxy makes is a :class:`Problem`: an HTTP status, an R4 issue-type code
(http://hl7.org/fhir/issue-type), a fixed sentence and a short machine-readable reason. The
sentence never repeats what the caller sent, what an upstream service said, a patient identifier
or an upstream URL; the reason is one of a closed set, so a test can check every refusal.

Upstream failures of the Cloud Healthcare API are mapped by error class (``pulsebridge_gcp``'s
typed errors) to four bounded reasons: permission denied, missing, unavailable, timed out.
"""

from __future__ import annotations

import json

from pulsebridge_gcp import (
    HealthcareApiError,
    MalformedResponseError,
    NotFoundError,
    PermissionDeniedError,
    RetryableError,
    TimedOutError,
)

FHIR_JSON = "application/fhir+json"
# Code system for the proxy's own reasons, in OperationOutcome.issue.details.coding.
REASON_SYSTEM = "urn:pulsebridge:fhir-proxy:reason"


class Problem(Exception):
    """A refusal or failure to answer with an OperationOutcome."""

    def __init__(
        self,
        status: int,
        code: str,
        reason: str,
        text: str,
        *,
        expression: list[str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(reason)
        self.status = status
        self.code = code
        self.reason = reason
        self.text = text
        self.expression = expression
        self.headers = headers or {}

    def outcome(self) -> dict:
        issue: dict = {
            "severity": "error",
            "code": self.code,
            "details": {"coding": [{"system": REASON_SYSTEM, "code": self.reason}]},
            "diagnostics": self.text,
        }
        if self.expression:
            issue["expression"] = list(self.expression)
        return {"resourceType": "OperationOutcome", "issue": [issue]}

    def body(self) -> bytes:
        return json.dumps(self.outcome()).encode("utf-8")


def operation_outcome_body(status_code: str, reason: str, text: str) -> bytes:
    return Problem(0, status_code, reason, text).body()


def upstream_problem(error: HealthcareApiError, what: str) -> Problem:
    """The bounded answer for a failed Cloud Healthcare call. ``what`` is a fixed noun phrase
    chosen by the caller ("the FHIR store", "the audit store", "the HL7v2 store")."""
    if isinstance(error, NotFoundError):
        return Problem(404, "not-found", "not-found", "The requested record was not found.")
    if isinstance(error, PermissionDeniedError):
        return Problem(
            502,
            "forbidden",
            "upstream-permission-denied",
            f"The proxy is not allowed to read {what}; check its IAM roles.",
        )
    if isinstance(error, TimedOutError):
        return Problem(
            504, "timeout", "upstream-timeout", f"{what.capitalize()} did not answer in time."
        )
    if isinstance(error, RetryableError):
        return Problem(
            503,
            "transient",
            "upstream-unavailable",
            f"{what.capitalize()} is unavailable; try again shortly.",
        )
    if isinstance(error, MalformedResponseError):
        return Problem(
            502, "exception", "upstream-bad-answer", f"{what.capitalize()} sent an unusable answer."
        )
    # ClientError and anything else: the store refused a request the proxy built. A bug or a
    # contract change, never the caller's input (which was validated first).
    return Problem(502, "exception", "upstream-refused", f"{what.capitalize()} refused the query.")


def reachability_reason(error: Exception) -> str:
    """A short code for the status strip's ``detail`` (never the server's text)."""
    if isinstance(error, PermissionDeniedError):
        return "permission-denied"
    if isinstance(error, NotFoundError):
        return "store-missing"
    if isinstance(error, TimedOutError):
        return "timeout"
    if isinstance(error, RetryableError):
        return "unavailable"
    return "bad-answer"
