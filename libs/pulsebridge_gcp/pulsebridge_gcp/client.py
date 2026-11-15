"""A thin client for the parts of the Cloud Healthcare REST API (v1) PulseBridge uses.

FHIR store:   executeBundle, read, search (one page, plus the next page), PUT one resource.
HL7v2 store:  messages.get (with a view), messages.list (filter, orderBy, paging),
              messages.patch of labels, messages:ingest.

Every call is one HTTP request through the injected :class:`~pulsebridge_gcp.transport.Transport`;
there are no hidden retries. Callers on Pub/Sub push (``v2-to-fhir``) let Pub/Sub retry; others
decide for themselves, guided by the error class (see :mod:`pulsebridge_gcp.errors`).

Paths are built from validated parts, never from free text, so a value that came from outside
(a Pub/Sub message, a FHIR id) cannot point a request at another resource.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from urllib.parse import urlsplit

from pulsebridge_gcp.errors import (
    ClientError,
    ConflictError,
    HealthcareApiError,
    MalformedResponseError,
    NotFoundError,
    PermissionDeniedError,
    RetryableError,
)
from pulsebridge_gcp.models import (
    Hl7v2Message,
    IngestResult,
    MessagePage,
    check_labels,
    decode_base64_field,
)
from pulsebridge_gcp.transport import HttpResponse, Params, Transport

DEFAULT_BASE_URL = "https://healthcare.googleapis.com/v1"
# One request must finish well inside the caller's own deadline. v2-to-fhir makes three calls
# per push and Pub/Sub waits 60 s for the answer (T05 ack_deadline_seconds).
DEFAULT_TIMEOUT_SECONDS = 15.0

# messages.get / messages.list views (spike S5, "Stored message shape").
MESSAGE_VIEWS = ("BASIC", "RAW_ONLY", "PARSED_ONLY", "FULL", "SCHEMATIZED_ONLY")

# Resource-name parts, the same as in MESSAGE_NAME below, so a client that starts with a
# configuration also recognises the message names of its own store. Project ids and locations
# are lowercase letters, digits and "-"; dataset and store ids may also hold "_" and ".".
_PROJECT_OR_LOCATION = re.compile(r"[a-z0-9-]{1,63}", re.ASCII)
_SEGMENT = re.compile(r"[A-Za-z0-9_.-]{1,256}", re.ASCII)
# A stored message's id is the URL-safe base64 of a SHA-256 (spike S5): letters, digits, -, _, =.
MESSAGE_NAME = re.compile(
    r"projects/[a-z0-9-]{1,63}/locations/[a-z0-9-]{1,63}/datasets/[A-Za-z0-9_.-]{1,256}"
    r"/hl7V2Stores/[A-Za-z0-9_.-]{1,256}/messages/[A-Za-z0-9_=-]{1,256}",
    re.ASCII,
)
# FHIR R4: resource type names are letters; ids are 1-64 of [A-Za-z0-9-.].
_FHIR_TYPE = re.compile(r"[A-Z][A-Za-z]{1,63}", re.ASCII)
_FHIR_ID = re.compile(r"[A-Za-z0-9.-]{1,64}", re.ASCII)

FHIR_JSON = "application/fhir+json; charset=utf-8"
JSON = "application/json; charset=utf-8"


def _check(pattern: re.Pattern[str], what: str, value: object) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{what} {value!r} is not valid")
    return value


def _google_error(body: object) -> dict:
    """``error`` from Google's JSON error envelope ``{"error": {"code", "message", ...}}``."""
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return body["error"]
    return {}


def _nack_from(error: dict) -> bytes | None:
    """The HL7 NACK the HL7v2 store puts in ``error.details[].hl7Nack`` (spike S5)."""
    for detail in error.get("details", []) or []:
        if isinstance(detail, dict) and isinstance(detail.get("hl7Nack"), str):
            try:
                return base64.b64decode(detail["hl7Nack"], validate=True)
            except ValueError:
                return None
    return None


def raise_for_status(operation: str, response: HttpResponse) -> None:
    """Turn a non-2xx response into the matching typed error."""
    status = response.status
    if 200 <= status < 300:
        return
    try:
        body = response.json()
    except ValueError:
        body = None
    error = _google_error(body)
    message = error.get("message") if isinstance(error.get("message"), str) else None
    # FHIR methods return an OperationOutcome resource as the error body (R4 http.html).
    outcome = (
        body if isinstance(body, dict) and body.get("resourceType") == "OperationOutcome" else None
    )
    if status == 404:
        raise NotFoundError(operation, status, server_message=message)
    if status in (401, 403):
        raise PermissionDeniedError(operation, status, server_message=message)
    if status in (408, 429) or status >= 500:
        raise RetryableError(operation, status, server_message=message)
    cls = ConflictError if status == 409 else ClientError
    raise cls(
        operation,
        status,
        server_message=message,
        operation_outcome=outcome,
        hl7_nack=_nack_from(error),
    )


class HealthcareClient:
    """Calls into one Cloud Healthcare dataset.

    ``project``, ``location`` and ``dataset`` come from configuration (Terraform outputs in
    environment variables); nothing deployment-specific is built in. Stores are passed by their
    short id (``fhir_store="main"``); HL7v2 messages by their full resource name, as the store's
    Pub/Sub notification and ``messages.list`` give it.
    """

    def __init__(
        self,
        *,
        project: str,
        location: str,
        dataset: str,
        transport: Transport,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.dataset_name = (
            f"projects/{_check(_PROJECT_OR_LOCATION, 'project', project)}"
            f"/locations/{_check(_PROJECT_OR_LOCATION, 'location', location)}"
            f"/datasets/{_check(_SEGMENT, 'dataset', dataset)}"
        )
        self._transport = transport
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    # ---- names -------------------------------------------------------------------------------

    def fhir_store_name(self, store: str) -> str:
        return f"{self.dataset_name}/fhirStores/{_check(_SEGMENT, 'FHIR store', store)}"

    def hl7v2_store_name(self, store: str) -> str:
        return f"{self.dataset_name}/hl7V2Stores/{_check(_SEGMENT, 'HL7v2 store', store)}"

    def is_message_in_store(self, name: str, store: str) -> bool:
        """True if ``name`` is a well-formed message name inside this dataset's HL7v2 ``store``."""
        return (
            isinstance(name, str)
            and MESSAGE_NAME.fullmatch(name) is not None
            and name.startswith(self.hl7v2_store_name(store) + "/messages/")
        )

    def fhir_base_url(self, store: str) -> str:
        return f"{self._base_url}/{self.fhir_store_name(store)}/fhir"

    # ---- plumbing ----------------------------------------------------------------------------

    def _call(
        self,
        method: str,
        url: str,
        *,
        params: Params = None,
        body: object = None,
        content_type: str = JSON,
    ) -> object:
        operation = f"{method} {url.removeprefix(self._base_url + '/')}"
        headers = {"Accept": "application/json"}
        payload = None
        if body is not None:
            payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = content_type
        response = self._transport.request(
            method, url, params=params, body=payload, headers=headers, timeout=self._timeout
        )
        raise_for_status(operation, response)
        if not response.body:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise MalformedResponseError(
                operation, response.status, server_message="body is not JSON"
            ) from exc

    def _object(self, method: str, url: str, **kwargs: object) -> dict:
        result = self._call(method, url, **kwargs)  # type: ignore[arg-type]
        if not isinstance(result, dict):
            raise MalformedResponseError(f"{method} {url}", 200, server_message="not an object")
        return result

    # ---- FHIR --------------------------------------------------------------------------------

    def execute_bundle(self, fhir_store: str, bundle: dict) -> dict:
        """POST a ``transaction`` (or ``batch``) Bundle to the FHIR base URL; returns the response
        Bundle. A transaction is all-or-nothing: on any failure nothing is written and this
        raises with the store's ``OperationOutcome`` on :class:`ClientError`."""
        if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
            raise ValueError("execute_bundle needs a Bundle resource")
        return self._object(
            "POST", self.fhir_base_url(fhir_store), body=bundle, content_type=FHIR_JSON
        )

    def read_resource(self, fhir_store: str, resource_type: str, resource_id: str) -> dict:
        """GET ``<type>/<id>``. Raises :class:`NotFoundError` if absent (or deleted)."""
        url = (
            f"{self.fhir_base_url(fhir_store)}/{_check(_FHIR_TYPE, 'resource type', resource_type)}"
            f"/{_check(_FHIR_ID, 'resource id', resource_id)}"
        )
        return self._object("GET", url)

    def search(
        self,
        fhir_store: str,
        resource_type: str,
        params: Mapping[str, str] | Sequence[tuple[str, str]],
    ) -> dict:
        """GET ``<type>?<params>``: one page of results as a ``searchset`` Bundle.

        ``params`` is a mapping, or a sequence of ``(name, value)`` pairs when a parameter
        repeats: FHIR combines repeats with AND, e.g. ``[("date", "ge2026-11-01"),
        ("date", "lt2026-11-08")]`` for one week.
        """
        resource_type = _check(_FHIR_TYPE, "resource type", resource_type)
        url = f"{self.fhir_base_url(fhir_store)}/{resource_type}"
        pairs = list(params.items()) if isinstance(params, Mapping) else list(params)
        for pair in pairs:
            if not (
                isinstance(pair, tuple)
                and len(pair) == 2
                and all(isinstance(part, str) for part in pair)
            ):
                raise ValueError("search parameters must be (name, value) pairs of strings")
        return self._object("GET", url, params=pairs)

    def capabilities(self, fhir_store: str) -> dict:
        """GET ``metadata``: the store's CapabilityStatement (what it supports: resource
        types, search parameters, interactions)."""
        return self._object("GET", f"{self.fhir_base_url(fhir_store)}/metadata")

    def next_page(self, fhir_store: str, bundle: dict) -> dict | None:
        """The page after ``bundle`` (its ``link`` with ``relation: next``), or ``None``.

        The link must point inside this FHIR store, so a response cannot send the access token
        elsewhere.
        """
        for link in bundle.get("link", []) or []:
            if isinstance(link, dict) and link.get("relation") == "next":
                url = link.get("url")
                if not isinstance(url, str) or not url:
                    return None
                base = urlsplit(self.fhir_base_url(fhir_store))
                target = urlsplit(url)
                inside = (target.scheme, target.netloc) == (base.scheme, base.netloc) and (
                    target.path == base.path or target.path.startswith(base.path + "/")
                )
                if not inside:
                    raise MalformedResponseError(
                        "search next page", 200, server_message="next link is outside the store"
                    )
                return self._object("GET", url)
        return None

    def put_resource(
        self, fhir_store: str, resource_type: str, resource_id: str, resource: dict
    ) -> dict:
        """PUT ``<type>/<id>``: update, or create with that id (needs ``enableUpdateCreate``)."""
        if not isinstance(resource, dict) or resource.get("resourceType") != resource_type:
            raise ValueError(f"put_resource needs a {resource_type} resource")
        if resource.get("id") not in (None, resource_id):
            raise ValueError("resource.id differs from the id in the URL")
        url = (
            f"{self.fhir_base_url(fhir_store)}/{_check(_FHIR_TYPE, 'resource type', resource_type)}"
            f"/{_check(_FHIR_ID, 'resource id', resource_id)}"
        )
        return self._object("PUT", url, body=resource, content_type=FHIR_JSON)

    # ---- HL7v2 -------------------------------------------------------------------------------

    def get_message(self, name: str, *, view: str = "RAW_ONLY") -> Hl7v2Message:
        """``messages.get``. ``RAW_ONLY`` returns the exact bytes in ``data`` plus metadata and
        labels (spike S5). Raises :class:`NotFoundError` if the message was deleted."""
        _check(MESSAGE_NAME, "HL7v2 message name", name)
        if view not in MESSAGE_VIEWS:
            raise ValueError(f"view must be one of {', '.join(MESSAGE_VIEWS)}")
        operation = "GET hl7v2 message"
        body = self._object("GET", f"{self._base_url}/{name}", params={"view": view})
        return Hl7v2Message.from_json(operation, body)

    def list_messages(
        self,
        hl7v2_store: str,
        *,
        filter: str | None = None,  # noqa: A002 - the API's own parameter name
        order_by: str | None = None,
        page_size: int | None = None,
        page_token: str | None = None,
        view: str = "BASIC",
    ) -> MessagePage:
        """One page of ``messages.list``.

        ``filter`` uses the store's syntax, e.g. ``labels.application_outcome = "error"`` or
        ``NOT labels.application_outcome:*`` (no outcome yet). ``order_by`` accepts
        ``send_time`` / ``send_time desc`` (``create_time`` is refused, spike S5). The default
        ``BASIC`` view returns names only.
        """
        if view not in MESSAGE_VIEWS:
            raise ValueError(f"view must be one of {', '.join(MESSAGE_VIEWS)}")
        params: dict[str, str] = {"view": view}
        if filter is not None:
            params["filter"] = filter
        if order_by is not None:
            params["orderBy"] = order_by
        if page_size is not None:
            if not 1 <= page_size <= 1000:
                raise ValueError("page_size must be 1 to 1000")
            params["pageSize"] = str(page_size)
        if page_token is not None:
            params["pageToken"] = page_token
        url = f"{self._base_url}/{self.hl7v2_store_name(hl7v2_store)}/messages"
        body = self._object("GET", url, params=params)
        items = body.get("hl7V2Messages", []) or []
        if not isinstance(items, list):
            raise MalformedResponseError("list hl7v2 messages", 200, server_message="bad list")
        token = body.get("nextPageToken") or None
        return MessagePage(
            messages=tuple(Hl7v2Message.from_json("list hl7v2 messages", m) for m in items),
            next_page_token=token if isinstance(token, str) else None,
        )

    def iter_messages(
        self, hl7v2_store: str, *, max_pages: int = 100, **kwargs: object
    ) -> Iterator[Hl7v2Message]:
        """Every message ``list_messages`` would return, following ``nextPageToken``.
        Stops after ``max_pages`` pages so a misbehaving token cannot loop forever."""
        token = None
        for _ in range(max_pages):
            page = self.list_messages(hl7v2_store, page_token=token, **kwargs)  # type: ignore[arg-type]
            yield from page.messages
            token = page.next_page_token
            if token is None:
                return

    def patch_labels(self, name: str, labels: dict[str, str]) -> Hl7v2Message:
        """``messages.patch`` with ``updateMask=labels``.

        The store merges: keys given here are set (an empty value removes the key), other labels
        stay (spike S5). Patching labels publishes no notification. ``labels`` must keep to the
        documented limits (:func:`pulsebridge_gcp.models.check_labels`), checked before sending.
        """
        _check(MESSAGE_NAME, "HL7v2 message name", name)
        check_labels(labels)
        body = self._object(
            "PATCH",
            f"{self._base_url}/{name}",
            params={"updateMask": "labels"},
            body={"labels": dict(labels)},
        )
        return Hl7v2Message.from_json("PATCH hl7v2 message labels", body)

    def ingest(
        self, hl7v2_store: str, data: bytes, *, labels: dict[str, str] | None = None
    ) -> IngestResult:
        """``messages:ingest``: store one message and return the store's ACK.

        A rejected message (HTTP 400 or 409) raises :class:`ClientError` /
        :class:`ConflictError` with ``hl7_nack`` holding the NACK; an API error without a NACK
        (missing store, bad request) has ``hl7_nack = None`` (spike S5).
        """
        if not isinstance(data, bytes):
            raise ValueError("ingest needs the message as bytes")
        message: dict = {"data": base64.b64encode(data).decode("ascii")}
        if labels:
            check_labels(labels)
            message["labels"] = dict(labels)
        url = f"{self._base_url}/{self.hl7v2_store_name(hl7v2_store)}/messages:ingest"
        operation = "POST hl7v2 messages:ingest"
        body = self._object("POST", url, body={"message": message})
        return IngestResult(
            hl7_ack=decode_base64_field(operation, "hl7Ack", body.get("hl7Ack")),
            message=Hl7v2Message.from_json(operation, body.get("message")),
        )


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "MESSAGE_NAME",
    "MESSAGE_VIEWS",
    "HealthcareApiError",
    "HealthcareClient",
    "raise_for_status",
]
