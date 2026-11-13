"""Deterministic keys, bundle-local UUIDs and conditional-update URLs.

Idempotency rests on one rule: the same input always produces the same key. Keys are UUIDv5
values (RFC 9562: a SHA-1 name-based UUID), computed from a canonical JSON list of the parts
that make a resource unique, so:

- the value is always safe inside a URL and a FHIR search token (only hex digits and hyphens);
- two different part lists cannot collide by concatenation (``["a|b", "c"]`` and
  ``["a", "b|c"]`` serialise differently);
- nothing outside the input is used: no clock, no random number, no counter.
"""

import json
import uuid
from urllib.parse import quote

# Fixed namespace for every PulseBridge key. Derived, not random, so anyone can recompute it.
PULSEBRIDGE_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:pulsebridge")


def deterministic_uuid(*parts: str) -> str:
    """UUIDv5 of the parts, as a lowercase string."""
    canonical = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"))
    return str(uuid.uuid5(PULSEBRIDGE_NAMESPACE, canonical))


def urn_uuid(value: str) -> str:
    """A bundle-local identity, used as ``Bundle.entry.fullUrl`` and in references to the entry."""
    return f"urn:uuid:{value}"


def escape_search_value(value: str) -> str:
    """Escape FHIR search special characters (R4 search.html, "Escaping Search Parameters").

    In a token parameter ``system|value``, a literal ``\\``, ``|``, ``,`` or ``$`` inside either
    part must be preceded by a backslash, or the server splits the value in the wrong place.
    """
    for ch in ("\\", "|", ",", "$"):
        value = value.replace(ch, "\\" + ch)
    return value


def conditional_url(resource_type: str, system: str, value: str) -> str:
    """``Bundle.entry.request.url`` for a conditional update on one identifier.

    ``PUT Observation?identifier=<system>|<value>`` asks the server to update the one resource
    with that identifier, or create it if there is none (R4 http.html, "conditional update").
    The Cloud Healthcare API's conditional update matches on identifier only, which is why every
    conditionally written resource here carries a deterministic identifier.

    The token is percent-encoded except for ``:`` and the ``|`` separator, which stay readable.
    """
    token = f"{escape_search_value(system)}|{escape_search_value(value)}"
    return f"{resource_type}?identifier={quote(token, safe=':|')}"
