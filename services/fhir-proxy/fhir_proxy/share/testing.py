"""Test helpers for the share (synthetic data only): a fake OpenEMR that behaves as spike S1
recorded OpenEMR 8.5.0 behaving, reachable through an injected transport or over real TLS on
127.0.0.1, plus keys and certificates generated at test time (none is ever committed).

What the fake does, and where it comes from (docs/spikes/s1.md):

- ``POST /oauth2/default/token``: client credentials with a ``private_key_jwt`` assertion,
  verified against the registered public key (RS384, the ``kid``, ``aud`` = the token URL,
  ``iss`` = ``sub`` = the client ID, ``exp``, a ``jti`` never seen before); ``invalid_client``
  otherwise. 300-second tokens, no refresh token; unregistered scopes silently left out.
- ``GET Patient?identifier=<value>``: exact match on the value, the system ignored. ``POST
  Patient`` ignores ``If-None-Exist`` (a repeat makes a duplicate), keeps the identifier only
  when it is ``PT``-typed, requires an official name, gender and birth date; answers
  ``{"pid", "uuid"}``.
- ``POST Encounter`` (status, class, subject, period) answers ``{"euuid", "encounter", "pid"}``;
  ``GET Encounter?patient=&date=`` finds a patient's Encounters whose ``period.start`` is on
  that day (OpenEMR's ``date`` search is on the encounter's date).
- ``POST Observation``, as OpenEMR 8.5.0's ``FhirObservationVitalsService`` reads it: needs
  ``category``, an ``encounter`` of the same patient and ``effectiveDateTime``
  (``effectivePeriod`` is never read); a second reading with the same code, Encounter and
  ``effectiveDateTime`` is refused with 8.5.0's text ``A "<code>" reading already exists for
  this encounter at this effectiveDateTime (Observation/<uuid>); POST does not overwrite ...``
  (the id only when mapped: ``name_duplicates``). Answers ``{"uuid"}``. It keeps no ``device``,
  ``identifier`` or ``performer``, skips component codes it has no column for (a heart rate's
  min and max), and adds its own ``id``, ``meta.profile`` and ``type`` on references. From S1
  and from reading the 8.5.0 source; not verified live.
- Reads by id; ``GET /meta/health/readyz``.
"""

from __future__ import annotations

import copy
import datetime as dt
import http.server
import ipaddress
import json
import re
import ssl
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from fhir_proxy.app import create_app
from fhir_proxy.config import OPENEMR_FHIR_PATH, OPENEMR_TOKEN_PATH, OpenEmrSettings
from fhir_proxy.share.transport import Answer, OpenEmrTimeout, OpenEmrUnreachable
from fhir_proxy.testing import (
    FHIR_STORE,
    PATIENT_ID,
    Clock,
    FakeHealthcare,
    FakeIdTokens,
    FakeSender,
    Harness,
    example,
    make_config,
)

SCOPES = (
    "system/Patient.read system/Patient.write system/Encounter.read system/Encounter.write "
    "system/Observation.read system/Observation.write"
)
CLIENT_ID = "synthetic-client-id-0001"
KID = "synthetic-kid-0001"
V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"


# ---- keys and certificates, generated at test time ---------------------------------------------


def make_key(bits: int = 2048) -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def key_pem(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def make_certificate(ips: list[str], key: rsa.RSAPrivateKey | None = None) -> tuple[str, str]:
    """A self-signed server certificate for ``ips``, shaped as module openemr's (accepted by
    Python 3.13's strict verification as its own trust anchor). Returns (cert PEM, key PEM)."""
    key = key or make_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-openemr")])
    now = dt.datetime.now(dt.UTC)
    public = key.public_key()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(i)) for i in ips]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(public), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii"), key_pem(key)


def settings_for(site: str, client_file: str, scopes: str = SCOPES, timeout: float = 5.0):
    """OpenEmrSettings as config.openemr_settings builds them from the environment."""
    from fhir_proxy.config import openemr_settings

    values = {
        "OPENEMR_SITE_ADDRESS": site,
        "OPENEMR_TOKEN_URL": site + OPENEMR_TOKEN_PATH,
        "OPENEMR_FHIR_BASE_URL": site + OPENEMR_FHIR_PATH,
        "OPENEMR_SCOPES": scopes,
        "OPENEMR_CLIENT_FILE": client_file,
        "OPENEMR_TIMEOUT_SECONDS": str(timeout),
    }
    settings = openemr_settings(values)
    if settings is None:  # pragma: no cover - a test helper
        raise AssertionError("settings expected")
    return settings


def client_document(
    settings: OpenEmrSettings,
    *,
    key: rsa.RSAPrivateKey,
    certificate_pem: str,
    client_id: str = CLIENT_ID,
    kid: str = KID,
    **overrides: object,
) -> dict:
    """The document module openemr stores (its README, "What fhir-proxy gets")."""
    doc: dict = {
        "format": "pulsebridge.openemr-client.v1",
        "client_id": client_id,
        "token_endpoint_auth_method": "private_key_jwt",
        "kid": kid,
        "alg": "RS384",
        "private_key": key_pem(key),
        "site_addr_oath": settings.site_address,
        "token_url": settings.token_url,
        "fhir_base_url": settings.fhir_base_url,
        "scope": " ".join(settings.scopes),
        "server_certificate_pem": certificate_pem,
        "created": "2026-01-01T00:00:00Z",
    }
    doc.update(overrides)
    return doc


# ---- the fake OpenEMR --------------------------------------------------------------------------


def _bundle(site: str, rtype: str, resources: list[dict]) -> dict:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "link": [{"relation": "self", "url": f"{site}{OPENEMR_FHIR_PATH}/{rtype}"}],
        "entry": [
            {"fullUrl": f"{site}{OPENEMR_FHIR_PATH}/{rtype}/{r['id']}", "resource": r}
            for r in resources
        ],
    }


def _utc(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(dt.UTC) if parsed.tzinfo else None


@dataclass
class Client:
    public_key: object
    kid: str
    scopes: tuple[str, ...]


@dataclass
class FakeOpenEmr:
    site: str
    token_url: str
    clients: dict[str, Client] = field(default_factory=dict)
    patients: dict[str, dict] = field(default_factory=dict)
    encounters: dict[str, dict] = field(default_factory=dict)
    observations: dict[str, dict] = field(default_factory=dict)
    duplicates: dict[tuple[str, str, str], str] = field(default_factory=dict)
    tokens: dict[str, float] = field(default_factory=dict)
    seen_jti: set[str] = field(default_factory=set)
    requests: list[dict] = field(default_factory=list)
    # (method, path regex, answer or exception or callable) checked before the normal logic;
    # each entry is used ``times`` times (None: always).
    overrides: list[list] = field(default_factory=list)
    token_lifetime: int = 300
    name_duplicates: bool = True
    counter: int = 0
    monotonic: Callable[[], float] = time.monotonic
    # Wall time for the assertion's iat/exp (the proxy signs with the same injected clock).
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def register(self, client_id: str, key: rsa.RSAPrivateKey, kid: str, scopes: str) -> None:
        self.clients[client_id] = Client(key.public_key(), kid, tuple(scopes.split()))

    def override(self, method: str, pattern: str, answer: object, times: int | None = None) -> None:
        self.overrides.append([method, pattern, answer, times])

    def _uuid(self) -> str:
        self.counter += 1
        return f"a337dc1b-0000-4000-8000-{self.counter:012d}"

    def calls(self, method: str | None = None, pattern: str = "") -> list[dict]:
        return [
            r
            for r in self.requests
            if (method is None or r["method"] == method) and re.search(pattern, r["path"])
        ]

    # -- dispatch --

    def handle(
        self, method: str, path: str, query: list[tuple[str, str]], headers: dict, body: bytes
    ) -> Answer:
        with self.lock:
            self.requests.append(
                {
                    "method": method,
                    "path": path,
                    "query": list(query),
                    "headers": {k.lower(): v for k, v in headers.items()},
                    "body": body,
                }
            )
            for entry in self.overrides:
                o_method, pattern, answer, times = entry
                if o_method == method and re.search(pattern, path) and times != 0:
                    if times is not None:
                        entry[3] = times - 1
                    if isinstance(answer, BaseException):
                        raise answer
                    if callable(answer):
                        return answer(method, path, query, headers, body)
                    return answer
            return self._route(method, path, dict(query), headers, body)

    @staticmethod
    def _json(status: int, body: object) -> Answer:
        return Answer(status, {"content-type": "application/json"}, json.dumps(body).encode())

    def _route(self, method, path, query, headers, body) -> Answer:  # noqa: ANN001, PLR0911
        if path == "/meta/health/readyz" and method == "GET":
            return Answer(200, {"content-type": "text/plain"}, b"ready")
        if path == OPENEMR_TOKEN_PATH and method == "POST":
            return self._token(body)
        auth = {k.lower(): v for k, v in headers.items()}.get("authorization", "")
        token = auth.removeprefix("Bearer ")
        if self.tokens.get(token, -1) < self.monotonic():
            return self._json(401, {"error": "invalid_token"})
        fhir = re.fullmatch(
            rf"{OPENEMR_FHIR_PATH}/(Patient|Encounter|Observation)(?:/([^/]+))?", path
        )
        if fhir is None:
            return self._json(404, {"message": "not found"})
        rtype, rid = fhir.groups()
        if method == "GET" and rid is not None:
            store = {"Patient": self.patients, "Encounter": self.encounters}.get(
                rtype, self.observations
            )
            found = store.get(rid)
            return self._json(200, found) if found else self._json(404, {"message": "missing"})
        if method == "GET":
            return self._search(rtype, query)
        if method == "POST" and rid is None:
            try:
                resource = json.loads(body.decode())
            except ValueError:
                return self._json(400, {"message": "not JSON"})
            return {"Patient": self._create_patient, "Encounter": self._create_encounter}.get(
                rtype, self._create_observation
            )(resource)
        return self._json(405, {"message": "no"})

    def _token(self, body: bytes) -> Answer:
        form = dict(parse_qsl(body.decode()))
        bad = self._json(401, {"error": "invalid_client", "error_description": "refused"})
        if form.get("grant_type") != "client_credentials":
            return self._json(400, {"error": "unsupported_grant_type"})
        if form.get("client_assertion_type") != (
            "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        ):
            return self._json(400, {"error": "invalid_request"})
        assertion = form.get("client_assertion", "")
        try:
            header = jwt.get_unverified_header(assertion)
            claims = jwt.decode(assertion, options={"verify_signature": False})
            client = self.clients.get(claims.get("iss"))
            if client is None or header.get("kid") != client.kid or header.get("alg") != "RS384":
                return bad
            verified = jwt.decode(
                assertion,
                client.public_key,
                algorithms=["RS384"],
                audience=self.token_url,
                issuer=claims.get("iss"),
                options={
                    "require": ["exp", "iat", "jti", "iss", "sub", "aud"],
                    "verify_exp": False,  # checked below against the injected clock
                    "verify_iat": False,
                },
            )
        except jwt.PyJWTError:
            return bad
        now = self.clock().timestamp()
        if not verified["iat"] - 30 <= now < verified["exp"]:
            return bad  # expired, or issued in the future
        if verified["sub"] != verified["iss"] or verified["exp"] - verified["iat"] > 300:
            return bad
        if verified["jti"] in self.seen_jti:
            return bad  # a replayed assertion
        self.seen_jti.add(verified["jti"])
        granted = [s for s in form.get("scope", "").split() if s in client.scopes]
        self.counter += 1
        token = f"synthetic-openemr-access-token-{self.counter:04d}"
        self.tokens[token] = self.monotonic() + self.token_lifetime
        return self._json(
            200,
            {
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": self.token_lifetime,
                "scope": " ".join(granted),
            },
        )

    def _search(self, rtype: str, query: dict) -> Answer:
        if rtype == "Patient":
            value = query.get("identifier", "").split("|")[-1]
            found = [
                p
                for p in self.patients.values()
                if any(i.get("value") == value for i in p.get("identifier", []))
            ]
            return self._json(200, _bundle(self.site, "Patient", found))
        if rtype == "Encounter":
            day = dt.date.fromisoformat(query["date"])
            found = []
            for e in self.encounters.values():
                # OpenEMR searches Encounter ``date`` on the encounter's own date, which is
                # ``period.start`` (FhirEncounterService); UTC here.
                start = _utc(e["period"]["start"])
                if (
                    e["subject"]["reference"] == f"Patient/{query.get('patient')}"
                    and start is not None
                    and start.date() == day
                ):
                    found.append(e)
            return self._json(200, _bundle(self.site, "Encounter", found))
        return self._json(200, _bundle(self.site, "Observation", []))

    def _create_patient(self, resource: dict) -> Answer:
        names = [n for n in resource.get("name", []) if n.get("use") == "official"]
        errors = {}
        if not names or not names[0].get("given"):
            errors["fname"] = {"NotEmpty::EMPTY_VALUE": "First Name must not be empty"}
        if not resource.get("gender"):
            errors["sex"] = {"NotEmpty::EMPTY_VALUE": "Gender must not be empty"}
        if errors:
            return self._json(400, {"validationErrors": errors})
        uuid = self._uuid()
        kept = [
            {"use": i.get("use"), "type": i["type"], "system": V2_0203, "value": i["value"]}
            for i in resource.get("identifier", [])
            if any(c.get("code") == "PT" for c in i.get("type", {}).get("coding", []))
        ]
        self.patients[uuid] = {
            "resourceType": "Patient",
            "id": uuid,
            "meta": {"versionId": "1", "lastUpdated": "2026-01-02T12:00:00+00:00"},
            "identifier": kept,
            "name": copy.deepcopy(names),
            "gender": resource["gender"],
            "birthDate": resource.get("birthDate"),
        }
        return self._json(201, {"pid": len(self.patients) + 2, "uuid": uuid})

    def _create_encounter(self, resource: dict) -> Answer:
        subject = resource.get("subject", {}).get("reference", "")
        if subject.removeprefix("Patient/") not in self.patients or "period" not in resource:
            return self._json(400, {"validationErrors": {"subject": "unknown patient"}})
        uuid = self._uuid()
        stored = copy.deepcopy(resource)
        stored.update(id=uuid, subject={"reference": subject, "type": "Patient"})
        self.encounters[uuid] = stored
        return self._json(201, {"euuid": uuid, "encounter": len(self.encounters), "pid": 3})

    def _create_observation(self, resource: dict) -> Answer:
        encounter = resource.get("encounter", {}).get("reference", "").removeprefix("Encounter/")
        if encounter not in self.encounters:
            return self._json(
                400,
                {
                    "validationErrors": {
                        "encounter": "Observation.encounter must reference an Encounter; "
                        "vitals are stored as an encounter form"
                    }
                },
            )
        if not resource.get("category"):
            return self._json(400, {"validationErrors": {"category": "required"}})
        if (
            resource.get("subject", {}).get("reference")
            != self.encounters[encounter]["subject"]["reference"]
        ):
            return self._json(
                400,
                {
                    "validationErrors": {
                        "subject": "Encounter does not belong to the patient named in "
                        "Observation.subject"
                    }
                },
            )
        code = resource["code"]["coding"][0]["code"]
        # 8.5.0 reads ONLY effectiveDateTime for a vitals write (effectivePeriod is never read)
        # and refuses an Observation without it; the time is half the identity of the row.
        at = _utc(resource.get("effectiveDateTime"))
        if at is None:
            return self._json(
                400,
                {
                    "validationErrors": {
                        "effectiveDateTime": "Observation.effectiveDateTime is required; it "
                        "identifies the vitals reading being written"
                    }
                },
            )
        key = (code, encounter, at.isoformat())
        existing = self.duplicates.get(key)
        if existing is not None:
            # 8.5.0's exact text; the id is included only when OpenEMR can map it
            # (``name_duplicates`` False imitates the unmapped case).
            named = f" (Observation/{existing})" if self.name_duplicates else ""
            return self._json(
                400,
                {
                    "validationErrors": {
                        "code": f'A "{code}" reading already exists for this encounter at this '
                        f"effectiveDateTime{named}; POST does not overwrite a stored reading -- "
                        "update that Observation with PUT"
                    }
                },
            )
        uuid = self._uuid()
        stored = copy.deepcopy(resource)
        for dropped in ("device", "identifier", "performer", "effectivePeriod"):
            stored.pop(dropped, None)
        # Component codes OpenEMR has no column for are skipped (a heart rate's min and max).
        kept = [
            c
            for c in stored.get("component", [])
            if c.get("code", {}).get("coding", [{}])[0].get("code") in ("8480-6", "8462-4")
        ]
        if kept:
            stored["component"] = kept
        else:
            stored.pop("component", None)
        stored["id"] = uuid
        stored["meta"] = {"profile": ["http://hl7.org/fhir/StructureDefinition/vitalsigns"]}
        stored["subject"] = {**stored["subject"], "type": "Patient"}
        stored["encounter"] = {**stored["encounter"], "type": "Encounter"}
        self.observations[uuid] = stored
        self.duplicates[key] = uuid
        return self._json(201, {"uuid": uuid})


@dataclass(frozen=True)
class SlowAnswer(Answer):
    """An answer the HTTPS fake sends slowly: the status line after ``header_delay`` seconds,
    then the body in two halves ``chunk_delay`` apart."""

    header_delay: float = 0.0
    chunk_delay: float = 0.0


class FakeTransport:
    """The share's ``Transport`` over :class:`FakeOpenEmr`, in process. Records the TLS context
    each call was given, so a test can check it is the pinned one."""

    def __init__(self, fake: FakeOpenEmr) -> None:
        self.fake = fake
        self.contexts: list[ssl.SSLContext] = []
        self.timeouts: list[float] = []

    def request(self, method, path, *, query, headers, body, timeout, context):  # noqa: ANN001, ANN201
        self.contexts.append(context)
        self.timeouts.append(timeout)
        if not (timeout > 0):
            raise AssertionError("a call without a time limit")
        return self.fake.handle(method, path, list(query or []), dict(headers), body or b"")


def raise_timeout(*_: object) -> Answer:
    raise OpenEmrTimeout()


def raise_unreachable(*_: object) -> Answer:
    raise OpenEmrUnreachable("unreachable")


# ---- a real HTTPS server on 127.0.0.1 -----------------------------------------------------------


class HttpsFakeServer:
    """:class:`FakeOpenEmr` served over TLS with a test-generated certificate."""

    def __init__(self, fake: FakeOpenEmr | None, certificate_pem: str, key_pem_text: str) -> None:
        # ``fake`` may be set later (``server.fake = ...``): the port is known only once bound.
        self.fake = fake
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                path, _, query = self.path.partition("?")
                try:
                    answer = owner.fake.handle(  # type: ignore[union-attr]
                        self.command, path, parse_qsl(query), dict(self.headers.items()), body
                    )
                except Exception:  # noqa: BLE001 - an injected failure: drop the connection
                    self.close_connection = True
                    return
                time.sleep(getattr(answer, "header_delay", 0.0))
                self.send_response(answer.status)
                for name, value in answer.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(answer.body)))
                self.end_headers()
                half = len(answer.body) // 2
                try:
                    self.wfile.write(answer.body[:half])
                    self.wfile.flush()
                    time.sleep(getattr(answer, "chunk_delay", 0.0))
                    self.wfile.write(answer.body[half:])
                except OSError:
                    return  # the client gave up

            do_GET = do_POST = _answer  # noqa: N815

            def log_message(self, *args: object) -> None:  # quiet
                return

        self.directory = tempfile.TemporaryDirectory()
        cert_file = Path(self.directory.name) / "cert.pem"
        key_file = Path(self.directory.name) / "key.pem"
        cert_file.write_text(certificate_pem)
        key_file.write_text(key_pem_text)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_file, key_file)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> HttpsFakeServer:
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.directory.cleanup()


# ---- a whole proxy with the share, over the fake stores and the fake OpenEMR -------------------

SITE = "https://10.10.0.2"
PATIENT_STORE_ID = "pb-internal-patient-7f3a"
OTHER_PATIENT_STORE_ID = "pb-internal-patient-other"
GOOGLE_HEALTH_ID = "synthetic-health-user-0001"
DEVICE_REF = "Device/pb-internal-device-91c2"
HR_ID = "pb-internal-obs-hr-1"
BP_ID = "pb-internal-obs-bp-1"


def stored_patient(store_id: str = PATIENT_STORE_ID, value: str = PATIENT_ID) -> dict:
    patient = example("patient.json")
    patient["id"] = store_id
    patient["identifier"][0]["value"] = value
    patient["meta"] = {"versionId": "3", "lastUpdated": "2026-01-01T10:00:00+00:00"}
    return patient


def stored_observation(name: str, rid: str, subject: str = PATIENT_STORE_ID) -> dict:
    observation = example(name)
    observation["id"] = rid
    observation["subject"] = {"reference": f"Patient/{subject}"}
    observation["performer"] = [{"reference": f"Patient/{subject}"}]
    observation["device"] = {"reference": DEVICE_REF}
    observation["meta"] = {
        "versionId": "1",
        "lastUpdated": "2026-01-01T15:00:00+00:00",
        "profile": observation["meta"]["profile"],
    }
    return observation


@dataclass
class ShareEnv:
    harness: Harness
    openemr: FakeOpenEmr
    transport: FakeTransport
    store: FakeHealthcare
    clock: Clock
    doc_path: Path
    settings: object
    key: object
    cert_pem: str

    @property
    def service(self):  # noqa: ANN201
        return self.harness.app.view_functions["share"].__self__

    def token(self) -> str:
        return self.harness.login()

    def share(
        self,
        body: object = None,
        *,
        observation_id: str = BP_ID,
        raw: bytes | None = None,
        content_type: str = "application/json",
        token: str | None = None,
    ):  # noqa: ANN201
        data = (
            raw
            if raw is not None
            else json.dumps({"observationId": observation_id} if body is None else body).encode()
        )
        return self.harness.client.post(
            "/share", data=data, content_type=content_type, headers=self.harness.auth(token)
        )

    def write_doc(self, **overrides: object) -> dict:
        doc = client_document(
            self.settings, key=self.key, certificate_pem=self.cert_pem, **overrides
        )
        self.doc_path.write_text(json.dumps(doc))
        return doc

    def sent_bodies(self) -> list[str]:
        """Every request body the fake OpenEMR received, as text."""
        return [r["body"].decode() for r in self.openemr.requests]


def build_share_env(
    tmp_path: Path,
    key,
    cert,
    *,
    site: str = SITE,
    transport=None,  # noqa: ANN001
    seed: bool = True,
    configured: bool = True,
    timeout: float = 5.0,
    **overrides,
) -> ShareEnv:
    doc_path = tmp_path / "openemr-client"
    settings = settings_for(site, str(doc_path), timeout=timeout)
    config = make_config(tmp_path, openemr=settings if configured else None, **overrides)
    clock = Clock()
    store = FakeHealthcare()
    openemr = FakeOpenEmr(
        site=site, token_url=settings.token_url, monotonic=clock.monotonic, clock=clock
    )
    openemr.register(CLIENT_ID, key, KID, SCOPES)
    fake_transport = transport or FakeTransport(openemr)
    sender = FakeSender()
    sender.answer("/status", 200, {"googleHealth": "connected"})
    app = create_app(
        config,
        store,
        id_tokens=FakeIdTokens(),
        send=sender,
        clock=clock,
        monotonic=clock.monotonic,
        extension_options={"openemr_transport": fake_transport},
    )
    harness = Harness(
        app,
        app.test_client(),
        store,
        sender,
        FakeIdTokens(),
        clock,
        Path(config.password_file),
        config,
    )
    env = ShareEnv(harness, openemr, fake_transport, store, clock, doc_path, settings, key, cert[0])
    env.write_doc()
    if seed:
        store.put(FHIR_STORE, stored_patient())
        store.put(FHIR_STORE, stored_observation("observation-blood-pressure.json", BP_ID))
        store.put(FHIR_STORE, stored_observation("observation-heart-rate.json", HR_ID))
    return env
