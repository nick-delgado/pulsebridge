"""Test helpers: synthetic values and fakes. No network beyond 127.0.0.1, no credentials, no GCP.

Every credential-shaped value is assembled at run time from obviously synthetic words, so none
matches a real token format (gitleaks scans the branch) and each is distinctive enough to search
for in captured output.
"""

from __future__ import annotations

import http.client
import io
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlsplit

from pulsebridge_gcp import HealthcareClient
from pulsebridge_gcp.testing import FakeTransport, json_response

from connect_google_health.cli import Config, Dependencies, FhirTarget
from connect_google_health.oauth import DEFAULT_SCOPE
from connect_google_health.patient import Demographics
from connect_google_health.secret_store import SecretStoreError, secret_id


def fake(*parts: str) -> str:
    return "-".join(("synthetic", *parts))


CLIENT_ID = fake("oauth", "client", "id")
CLIENT_SECRET = fake("oauth", "client", "secret", "value")
AUTH_CODE = fake("one", "time", "code", "value")
ACCESS_TOKEN = fake("short", "lived", "access", "value")
REFRESH_TOKEN = fake("long", "lived", "refresh", "value")
HEALTH_USER_ID = fake("health", "user", "id", "0001")
# Synthetic demographics, distinctive so a leak test can search for them.
DEMOGRAPHICS = Demographics(
    patient_id="PB-SYN-0001",
    family="Syntheticfamily",
    given="Syntheticgiven",
    gender="female",
    birth_date=date(1980, 4, 12),
)
NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

PROJECT = "demo-project-1"
FHIR = FhirTarget(location="us-central1", dataset="pulsebridge", fhir_store="fhir")
FHIR_PATH = "/fhirStores/fhir/fhir"

SECRET_VALUES = (CLIENT_ID, CLIENT_SECRET, AUTH_CODE, ACCESS_TOKEN, REFRESH_TOKEN)
PRIVATE_VALUES = (
    *SECRET_VALUES,
    HEALTH_USER_ID,
    DEMOGRAPHICS.family,
    DEMOGRAPHICS.given,
    "1980-04-12",
)


def leaked(text: str, values: tuple[str, ...] = PRIVATE_VALUES) -> list[str]:
    """Which private values appear in ``text``; a test asserts ``[]``. Also checks the
    URL-encoded and lower-cased forms (a value can reach output inside a URL)."""
    from urllib.parse import quote, quote_plus

    found = []
    for value in values:
        forms = {value, quote(value, safe=""), quote_plus(value), value.lower()}
        if any(form in text or form in text.lower() for form in forms):
            found.append(value)
    return found


def config(**overrides: object) -> Config:
    values: dict = {
        "project": PROJECT,
        "prefix": "pb",
        "scope": DEFAULT_SCOPE,
        "port": 0,
        "paste": False,
        "open_browser": True,
        "timeout_seconds": 20,
        "reset_client": False,
        "disable_previous": False,
        "accept_unlisted_scope": False,
        "fhir": FHIR,
        "demographics": DEMOGRAPHICS,
    }
    values.update(overrides)
    return Config(**values)


# ---- Secret Manager -------------------------------------------------------------------------


@dataclass
class FakeSecretStore:
    """Secrets as lists of [value, state]; version n is index n-1."""

    secrets: dict[str, list[list[str]]] = field(default_factory=dict)
    fail_add: set[str] = field(default_factory=set)
    fail_disable: bool = False
    calls: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def with_client(cls, prefix: str = "pb") -> FakeSecretStore:
        store = cls()
        for key in ("client-id", "client-secret", "refresh-token"):
            store.secrets[secret_id(prefix, key)] = []
        store.secrets[secret_id(prefix, "client-id")].append([CLIENT_ID, "ENABLED"])
        store.secrets[secret_id(prefix, "client-secret")].append([CLIENT_SECRET, "ENABLED"])
        return store

    @classmethod
    def empty(cls, prefix: str = "pb") -> FakeSecretStore:
        store = cls()
        for key in ("client-id", "client-secret", "refresh-token"):
            store.secrets[secret_id(prefix, key)] = []
        return store

    def access_latest(self, secret: str) -> str | None:
        self.calls.append(("access", secret))
        versions = self.secrets.get(secret) or []
        if not versions or versions[-1][1] != "ENABLED":
            return None
        return versions[-1][0]

    def add_version(self, secret: str, value: str) -> int:
        self.calls.append(("add", secret))
        if secret in self.fail_add or secret not in self.secrets:
            raise SecretStoreError(f"Secret {secret} does not exist (fake).")
        self.secrets[secret].append([value, "ENABLED"])
        return len(self.secrets[secret])

    def disable_versions_before(self, secret: str, version: int) -> int:
        self.calls.append(("disable", secret))
        if self.fail_disable:
            raise SecretStoreError(f"Permission denied on secret {secret} (fake).")
        count = 0
        for number, item in enumerate(self.secrets[secret], start=1):
            if number < version and item[1] == "ENABLED":
                item[1] = "DISABLED"
                count += 1
        return count

    def values(self, secret: str) -> list[str]:
        return [v for v, _ in self.secrets[secret]]


# ---- Google (token endpoint and identity) and the FHIR store ---------------------------------


def token_response(**overrides: object):
    body: dict = {
        "access_token": ACCESS_TOKEN,
        "expires_in": 3599,
        "refresh_token": REFRESH_TOKEN,
        "refresh_token_expires_in": 604799,
        "scope": DEFAULT_SCOPE,
        "token_type": "Bearer",
    }
    for key, value in overrides.items():
        if value is None:
            body.pop(key, None)
        else:
            body[key] = value
    return json_response(200, body)


def identity_response():
    return json_response(
        200,
        {"name": "users/me/identity", "healthUserId": HEALTH_USER_ID, "legacyUserId": "SYN0001"},
    )


def fhir_response(status: str = "201 Created"):
    return json_response(
        200,
        {
            "resourceType": "Bundle",
            "type": "transaction-response",
            "entry": [{"response": {"status": status, "location": "Patient/abc/_history/1"}}],
        },
    )


# ---- a scripted browser ---------------------------------------------------------------------


@dataclass
class FakeBrowser:
    """Stands in for the user's browser and Google's consent screen.

    ``open(url)`` is what the script calls. In a thread it requests the local ``/start`` link,
    reads the ``302`` to Google's address, and then requests the redirect address the way the
    browser would after the user clicks Allow: by default ``?state=<the state>&code=<code>``.
    ``answer`` can change what Google "sends" (a wrong state, ``error=access_denied``...).
    """

    answer: object = None  # callable(state) -> query string; None = success
    opens: bool = True  # False: webbrowser.open fails...
    by_hand: bool = True  # ...and the user then opens the printed link themselves
    before: list[tuple[str, str]] = field(default_factory=list)  # extra requests first
    opened: list[str] = field(default_factory=list)
    authorization_urls: list[str] = field(default_factory=list)
    pages: list[tuple[int, str]] = field(default_factory=list)
    thread: threading.Thread | None = None

    def open(self, url: str) -> bool:
        self.opened.append(url)
        if self.opens or self.by_hand:
            self.thread = threading.Thread(target=self._run, args=(url,), daemon=True)
            self.thread.start()
        return self.opens

    def _get(self, host: str, port: int, path: str, host_header: str | None = None):
        conn = http.client.HTTPConnection(host, port, timeout=5)
        try:
            conn.putrequest("GET", path, skip_host=host_header is not None)
            if host_header is not None:
                conn.putheader("Host", host_header)
            conn.endheaders()
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            conn.close()

    def _run(self, url: str) -> None:
        start = urlsplit(url)
        for path, host_header in self.before:
            status, _h, body = self._get(start.hostname, start.port, path, host_header)
            self.pages.append((status, body))
        status, headers, body = self._get(start.hostname, start.port, start.path)
        self.pages.append((status, body))
        location = headers.get("Location", "")
        self.authorization_urls.append(location)
        params = parse_qs(urlsplit(location).query)
        state = params["state"][0]
        redirect = urlsplit(params["redirect_uri"][0])
        query = self.answer(state) if callable(self.answer) else f"state={state}&code={AUTH_CODE}"
        status, _h, body = self._get(redirect.hostname, redirect.port, "/?" + query)
        self.pages.append((status, body))

    def join(self) -> None:
        if self.thread is not None:
            self.thread.join(timeout=10)


@dataclass
class Harness:
    """One test's fakes, wired into :class:`Dependencies`."""

    store: FakeSecretStore = field(default_factory=FakeSecretStore.with_client)
    google: FakeTransport = field(default_factory=FakeTransport)
    fhir: FakeTransport = field(default_factory=FakeTransport)
    browser: FakeBrowser = field(default_factory=FakeBrowser)
    # Answers to hidden prompts, in order: a string, or a callable(harness) -> string (for a
    # paste that needs what the script printed), or an exception to raise.
    prompts: list = field(default_factory=list)
    prompt_labels: list[str] = field(default_factory=list)
    out: io.StringIO = field(default_factory=io.StringIO)
    err: io.StringIO = field(default_factory=io.StringIO)
    random_port: int = 50123

    def __post_init__(self) -> None:
        self.google.add("POST", "/token", token_response())
        self.google.add("GET", "/v4/users/me/identity", identity_response())
        self.fhir.add("POST", FHIR_PATH, fhir_response())

    def prompt(self, label: str) -> str:
        self.prompt_labels.append(label)
        if not self.prompts:
            raise AssertionError(f"unexpected prompt: {label}")
        answer = self.prompts.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer(self) if callable(answer) else answer

    def deps(self) -> Dependencies:
        def healthcare_client(project: str, target: FhirTarget) -> HealthcareClient:
            return HealthcareClient(
                project=project,
                location=target.location,
                dataset=target.dataset,
                transport=self.fhir,
            )

        return Dependencies(
            secret_store=lambda project: self.store,
            google_transport=lambda: self.google,
            healthcare_client=healthcare_client,
            prompt_hidden=self.prompt,
            open_browser=self.browser.open,
            now=lambda: NOW,
            random_port=lambda: self.random_port,
            out=self.out,
            err=self.err,
        )

    def printed_authorization_url(self) -> str:
        """The Google address the script printed (paste mode)."""
        for line in self.out.getvalue().splitlines():
            if line.strip().startswith("https://accounts.google.com/"):
                return line.strip()
        raise AssertionError("no authorization address was printed")

    def paste_redirect(self, query: str | None = None) -> str:
        """What the browser's address bar shows after Google's redirect (paste mode)."""
        params = parse_qs(urlsplit(self.printed_authorization_url()).query)
        state = params["state"][0]
        query = query if query is not None else f"state={state}&code={AUTH_CODE}"
        return f"{params['redirect_uri'][0]}/?{query.replace('{state}', state)}"

    def output(self) -> str:
        pages = "\n".join(body for _status, body in self.browser.pages)
        return self.out.getvalue() + "\n" + self.err.getvalue() + "\n" + pages
