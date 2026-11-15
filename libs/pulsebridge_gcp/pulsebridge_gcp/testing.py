"""A scripted :class:`~pulsebridge_gcp.transport.Transport` for tests. No network, no credentials.

    transport = FakeTransport()
    transport.add("GET", "/hl7V2Stores/main/messages/abc", json_response(200, {...}))
    client = HealthcareClient(project="p", location="l", dataset="d", transport=transport)
    ...
    assert transport.requests[0].params == {"view": "RAW_ONLY"}

A route matches when the method is equal and the request URL's path (without the query) ends
with the given suffix. Each route answers with its responses in order and then repeats the last
one; a response may also be an exception instance, which is raised (to fake a network failure).
A request no route matches fails the test with an AssertionError naming it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from pulsebridge_gcp.transport import HttpResponse


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(
        status=status,
        body=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    url: str
    params: dict[str, str]  # last value wins when a name repeats; see param_pairs
    param_pairs: list[tuple[str, str]]  # every parameter, in order
    body: bytes | None
    headers: dict[str, str]
    timeout: float

    @property
    def path(self) -> str:
        return urlsplit(self.url).path

    def json(self) -> object:
        return None if self.body is None else json.loads(self.body.decode("utf-8"))


@dataclass
class _Route:
    method: str
    suffix: str
    responses: list[HttpResponse | BaseException]
    calls: int = 0


@dataclass
class FakeTransport:
    requests: list[RecordedRequest] = field(default_factory=list)
    _routes: list[_Route] = field(default_factory=list)

    def add(self, method: str, path_suffix: str, *responses: HttpResponse | BaseException) -> None:
        if not responses:
            raise ValueError("a route needs at least one response")
        self._routes.append(_Route(method, path_suffix, list(responses)))

    def calls(self, method: str, path_suffix: str = "") -> list[RecordedRequest]:
        return [r for r in self.requests if r.method == method and r.path.endswith(path_suffix)]

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | Sequence[tuple[str, str]] | None,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        pairs = list(params.items()) if isinstance(params, Mapping) else list(params or [])
        recorded = RecordedRequest(method, url, dict(pairs), pairs, body, dict(headers), timeout)
        self.requests.append(recorded)
        for route in self._routes:
            if route.method == method and recorded.path.endswith(route.suffix):
                response = route.responses[min(route.calls, len(route.responses) - 1)]
                route.calls += 1
                if isinstance(response, BaseException):
                    raise response
                return response
        raise AssertionError(f"FakeTransport: no route for {method} {recorded.path}")
