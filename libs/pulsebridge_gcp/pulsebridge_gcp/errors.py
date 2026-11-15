"""Typed errors for Cloud Healthcare API calls.

Callers decide what to do with a failure by its class, not by reading status codes:

- :class:`NotFoundError` (404): the resource is gone; stop.
- :class:`PermissionDeniedError` (401, 403): configuration or IAM, often still propagating after
  an apply; retry later.
- :class:`ConflictError` (409): depends on the call, a concurrent FHIR update or an HL7v2
  duplicate.
- :class:`ClientError` (any other 4xx): the request was refused as it stands; sending the same
  request again will not help.
- :class:`RetryableError` (408, 429, 5xx, a network failure, a token refresh failure): retry.
- :class:`MalformedResponseError`: a 2xx whose body is not what the API documents; a bug or a
  contract change.

Error messages (``str(error)``) name the operation and the HTTP status only. The server's own
text can echo request content, which for this project is health data, so it is kept on the
exception (``server_message``, ``operation_outcome``) for the caller to log selectively, and never
put into the message itself.
"""

from __future__ import annotations


class HealthcareApiError(Exception):
    """Base class for every failure of a Cloud Healthcare API call."""

    def __init__(
        self,
        operation: str,
        status: int | None,
        *,
        server_message: str | None = None,
    ) -> None:
        shown = f"HTTP {status}" if status is not None else "no HTTP response"
        super().__init__(f"{operation}: {shown}")
        self.operation = operation
        self.status = status
        # Google's error.message. May echo request content: log with care.
        self.server_message = server_message


class NotFoundError(HealthcareApiError):
    """HTTP 404: the store, the message or the resource does not exist."""


class PermissionDeniedError(HealthcareApiError):
    """HTTP 401 or 403: the caller's identity may not do this (or its role is still propagating)."""


class ClientError(HealthcareApiError):
    """A 4xx other than 401, 403 and 404: the request was refused as it stands.

    - ``operation_outcome``: the FHIR ``OperationOutcome`` the FHIR store returned, if any.
    - ``hl7_nack``: the HL7v2 negative acknowledgment (ER7 bytes, decoded from base64) that
      ``messages:ingest`` puts in ``error.details[].hl7Nack`` when the HL7v2 store rejects a
      message (spike S5). Parse it with ``pulsebridge_hl7v2.parse_ack``.
    """

    def __init__(
        self,
        operation: str,
        status: int | None,
        *,
        server_message: str | None = None,
        operation_outcome: dict | None = None,
        hl7_nack: bytes | None = None,
    ) -> None:
        super().__init__(operation, status, server_message=server_message)
        self.operation_outcome = operation_outcome
        self.hl7_nack = hl7_nack


class ConflictError(ClientError):
    """HTTP 409. For FHIR, usually two writes to one resource at once (worth a retry); for HL7v2
    ingest with ``rejectDuplicateMessage``, "message already exists" with a NACK (not worth one)."""


class RetryableError(HealthcareApiError):
    """A failure that may pass if the same request is sent again later: HTTP 408 (timeout),
    429 (quota), any 5xx, a network error, or a failure to obtain an access token."""


class MalformedResponseError(HealthcareApiError):
    """A successful status with a body that is not what the API documents (not JSON, or a
    required field missing or not decodable)."""


def operation_outcome_summary(outcome: dict | None) -> list[dict]:
    """The parts of a FHIR ``OperationOutcome`` that are safe to log.

    Keeps each issue's ``severity``, ``code`` (the R4 ``issue-type`` code, such as ``invalid`` or
    ``processing``) and ``expression``/``location`` (element paths such as
    ``Observation.component[0].valueQuantity``). Drops ``diagnostics`` and ``details.text``,
    which servers fill with free text that can quote the resource, and so the health data in it.
    """
    if not isinstance(outcome, dict):
        return []
    issues = outcome.get("issue")
    if not isinstance(issues, list):
        return []
    summary = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        item = {k: issue[k] for k in ("severity", "code") if isinstance(issue.get(k), str)}
        for key in ("expression", "location"):
            paths = issue.get(key)
            if isinstance(paths, list) and all(isinstance(p, str) for p in paths):
                item[key] = list(paths)
        summary.append(item)
    return summary
