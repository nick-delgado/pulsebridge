"""RequestsTransport over a stand-in session: no network, no credentials."""

import google.auth.exceptions
import pytest
import requests
from pulsebridge_gcp import RequestsTransport, RetryableError, TimedOutError


class _Response:
    status_code = 201
    content = b'{"ok": true}'
    headers = {"Content-Type": "application/json", "X-Goog-Thing": "1"}


class _Session:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _send(transport):
    return transport.request(
        "POST",
        "https://healthcare.googleapis.com/v1/x?y=1",
        params={"a": "b"},
        body=b"{}",
        headers={"Content-Type": "application/json"},
        timeout=3.0,
    )


def test_passes_the_request_through_and_lowercases_headers():
    session = _Session(_Response())
    response = _send(RequestsTransport(session))
    assert response.status == 201
    assert response.json() == {"ok": True}
    assert response.headers == {"content-type": "application/json", "x-goog-thing": "1"}
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("POST", "https://healthcare.googleapis.com/v1/x?y=1")
    assert kwargs == {
        "params": {"a": "b"},
        "data": b"{}",
        "headers": {"Content-Type": "application/json"},
        "timeout": 3.0,
    }


@pytest.mark.parametrize(
    "failure",
    [
        requests.ConnectionError("reset"),
        requests.Timeout("slow"),
        requests.ConnectTimeout("connect slow"),  # also a ConnectionError: still a timeout
        requests.ReadTimeout("read slow"),
        google.auth.exceptions.RefreshError("metadata server did not answer"),
    ],
)
def test_no_response_at_all_is_retryable(failure):
    with pytest.raises(RetryableError) as caught:
        _send(RequestsTransport(_Session(failure)))
    assert caught.value.status is None
    # Only a timeout is a TimedOutError (T33): readable without inspecting __cause__.
    assert isinstance(caught.value, TimedOutError) is isinstance(failure, requests.Timeout)
    # The URL's query string is not repeated in the error.
    assert "y=1" not in str(caught.value)
