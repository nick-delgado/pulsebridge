"""Test helpers (synthetic data only), like ``pulsebridge_gcp.testing``.

An in-memory fake of the three Cloud Healthcare stores, behind the real
``pulsebridge_gcp.HealthcareClient``; a fake HTTP sender for cuff-sim and health-sync; a fake
identity-token source; an injected clock. No network beyond 127.0.0.1, no credentials, no GCP.

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
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from pulsebridge_gcp import HttpResponse, TimedOutError
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
# Obviously fake: never a real password anywhere in this repository.
PASSWORD = "synthetic-reviewer-password-for-tests-only"  # noqa: S105 - synthetic
OTHER_PASSWORD = "rotated-synthetic-password-for-tests-only"  # noqa: S105 - synthetic
ORIGIN = "https://dashboard.example.test"
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
                "_summary",
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
        if params.get("_summary") == ["count"]:
            return {"resourceType": "Bundle", "type": "searchset", "total": len(resources)}
        count = int(params.get("_count", ["100"])[0])
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


@dataclass
class Harness:
    app: object
    client: object
    fake: FakeHealthcare
    sender: FakeSender
    tokens: FakeIdTokens
    clock: Clock
    password_file: Path
    config: ServiceConfig

    def login(self, password: str = PASSWORD, **headers: str) -> str:
        response = self.client.post("/auth/login", json={"password": password}, headers=headers)
        if not (response.status_code == 200):
            raise AssertionError(response.get_data(as_text=True))
        return response.get_json()["token"]

    def auth(self, token: str | None = None) -> dict:
        return {"Authorization": f"Bearer {token or self.login()}"}

    def get(self, path: str, **kwargs):  # noqa: ANN201
        headers = kwargs.pop("headers", None) or self.auth()
        return self.client.get(path, headers=headers, **kwargs)


def make_config(tmp_path: Path, **overrides: object) -> ServiceConfig:
    password_file = tmp_path / "reviewer-password"
    if not password_file.exists():
        password_file.write_text(PASSWORD + "\n")
    values = dict(
        project_id=PROJECT,
        location=LOCATION,
        dataset_id=DATASET,
        fhir_store_id=FHIR_STORE,
        audit_fhir_store_id=AUDIT_STORE,
        hl7v2_store_id=HL7_STORE,
        patient_id=PATIENT_ID,
        password_file=str(password_file),
        allowed_origins=(ORIGIN,),
        cuff_sim_url=CUFF_URL,
        health_sync_url=HEALTH_URL,
        sample_patient=SAMPLE_PATIENT,
        password_cache_seconds=0,
        status_cache_seconds=60,
        fhir_timeout=5,
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
    app = create_app(
        config,
        fake,
        id_tokens=tokens,
        send=sender,
        clock=clock,
        monotonic=clock.monotonic,
        extension_module=extension_module,
    )
    return Harness(
        app, app.test_client(), fake, sender, tokens, clock, Path(config.password_file), config
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
