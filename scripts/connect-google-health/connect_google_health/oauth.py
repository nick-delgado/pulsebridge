"""The OAuth 2.0 authorization-code flow for an installed app, as Google documents it.

Source: Google Identity, "OAuth 2.0 for Mobile & Desktop Apps"
(developers.google.com/identity/protocols/oauth2/native-app), read 2026-11-15, and "Using OAuth
2.0 for Web Server Applications" (…/oauth2/web-server) for ``access_type`` and ``prompt``.

1. **Authorization request**: the browser opens ``https://accounts.google.com/o/oauth2/v2/auth``
   with ``client_id``, ``redirect_uri`` (a loopback address, ``http://127.0.0.1:<port>``),
   ``response_type=code``, ``scope``, ``code_challenge`` + ``code_challenge_method=S256`` (PKCE),
   ``state``, ``access_type=offline`` and ``prompt=consent``.
2. **Redirect**: after the user approves, Google sends the browser to
   ``redirect_uri?state=…&code=…`` (or ``?error=access_denied`` and similar when they do not).
3. **Code exchange**: ``POST https://oauth2.googleapis.com/token`` with ``client_id``,
   ``client_secret``, ``code``, ``code_verifier``, ``grant_type=authorization_code`` and the same
   ``redirect_uri``. The answer holds the ``access_token`` (about an hour) and the
   ``refresh_token`` (long-lived; what ``health-sync`` keeps using).

Why ``access_type=offline`` and ``prompt=consent``: ``offline`` asks for a refresh token, so
``health-sync`` can get access tokens while the user is not at the browser. Without
``prompt=consent``, Google shows the consent screen only the first time and a later run (a
reconnect) can come back without a refresh token. Google's desktop-app page says refresh tokens
"are always returned for installed applications"; we send both anyway, because the OAuth client
the deployer created may not be of the "Desktop app" type, and spike S3 ran with both.

Nothing here prints or logs. Errors carry a fixed code and a sentence written here, never a
value from the user, the browser or Google's response body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlsplit

from pulsebridge_gcp import RetryableError
from pulsebridge_gcp.transport import Transport

from connect_google_health.pkce import state_matches

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a password

# The one scope PulseBridge needs (spike S3: it covers heart rate; there is no heart-rate-only
# scope). The same default as health-sync's GOOGLE_HEALTH_SCOPE.
DEFAULT_SCOPE = (
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly"
)

HTTP_TIMEOUT_SECONDS = 30.0

# Google documents refresh tokens up to 512 bytes and access tokens up to 2048 bytes ("Token
# size"). health-sync refuses a mounted secret over 4 KiB, or one holding anything but printable
# ASCII without spaces; the same rule is applied before anything is stored, so what this script
# stores is always something health-sync can use.
MAX_CREDENTIAL_LENGTH = 4096
_CREDENTIAL = re.compile(r"[!-~]+", re.ASCII)

# OAuth error codes that may be shown. RFC 6749 4.1.2.1 (authorization response) and 5.2 (token
# endpoint), plus codes Google documents. Any other value is shown as "refused", so nothing a
# server or a pasted address sends reaches the terminal unchecked. `error_description` (free
# text) is never shown.
KNOWN_OAUTH_ERRORS = frozenset(
    {
        "invalid_request",
        "unauthorized_client",
        "access_denied",
        "unsupported_response_type",
        "invalid_scope",
        "server_error",
        "temporarily_unavailable",
        "invalid_client",
        "invalid_grant",
        "unsupported_grant_type",
        "deleted_client",
        "disabled_client",
        "admin_policy_enforced",
        "invalid_rapt",
        "org_internal",
        "redirect_uri_mismatch",
        "policy_enforced",
    }
)


GOOGLE_SCOPE_PREFIX = "https://www.googleapis.com/auth/"


def scope_name(scope: str) -> str:
    """A Google scope without the ``https://www.googleapis.com/auth/`` prefix, so the long and
    the short spelling of the same scope compare equal."""
    return scope[len(GOOGLE_SCOPE_PREFIX) :] if scope.startswith(GOOGLE_SCOPE_PREFIX) else scope


def missing_scopes(requested: str, granted: tuple[str, ...]) -> list[str]:
    """Requested scopes absent from the granted list, compared by :func:`scope_name`."""
    have = {scope_name(s) for s in granted}
    return [s for s in requested.split() if scope_name(s) not in have]


def is_credential_shaped(value: str) -> bool:
    """One token of printable ASCII, no spaces, not too long (health-sync's own rule)."""
    return 0 < len(value) <= MAX_CREDENTIAL_LENGTH and bool(_CREDENTIAL.fullmatch(value))


def safe_error_code(value: object) -> str:
    return value if isinstance(value, str) and value in KNOWN_OAUTH_ERRORS else "refused"


class OAuthError(Exception):
    """A step of the flow failed. ``code`` is a fixed short code; ``str()`` is a sentence
    written in this module, safe to print. Neither contains a credential or any received text
    except an allowlisted OAuth error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---- step 1: the authorization request -------------------------------------------------------


def loopback_redirect_uri(port: int) -> str:
    """``http://127.0.0.1:<port>``, the form in Google's guide ("Loopback IP address"). The IP
    literal, not ``localhost``: Google notes ``localhost`` "may cause issues with client
    firewalls", and a name could resolve to something other than this machine."""
    if not 1 <= port <= 65535:
        raise ValueError("port must be 1 to 65535")
    return f"http://127.0.0.1:{port}"


def authorization_url(
    *, client_id: str, redirect_uri: str, scope: str, state: str, code_challenge: str
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    # quote_via default (quote_plus) encodes a space in a multi-scope value as "+", which is
    # correct for application/x-www-form-urlencoded query strings.
    return f"{AUTH_URL}?{urlencode(params)}"


# ---- step 2: the redirect --------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizationCode:
    value: str = field(repr=False)


def _single(params: dict[str, list[str]], name: str) -> str | None:
    values = params.get(name)
    if not values:
        return None
    if len(values) > 1:
        raise OAuthError("repeated-parameter", f"The redirect repeated the '{name}' parameter.")
    return values[0]


def _parse_query(query: str) -> dict[str, list[str]]:
    """``parse_qs`` with a cap on the number of fields; too many is a malformed redirect."""
    try:
        return parse_qs(query, keep_blank_values=True, max_num_fields=20)
    except ValueError:
        raise OAuthError("malformed-redirect", "The redirect address is malformed.") from None


def redirect_state_matches(query: str, *, expected_state: str) -> bool:
    """Whether a request to the listener carries this run's ``state``, exactly once.

    The listener uses this to tell the real redirect from anything else that reaches
    127.0.0.1: any web page the user has open can make the browser send a request there, so a
    request without the right ``state`` is answered and ignored; it does not end the wait.
    """
    try:
        state = _single(_parse_query(query), "state")
    except OAuthError:
        return False
    return state_matches(expected_state, state)


def parse_redirect_query(query: str, *, expected_state: str) -> AuthorizationCode:
    """Check one redirect's query string and return the code.

    Order matters: ``state`` is checked first, so neither an error nor a code from a redirect
    this run did not ask for is acted on (RFC 6749 10.12, cross-site request forgery).
    """
    params = _parse_query(query)
    state = _single(params, "state")
    if not state_matches(expected_state, state):
        raise OAuthError(
            "state-mismatch",
            "The redirect's 'state' did not match this run's, so it was refused. It may come "
            "from an older attempt or another program. Run the script again.",
        )
    error = _single(params, "error")
    if error is not None:
        code = safe_error_code(error)
        if code == "access_denied":
            raise OAuthError(
                "access_denied",
                "Google returned error=access_denied: the consent screen was declined, or your "
                "account is not a test user of the OAuth app (Google Auth Platform > Audience > "
                "Test users). Nothing was stored.",
            )
        raise OAuthError(code, f"Google returned error={code}. Nothing was stored.")
    value = _single(params, "code")
    if not value:
        raise OAuthError(
            "missing-code", "The redirect carried no authorization code. Nothing was stored."
        )
    if not is_credential_shaped(value):
        raise OAuthError("malformed-code", "The authorization code is not in a valid form.")
    return AuthorizationCode(value)


def parse_pasted_redirect(
    pasted: str, *, redirect_uri: str, expected_state: str
) -> AuthorizationCode:
    """The full address the browser ended on, pasted by the user when the script cannot listen.

    It must be the redirect address this run asked for (scheme, host, port and the root path),
    so a link from elsewhere is not mistaken for it. The query string, or failing that the
    fragment, holds the parameters (Google's guide mentions the fragment for ``state``; spike S3
    saw everything in the query). The pasted text is never echoed, in success or error.
    """
    text = pasted.strip()
    # Some terminals and copy tools wrap a copied address in one pair of quotes.
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1].strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise OAuthError(
            "paste-not-a-url", "That is not a web address. Paste the whole address bar."
        ) from None
    expected = urlsplit(redirect_uri)
    if (
        parts.scheme != expected.scheme
        or parts.hostname != expected.hostname
        or port != expected.port
        or parts.path not in ("", "/")
    ):
        raise OAuthError(
            "paste-wrong-address",
            f"That is not the redirect address. It should start with {redirect_uri}/?",
        )
    query = parts.query or parts.fragment
    if not query:
        raise OAuthError("missing-code", "The pasted address has no parameters (no code).")
    return parse_redirect_query(query, expected_state=expected_state)


# ---- step 3: the code exchange ---------------------------------------------------------------


@dataclass(frozen=True)
class Tokens:
    """The token response. ``repr`` hides the tokens."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_in: int
    scopes: tuple[str, ...] | None  # None when the response had no `scope`
    refresh_token_expires_in: int | None  # None when Google did not say


@dataclass(frozen=True)
class ClientCredentials:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)


def token_request_body(
    client: ClientCredentials, code: AuthorizationCode, *, code_verifier: str, redirect_uri: str
) -> bytes:
    """The form Google's guide lists for "Exchange authorization code for refresh and access
    tokens", in that order."""
    return urlencode(
        {
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "code": code.value,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    ).encode("ascii")


_TOKEN_ERRORS = {
    "invalid_grant": (
        "Google refused the authorization code (invalid_grant): it expired, was already used, "
        "or did not match this run's PKCE verifier or redirect address. Run the script again."
    ),
    "invalid_client": (
        "Google refused the OAuth client (invalid_client): the stored client ID or client "
        "secret is wrong, or the client was deleted. Run again with --reset-client and paste "
        "the values from Google Auth Platform > Clients."
    ),
    "unauthorized_client": (
        "Google says this OAuth client may not use this flow (unauthorized_client). Create a "
        "client of type 'Desktop app' and run again with --reset-client."
    ),
    "deleted_client": (
        "The OAuth client was deleted (deleted_client). Create a new 'Desktop app' client and "
        "run again with --reset-client."
    ),
}


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def exchange_code(
    transport: Transport,
    client: ClientCredentials,
    code: AuthorizationCode,
    *,
    code_verifier: str,
    redirect_uri: str,
    token_url: str = TOKEN_URL,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> Tokens:
    """Exchange the code. ``token_url`` exists only so tests can point it at a local fake; the
    command line has no way to change it, so the client secret only ever goes to Google."""
    body = token_request_body(client, code, code_verifier=code_verifier, redirect_uri=redirect_uri)
    try:
        response = transport.request(
            "POST",
            token_url,
            params=None,
            body=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=timeout,
        )
    except RetryableError:
        raise OAuthError(
            "network-error",
            "Could not reach Google's token endpoint (no answer). Check the network and run the "
            "script again; the authorization code is single-use, so a new consent is needed.",
        ) from None
    try:
        parsed = response.json()
    except (ValueError, UnicodeDecodeError):
        parsed = None
    data = parsed if isinstance(parsed, dict) else None

    if response.status in (400, 401):
        code_name = safe_error_code(data.get("error") if data else None)
        message = _TOKEN_ERRORS.get(
            code_name,
            f"Google refused the code exchange ({code_name}, HTTP {response.status}).",
        )
        raise OAuthError(code_name, message)
    if not 200 <= response.status < 300:
        raise OAuthError(
            "unexpected-status",
            f"Google's token endpoint answered HTTP {response.status}. Run the script again.",
        )
    if data is None:
        raise OAuthError("malformed-response", "Google's token response was not a JSON object.")

    access = data.get("access_token")
    token_type = data.get("token_type")
    expires_in = _positive_int(data.get("expires_in"))
    if (
        not isinstance(access, str)
        or not is_credential_shaped(access)
        or expires_in is None
        or not (isinstance(token_type, str) and token_type.lower() == "bearer")
    ):
        raise OAuthError("malformed-response", "Google's token response is missing fields.")

    refresh = data.get("refresh_token")
    if refresh is None or refresh == "":
        raise OAuthError(
            "no-refresh-token",
            "Google returned no refresh token, so health-sync could not keep syncing. Nothing "
            "was stored. Usually Google already holds a grant for this app and did not issue a "
            "new one: remove PulseBridge's access at https://myaccount.google.com/connections "
            "and run the script again. Also check the OAuth client is of type 'Desktop app'.",
        )
    if not isinstance(refresh, str) or not is_credential_shaped(refresh):
        raise OAuthError("malformed-response", "The refresh token is not in a valid form.")

    scope = data.get("scope")
    scopes = tuple(scope.split()) if isinstance(scope, str) else None
    return Tokens(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
        scopes=scopes,
        refresh_token_expires_in=_positive_int(data.get("refresh_token_expires_in")),
    )
