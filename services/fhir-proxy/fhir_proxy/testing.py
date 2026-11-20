"""Test helpers (synthetic data only), like ``pulsebridge_gcp.testing``.

An in-memory fake of the three Cloud Healthcare stores, behind the real
``pulsebridge_gcp.HealthcareClient``; a fake HTTP sender for cuff-sim and health-sync; a fake
identity-token source; fakes of Fraud Defense's assessments API and of the Cloud Storage counter
bucket (``FakeFraudDefense``, ``FakeBucket``), each behind the real client code; an injected
clock. No network beyond 127.0.0.1, no credentials, no GCP.

All data is synthetic. The configuration uses the placeholder names of T31's AuditEvent examples
(``example-project``, ``example-dataset``, ``example-store``, patient ``PB-SYN-0001``), so those
five examples can be loaded into the fake audit store as they are.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from pulsebridge_gcp import HttpResponse, RetryableError, TimedOutError
from pulsebridge_hl7v2 import Patient
from pulsebridge_hl7v2.samples import load as load_sample

from fhir_proxy.app import create_app
from fhir_proxy.config import SamplePatient, ServiceConfig
from fhir_proxy.logs import JsonFormatter

BASE = "https://healthcare.googleapis.com/v1"
PROJECT = "example-project"
LOCATION = "us-central1"
DATASET = "example-dataset"
FHIR_STORE = "main"
AUDIT_STORE = "audit"
HL7_STORE = "example-store"
STORE_NAME = f"projects/{PROJECT}/locations/{LOCATION}/datasets/{DATASET}/hl7V2Stores/{HL7_STORE}"
FHIR_BASE = f"{BASE}/projects/{PROJECT}/locations/{LOCATION}/datasets/{DATASET}/fhirStores"
PATIENT_ID = "PB-SYN-0001"
# Obviously fake signing keys (32 bytes and more): never a real key anywhere in this repository.
SIGNING_KEY = b"synthetic-session-key-for-tests-only-0001"
OTHER_SIGNING_KEY = b"rotated-synthetic-session-key-for-tests-02"
ORIGIN = "https://dashboard.example.test"
ORIGIN_HOST = "dashboard.example.test"
SITE_KEY = "synthetic-site-key-0000000000000000000000"
COUNTER_BUCKET = "example-demo-counters"
CUFF_URL = "https://cuff-sim.example.test"
HEALTH_URL = "https://health-sync.example.test"
PUBLIC_BASE = "https://proxy.example.test"
SAMPLE_PATIENT = SamplePatient("PULSEBRIDGE", "TESTPATIENT", "SYNTHIA")
EXAMPLES = Path(__file__).resolve().parents[3] / "libs" / "pulsebridge_fhir" / "examples"
START = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


def example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text())


def sample_bytes(name: str, patient_id: str = PATIENT_ID) -> bytes:
    """A T07 sample with its PID rebuilt for ``patient_id``, as cuff-sim sends it."""
    sample = load_sample(name)
    return sample.with_patient(
        Patient(
            identifier=patient_id,
            assigning_authority=SAMPLE_PATIENT.assigning_authority,
            family_name=SAMPLE_PATIENT.family_name,
            given_name=SAMPLE_PATIENT.given_name,
        )
    ).payload


# ---- clock -----------------------------------------------------------------------------------


@dataclass
class Clock:
    now: datetime = START
    mono: float = 1000.0

    def __call__(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.mono += seconds


# ---- the fake Cloud Healthcare API --------------------------------------------------------------


def _json(status: int, body: object) -> HttpResponse:
    return HttpResponse(status, json.dumps(body).encode(), {"content-type": "application/json"})


def _instant(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _token_matches(values: str, system: str, code: str) -> bool:
    for one in values.split(","):
        s, _, c = one.partition("|")
        if s == system and c == code:
            return True
    return False


def _date_ok(at: datetime, clause: str) -> bool:
    prefix, value = clause[:2], _instant(clause[2:])
    return {
        "lt": at < value,
        "le": at <= value,
        "gt": at > value,
        "ge": at >= value,
    }[prefix]


@dataclass
class FakeHealthcare:
    """The FHIR and HL7v2 REST surface the proxy uses, in memory. It answers only the requests
    the proxy should send and fails the test on anything else."""

    fhir: dict[str, dict[tuple[str, str], dict]] = field(
        default_factory=lambda: {FHIR_STORE: {}, AUDIT_STORE: {}}
    )
    messages: dict[str, dict] = field(default_factory=dict)
    requests: list[tuple[str, str, list[tuple[str, str]]]] = field(default_factory=list)
    # (method, path regex) -> HttpResponse or exception, checked before the store logic.
    failures: list[tuple[str, str, object]] = field(default_factory=list)
    updated: int = 0
    page_size_override: int | None = None
    # (method, path regex, seconds): answer slowly, to test the request budget.
    delays: list[tuple[str, str, float]] = field(default_factory=list)
    # A store that ignores the subject pin, to test the proxy's own post-filter.
    ignore_subject: bool = False
    # (method, path regex, seconds): answer slowly and IGNORE the caller's timeout, like a
    # stuck connection, so a parallel wait really reaches its deadline first.
    stubborn_delays: list[tuple[str, str, float]] = field(default_factory=list)
    # (path, start, end) of every delayed call, monotonic seconds: for ordering assertions.
    timeline: list[tuple[str, float, float]] = field(default_factory=list)

    # -- seeding --

    def put(self, store: str, resource: dict) -> dict:
        resource = copy.deepcopy(resource)
        self.updated += 1
        resource.setdefault("meta", {})["lastUpdated"] = (
            START + timedelta(seconds=self.updated)
        ).isoformat()
        self.fhir[store][(resource["resourceType"], resource["id"])] = resource
        return resource

    def add_message(
        self,
        name: str,
        data: bytes,
        *,
        labels: dict | None = None,
        create_time: str = "2026-01-01T14:30:30.500000Z",
        patient_ids: list[str] | None = None,
    ) -> None:
        body = {
            "name": name,
            "data": base64.b64encode(data).decode(),
            "createTime": create_time,
            "sendTime": "2026-01-01T14:30:30Z",
            "messageType": "ORU",
        }
        if labels:
            body["labels"] = labels
        if patient_ids is not None:
            body["patientIds"] = [{"value": p, "type": "MR"} for p in patient_ids]
        self.messages[name] = body

    def fail(self, method: str, path_pattern: str, response: object) -> None:
        self.failures.append((method, path_pattern, response))

    def calls(self, method: str, pattern: str) -> list[tuple[str, str, list[tuple[str, str]]]]:
        return [r for r in self.requests if r[0] == method and re.search(pattern, r[1])]

    # -- Transport --

    def request(self, method, url, *, params, body, headers, timeout):  # noqa: ANN001
        if not (timeout > 0):
            raise AssertionError("unexpected request")
        pairs = list(params.items()) if isinstance(params, dict) else list(params or [])
        path = urlsplit(url).path
        if urlsplit(url).query:
            pairs += parse_qsl(urlsplit(url).query)
        self.requests.append((method, path, pairs))
        for delay_method, pattern, seconds in self.delays:
            if delay_method == method and re.search(pattern, path):
                began = time.monotonic()
                time.sleep(min(seconds, timeout))
                self.timeline.append((path, began, time.monotonic()))
                if seconds > timeout:
                    raise TimedOutError(f"{method} {path}", None)
        for delay_method, pattern, seconds in self.stubborn_delays:
            if delay_method == method and re.search(pattern, path):
                time.sleep(seconds)
        for fail_method, pattern, response in self.failures:
            if fail_method == method and re.search(pattern, path):
                if isinstance(response, BaseException):
                    raise response
                return response
        if not (method == "GET"):
            raise AssertionError(f"the proxy must not write: {method} {path}")
        if "/hl7V2Stores/" in path:
            return self._hl7(path, dict(pairs))
        match = re.fullmatch(r".*/fhirStores/([^/]+)/fhir/([A-Za-z]+)(?:/([^/]+))?", path)
        if not (match):
            raise AssertionError(f"unexpected path {path}")
        store, rtype, rid = match.groups()
        if rid is not None:
            found = self.fhir[store].get((rtype, rid))
            return _json(200, found) if found else _json(404, {"resourceType": "OperationOutcome"})
        return _json(200, self._search(store, rtype, pairs))

    def _hl7(self, path: str, params: dict) -> HttpResponse:
        name = path.split("/v1/", 1)[1]
        if name.endswith("/messages"):
            items = sorted(self.messages.values(), key=lambda m: m["sendTime"], reverse=True)
            for clause in (params.get("filter") or "").split(" AND "):
                found = re.fullmatch(r'create_time (>=|<=) "([^"]+)"', clause.strip())
                if clause.strip() and not found:
                    raise AssertionError(f"unexpected filter {clause!r}")
                if found:
                    op, value = found.groups()
                    bound = _instant(value)
                    if op == ">=":
                        items = [m for m in items if _instant(m["createTime"]) >= bound]
                    else:
                        items = [m for m in items if _instant(m["createTime"]) <= bound]
            start = int(params.get("pageToken") or 0)
            size = int(params.get("pageSize", 100))
            page = items[start : start + size]
            out = {"hl7V2Messages": page}
            if start + size < len(items):
                out["nextPageToken"] = str(start + size)
            return _json(200, out)
        found = self.messages.get(name)
        return _json(200, found) if found else _json(404, {"error": {"code": 404}})

    def _search(self, store: str, rtype: str, pairs: list[tuple[str, str]]) -> dict:
        resources = [r for (t, _), r in self.fhir[store].items() if t == rtype]
        params: dict[str, list[str]] = {}
        for k, v in pairs:
            params.setdefault(k, []).append(v)
        allowed = {
            "Patient": {"identifier", "_elements"},
            "Observation": {
                "identifier",
                "subject",
                "code",
                "date",
                "_sort",
                "_count",
                "_total",
                "_elements",
                "_page_token",
            },
            "AuditEvent": {"type", "subtype", "date", "_sort", "_count", "_page_token"},
            "Provenance": {"recorded", "_sort", "_count", "_include"},
        }[rtype]
        if not (set(params) <= allowed):
            raise AssertionError(f"unexpected {rtype} search parameters {set(params)}")
        if "identifier" in params:
            wanted = params["identifier"][0].split(",")
            resources = [
                r
                for r in resources
                if any(
                    f"{i.get('system')}|{i.get('value')}" in wanted for i in r.get("identifier", [])
                )
            ]
        if "subject" in params and not self.ignore_subject:
            refs = params["subject"][0].split(",")
            resources = [r for r in resources if r.get("subject", {}).get("reference") in refs]
        if "code" in params:
            system, _, code = params["code"][0].partition("|")
            resources = [
                r
                for r in resources
                if any(
                    c.get("system") == system and c.get("code") == code
                    for c in r.get("code", {}).get("coding", [])
                )
            ]
        if rtype == "AuditEvent":
            for key in ("type", "subtype"):
                value = params[key][0]
                resources = [
                    r
                    for r in resources
                    if any(
                        _token_matches(value, c.get("system"), c.get("code"))
                        for c in ([r["type"]] if key == "type" else r.get("subtype", []))
                    )
                ]
            for clause in params.get("date", []):
                resources = [r for r in resources if _date_ok(_instant(r["recorded"]), clause)]
            resources.sort(key=lambda r: _instant(r["recorded"]), reverse=True)
        if rtype == "Provenance":
            for clause in params.get("recorded", []):
                resources = [r for r in resources if _date_ok(_instant(r["recorded"]), clause)]
            resources.sort(key=lambda r: (_instant(r["recorded"]), r["id"]), reverse=True)
        if rtype == "Observation":
            # The fake applies NO date filter on purpose: the proxy's own R4 rule is under test.
            sort = params.get("_sort", ["-_lastUpdated"])[0]
            if sort == "-_lastUpdated":
                resources.sort(key=lambda r: r["meta"]["lastUpdated"], reverse=True)
            else:
                resources.sort(
                    key=lambda r: (
                        (r.get("effectivePeriod") or {}).get("start")
                        or r.get("effectiveDateTime", "")
                    ),
                    reverse=sort.startswith("-"),
                )
        # As Cloud Healthcare: ``_summary`` is refused (it is not in the allowed set above); a
        # count is ``_total=accurate`` with ``_count=0``, which returns the total and no entries.
        count = int(params.get("_count", ["100"])[0])
        if count == 0:
            if params.get("_total") != ["accurate"]:
                raise AssertionError("_count=0 without _total=accurate gives an estimated total")
            return {"resourceType": "Bundle", "type": "searchset", "total": len(resources)}
        if self.page_size_override:
            count = min(count, self.page_size_override)
        start = int((params.get("_page_token") or ["0"])[0])
        page = resources[start : start + count]
        bundle: dict = {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": len(resources),
            "entry": [
                {"fullUrl": f"{FHIR_BASE}/{store}/fhir/{rtype}/{r['id']}", "resource": r}
                for r in page
            ],
        }
        if params.get("_include") == ["Provenance:target"]:
            for r in page:
                for target in r.get("target", []):
                    kind, _, rid = target.get("reference", "").partition("/")
                    found = self.fhir[store].get((kind, rid))
                    if found is not None:
                        bundle["entry"].append({"resource": found, "search": {"mode": "include"}})
        if start + count < len(resources):
            query = [(k, v) for k, v in pairs if k != "_page_token"]
            query.append(("_page_token", str(start + count)))
            bundle["link"] = [
                {
                    "relation": "next",
                    "url": f"{FHIR_BASE}/{store}/fhir/{rtype}?{urlencode(query)}",
                }
            ]
        return bundle


def _same_identifier(fake: FakeHealthcare, rtype: str, resource: dict) -> dict | None:
    wanted = {(i.get("system"), i.get("value")) for i in resource.get("identifier", [])}
    for (t, _), stored in fake.fhir[FHIR_STORE].items():
        have = {(i.get("system"), i.get("value")) for i in stored.get("identifier", [])}
        if t == rtype and wanted & have:
            return stored
    return None


def apply_transaction(fake: FakeHealthcare, bundle: dict, *, prefix: str = "srv") -> dict:
    """Store an ingest Bundle as a FHIR server would: give each new resource a server id,
    rewrite ``urn:uuid`` references to ``Type/id``. Returns fullUrl -> reference."""
    ids: dict[str, str] = {}
    existing: set[str] = set()
    for n, entry in enumerate(bundle["entry"]):
        resource = entry["resource"]
        rtype = resource["resourceType"]
        request = entry.get("request", {})
        conditional = request.get("ifNoneExist") or (
            request.get("method") == "PUT" and "?" in request.get("url", "")
        )
        match = _same_identifier(fake, rtype, resource) if conditional else None
        if match is not None:
            # Conditional create (D11): the existing resource is kept as it is. Conditional
            # update: the existing resource keeps its id and gets the new content.
            ids[entry["fullUrl"]] = f"{rtype}/{match['id']}"
            if request.get("ifNoneExist"):
                existing.add(entry["fullUrl"])
            continue
        rid = resource.get("id") or f"{prefix}-{rtype.lower()}-{n}"
        ids[entry["fullUrl"]] = f"{rtype}/{rid}"

    def rewrite(value: object) -> object:
        if isinstance(value, dict):
            return {
                k: (ids.get(v, v) if k == "reference" and isinstance(v, str) else rewrite(v))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [rewrite(v) for v in value]
        return value

    for entry in bundle["entry"]:
        if entry["fullUrl"] in existing:
            continue
        resource = rewrite(copy.deepcopy(entry["resource"]))
        resource["id"] = ids[entry["fullUrl"]].split("/", 1)[1]
        fake.put(FHIR_STORE, resource)
    return ids


# ---- the fake upstream services --------------------------------------------------------------


@dataclass
class FakeSender:
    """cuff-sim and health-sync: answers by path, records every call."""

    routes: dict[str, object] = field(default_factory=dict)
    calls: list[dict] = field(default_factory=list)

    def answer(self, path: str, status: int, body: bytes | object, **headers: str) -> None:
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        headers = {k.lower().replace("_", "-"): v for k, v in headers.items()}
        headers.setdefault("content-type", "application/json")
        self.routes[path] = (status, headers, body)

    def __call__(self, method, url, *, headers, body, timeout):  # noqa: ANN001
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        response = self.routes.get(urlsplit(url).path)
        if response is None:
            raise AssertionError(f"no fake upstream for {url}")
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(timeout=timeout, body=body)
        return response


def slow_answer(  # noqa: ANN201
    seconds: float,
    status: int,
    answer_body: object,
    timeline: list[tuple[str, float, float]] | None = None,
):
    """A fake upstream answer that takes ``seconds``, honouring the caller's timeout as
    ``requests`` would (a timeout is ``UpstreamTimeout``)."""
    from fhir_proxy.upstream import UpstreamTimeout

    payload = json.dumps(answer_body).encode()

    def answer(*, timeout: float, body: bytes | None) -> tuple:  # noqa: ARG001
        began = time.monotonic()
        time.sleep(min(seconds, timeout))
        if timeline is not None:
            timeline.append(("upstream", began, time.monotonic()))
        if seconds > timeout:
            raise UpstreamTimeout()
        return status, {"content-type": "application/json"}, payload

    return answer


class FakeIdTokens:
    def __init__(self) -> None:
        self.audiences: list[str] = []
        self.timeouts: list[float] = []
        self.delay = 0.0  # seconds a fetch takes, to test the budget

    def token(self, audience: str, timeout: float = 10.0) -> str:
        self.timeouts.append(timeout)
        if self.delay:
            time.sleep(min(self.delay, timeout))
        self.audiences.append(audience)
        return f"fake-identity-token-for-{audience.split('//')[1]}"


# ---- the app ---------------------------------------------------------------------------------


# ---- Fraud Defense (reCAPTCHA Enterprise) assessments --------------------------------------------


class FakeFraudDefense:
    """``POST https://recaptchaenterprise.googleapis.com/v1/projects/<project>/assessments`` as
    the REST reference describes it, behind the proxy's real ``FraudDefense`` client.

    :meth:`issue` stands in for the browser's ``grecaptcha.enterprise.execute``: it returns a
    synthetic token and what Google will say about it. As the reference documents, a bad token
    is still a ``200`` assessment with ``valid`` false: a token is good for one assessment (a
    second one says ``DUPE``), an unknown token is ``MALFORMED``, and a request whose ``siteKey``
    is not the key the token was made with says ``KEY_MISMATCH``. A request the API cannot parse
    is ``400``. ``failure`` makes every call fail: ``"timeout"``, ``"unreachable"``, an HTTP
    status, or ``"not-json"``. The answer leaves out default values, as protobuf's JSON form
    does (no ``valid`` when false, no ``score`` when 0.0)."""

    def __init__(self, project: str = PROJECT, site_key: str = SITE_KEY) -> None:
        self.url = f"https://recaptchaenterprise.googleapis.com/v1/projects/{project}/assessments"
        self.site_key = site_key
        self.requests: list[dict] = []
        self.failure: str | int | None = None
        self._tokens: dict[str, dict] = {}
        self._used: set[str] = set()
        self._lock = threading.Lock()

    def issue(
        self,
        *,
        score: float = 0.9,
        action: str = "view_demo",
        hostname: str = ORIGIN_HOST,
        valid: bool = True,
        invalid_reason: str = "EXPIRED",
    ) -> str:
        token = "03" + secrets.token_urlsafe(300)  # the shape and about the length of a real one
        self._tokens[token] = {
            "score": score,
            "action": action,
            "hostname": hostname,
            "valid": valid,
            "invalid_reason": invalid_reason,
        }
        return token

    @property
    def assessments(self) -> int:
        return len(self.requests)

    def request(self, method, url, *, params, body, headers, timeout):  # noqa: ANN001, ANN201
        with self._lock:
            try:
                event = json.loads(body.decode())["event"]
            except (AttributeError, ValueError, KeyError, TypeError):
                event = None
            self.requests.append(
                {"method": method, "url": url, "params": params, "event": event, "timeout": timeout}
            )
            if self.failure == "timeout":
                raise TimedOutError(f"{method} {url}", None)
            if self.failure == "unreachable":
                raise RetryableError(f"{method} {url}", None)
            if self.failure == "not-json":
                return HttpResponse(200, b"<html>", {"content-type": "text/html"})
            if isinstance(self.failure, int):
                return _json(self.failure, {"error": {"code": self.failure}})
            if (
                method != "POST"
                or url != self.url
                or params
                or headers.get("Content-Type") != "application/json"
                or not isinstance(event, dict)
                or set(event) - {"token", "siteKey", "expectedAction"}
            ):
                return _json(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT"}})
            token = event.get("token", "")
            facts = self._tokens.get(token)
            name = f"projects/123456789/assessments/{secrets.token_hex(8)}"
            props: dict = {"createTime": "2026-01-02T12:00:00.000Z"}
            answer: dict = {"name": name, "event": event, "tokenProperties": props}
            if facts is None:
                reason = "MALFORMED"
            elif event.get("siteKey") != self.site_key:
                reason = "KEY_MISMATCH"
            elif token in self._used:
                reason = "DUPE"
            elif not facts["valid"]:
                reason = facts["invalid_reason"]
            else:
                reason = None
            if reason is not None:
                props["invalidReason"] = reason
                return _json(200, answer)
            self._used.add(token)
            props.update(
                {
                    "valid": True,
                    "invalidReason": "INVALID_REASON_UNSPECIFIED",
                    "hostname": facts["hostname"],
                    "action": facts["action"],
                }
            )
            risk: dict = {"reasons": []}
            if facts["score"]:
                risk["score"] = facts["score"]
            answer["riskAnalysis"] = risk
            return _json(200, answer)


# ---- the Cloud Storage counter bucket ----------------------------------------------------------


class FakeBucket:
    """Cloud Storage's JSON API for one bucket, as far as ``DailyCounter`` uses it:
    ``objects.get`` (metadata) and a multipart ``objects.insert`` with ``ifGenerationMatch``.
    Every write gets a new generation; a precondition that does not hold answers ``412`` and
    writes nothing, as Cloud Storage does.

    ``before_write(name)`` runs inside each insert before the precondition is checked: a test
    uses it to let "another instance" write in between (the race). ``fail_reads`` /
    ``fail_writes`` make those calls answer that status, or raise for ``"timeout"`` /
    ``"unreachable"``."""

    def __init__(self, bucket: str = COUNTER_BUCKET) -> None:
        self.bucket = bucket
        self.objects: dict[str, dict] = {}
        self.requests: list[tuple[str, str]] = []
        self.before_write: Callable[[str], None] | None = None
        self.fail_reads: str | int | None = None
        self.fail_writes: str | int | None = None
        self._generation = 1_700_000_000_000_000
        self._lock = threading.RLock()

    def count(self, name: str) -> int | None:
        found = self.objects.get(name)
        return None if found is None else int(found["metadata"]["count"])

    def put(self, name: str, count: str) -> None:
        """Write directly (what another instance's increment leaves behind)."""
        with self._lock:
            self._generation += 1
            self.objects[name] = {"generation": str(self._generation), "metadata": {"count": count}}

    def _fail(self, failure: str | int | None, what: str) -> HttpResponse | None:
        if failure == "timeout":
            raise TimedOutError(what, None)
        if failure == "unreachable":
            raise RetryableError(what, None)
        if isinstance(failure, int):
            return _json(failure, {"error": {"code": failure}})
        return None

    def request(self, method, url, *, params, body, headers, timeout):  # noqa: ANN001, ANN201, ARG002
        self.requests.append((method, url))
        get_prefix = f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/"
        insert_url = f"https://storage.googleapis.com/upload/storage/v1/b/{self.bucket}/o"
        if method == "GET" and url.startswith(get_prefix):
            failed = self._fail(self.fail_reads, "get")
            if failed is not None:
                return failed
            from urllib.parse import unquote

            encoded = url[len(get_prefix) :]
            if "/" in encoded:
                return _json(404, {"error": {"code": 404}})  # a "/" in the name must be %2F
            with self._lock:
                found = self.objects.get(unquote(encoded))
                if found is None:
                    return _json(404, {"error": {"code": 404}})
                return _json(
                    200,
                    {"kind": "storage#object", "name": unquote(encoded), "bucket": self.bucket}
                    | copy.deepcopy(found),
                )
        if method == "POST" and url == insert_url:
            failed = self._fail(self.fail_writes, "insert")
            if failed is not None:
                return failed
            params = dict(params or {})
            boundary = headers.get("Content-Type", "").partition("boundary=")[2]
            if params.get("uploadType") != "multipart" or not boundary:
                return _json(400, {"error": {"code": 400}})
            parts = body.decode().split(f"--{boundary}")
            resource = json.loads(parts[1].split("\r\n\r\n", 1)[1].rstrip("\r\n"))
            name = resource["name"]
            if self.before_write is not None:
                self.before_write(name)
            with self._lock:
                current = self.objects.get(name)
                expected = params.get("ifGenerationMatch")
                actual = "0" if current is None else current["generation"]
                if expected is None or expected != actual:
                    return _json(412, {"error": {"code": 412, "message": "conditionNotMet"}})
                self._generation += 1
                self.objects[name] = {
                    "generation": str(self._generation),
                    "metadata": dict(resource.get("metadata") or {}),
                }
                return _json(200, {"name": name, "generation": str(self._generation)})
        return _json(404, {"error": {"code": 404}})


# ---- the app ---------------------------------------------------------------------------------


@dataclass
class Harness:
    """The app over fakes. ``client`` keeps no cookie jar: every request states its own cookie
    (``auth()``), so a test sees exactly what the browser would send."""

    app: object
    client: object
    fake: FakeHealthcare
    sender: FakeSender
    tokens: FakeIdTokens
    clock: Clock
    key_file: Path
    config: ServiceConfig
    fraud: FakeFraudDefense = field(default_factory=FakeFraudDefense)
    bucket: FakeBucket = field(default_factory=FakeBucket)
    _session: str | None = field(default=None, repr=False)

    def start_session(self, token: str | None = None, **headers: str):  # noqa: ANN201
        """``POST /auth/session`` as the page sends it (same origin, a fresh bot-check token)."""
        return self.client.post(
            f"{self.config.path_prefix}/auth/session",
            json={"recaptcha_token": token if token is not None else self.fraud.issue()},
            headers={"Origin": ORIGIN, **headers},
        )

    def login(self, **headers: str) -> str:
        """A new demo session's token, taken from its ``Set-Cookie``."""
        response = self.start_session(**headers)
        if not (response.status_code == 200):
            raise AssertionError(response.get_data(as_text=True))
        return session_from(response)

    def auth(self, token: str | None = None) -> dict:
        """What the page's same-origin requests carry: the cookie and, as every browser does on
        a POST, the page's Origin. Without ``token``, one session per harness is started on
        first use and reused, as a browser keeps its cookie (a new session per request would
        run into the per-address limit, which counts every attempt)."""
        if token is None:
            if self._session is None:
                self._session = self.login()
            token = self._session
        return {"Cookie": f"__session={token}", "Origin": ORIGIN}

    def get(self, path: str, **kwargs):  # noqa: ANN201
        headers = kwargs.pop("headers", None) or self.auth()
        return self.client.get(path, headers=headers, **kwargs)


def session_from(response) -> str:  # noqa: ANN001
    """The token in a response's ``__session`` Set-Cookie (exactly one is expected)."""
    cookies = response.headers.getlist("Set-Cookie")
    name, _, rest = (cookies[0] if len(cookies) == 1 else "").partition("=")
    if name != "__session":
        raise AssertionError(f"expected one __session cookie, got {cookies}")
    return rest.split(";", 1)[0]


GENEROUS_LIMITS: dict[str, int] = {
    "requests_per_address_per_minute": 6000,
    "reads_per_session_per_minute": 1200,
    "cuff_sends_per_session_per_10_minutes": 30,
    "shares_per_session_per_10_minutes": 20,
    "shares_per_instance_per_hour": 600,
}
# The production defaults (ServiceConfig's, which are VARIABLES' defaults).
DEMO_LIMITS: dict[str, int] = {
    "requests_per_address_per_minute": 300,
    "reads_per_session_per_minute": 120,
    "cuff_sends_per_session_per_10_minutes": 3,
    "shares_per_session_per_10_minutes": 2,
    "shares_per_instance_per_hour": 30,
}


def make_config(tmp_path: Path, **overrides: object) -> ServiceConfig:
    key_file = tmp_path / "session-signing-key"
    if not key_file.exists():
        key_file.write_bytes(SIGNING_KEY)
    values = dict(
        project_id=PROJECT,
        location=LOCATION,
        dataset_id=DATASET,
        fhir_store_id=FHIR_STORE,
        audit_fhir_store_id=AUDIT_STORE,
        hl7v2_store_id=HL7_STORE,
        patient_id=PATIENT_ID,
        session_key_file=str(key_file),
        allowed_origins=(ORIGIN,),
        cuff_sim_url=CUFF_URL,
        recaptcha_site_key=SITE_KEY,
        demo_counter_bucket=COUNTER_BUCKET,
        health_sync_url=HEALTH_URL,
        sample_patient=SAMPLE_PATIENT,
        status_cache_seconds=60,
        fhir_timeout=5,
        # T46's rate limits at the top of their ranges, so a test that is not about them can make
        # as many requests as it needs; tests/test_rate_limits.py uses the defaults (DEMO_LIMITS).
        **GENEROUS_LIMITS,
    )
    values.update(overrides)
    values.setdefault("public_base_url", PUBLIC_BASE + str(values.get("path_prefix", "")))
    return ServiceConfig(**values)


def build_harness(
    tmp_path: Path,
    extension_module: str = "fhir_proxy.share",
    sender: FakeSender | None = None,
    **overrides,
):
    config = make_config(tmp_path, **overrides)
    fake = FakeHealthcare()
    sender = sender or FakeSender()
    tokens = FakeIdTokens()
    clock = Clock()
    fraud = FakeFraudDefense()
    bucket = FakeBucket()
    app = create_app(
        config,
        fake,
        id_tokens=tokens,
        send=sender,
        recaptcha_transport=fraud,
        storage_transport=bucket,
        clock=clock,
        monotonic=clock.monotonic,
        extension_module=extension_module,
    )
    return Harness(
        app,
        app.test_client(use_cookies=False),
        fake,
        sender,
        tokens,
        clock,
        Path(config.session_key_file),
        config,
        fraud,
        bucket,
    )


class LogCapture(logging.Handler):
    """Every log record, both as the JSON line production writes and with a full standard
    traceback, so a test can check that no value appears in either."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []
        self.plain = logging.Formatter("%(name)s %(levelname)s %(message)s")
        self.json = JsonFormatter()

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.json.format(record))
        self.lines.append(self.plain.format(record))

    def text(self) -> str:
        return "\n".join(self.lines)

    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.lines if line.startswith("{")]


class EchoSender(FakeSender):
    """cuff-sim answering with the length and SHA-256 of the body it received, so a test can see
    exactly what was forwarded."""

    def __call__(self, method, url, *, headers, body, timeout):  # noqa: ANN001
        import hashlib

        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        answer = {"received": len(body or b""), "sha256": hashlib.sha256(body or b"").hexdigest()}
        return 200, {"content-type": "application/json"}, json.dumps(answer).encode()


def gunicorn_test_app():  # noqa: ANN201
    """A WSGI app for a test that runs a real gunicorn on 127.0.0.1 (synthetic config, fake
    stores, cuff-sim replaced by :class:`EchoSender`). The directory comes from the
    ``FHIR_PROXY_TEST_DIR`` environment variable; nothing else is read from the environment."""
    import os

    return build_harness(Path(os.environ["FHIR_PROXY_TEST_DIR"]), sender=EchoSender()).app
