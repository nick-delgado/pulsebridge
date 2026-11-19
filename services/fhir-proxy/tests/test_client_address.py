"""Which X-Forwarded-For entry is the client (``TRUSTED_PROXY_HOPS``): the key of the
per-address session limit. Moved from the D9 login tests; the rules are unchanged."""

from __future__ import annotations

import pytest
from fhir_proxy.client_address import UNPARSEABLE, client_address


@pytest.mark.parametrize(
    ("xff", "remote", "hops", "expected"),
    [
        # 0 hops: no proxy in front; the header is ignored entirely.
        ("203.0.113.5", "192.0.2.10", 0, "192.0.2.10"),
        # 1 hop: straight to *.run.app; Cloud Run appended the last entry.
        ("203.0.113.5", "169.254.1.1", 1, "203.0.113.5"),
        ("6.6.6.6, 203.0.113.5", "169.254.1.1", 1, "203.0.113.5"),
        ("evil, also evil, 203.0.113.5", "169.254.1.1", 1, "203.0.113.5"),
        # 2 hops: a proxy in front that appends one more entry (e.g. LB: CLIENT_IP, LB_IP).
        ("6.6.6.6, 203.0.113.5, 35.191.0.1", "169.254.1.1", 2, "203.0.113.5"),
        ("203.0.113.5, 35.191.0.1", "169.254.1.1", 2, "203.0.113.5"),
        # Fewer entries than hops: every entry is a trusted one; use the left-most.
        ("203.0.113.5", "169.254.1.1", 2, "203.0.113.5"),
        # Ports and IPv6.
        ("[2001:db8::7]:443", "169.254.1.1", 1, "2001:db8::/64"),
        ("203.0.113.5:8443", "169.254.1.1", 1, "203.0.113.5"),
        ("::ffff:203.0.113.5", "169.254.1.1", 1, "203.0.113.5"),
        # Not an address in the trusted position: one shared bucket.
        ("203.0.113.5, not-an-ip", "169.254.1.1", 1, UNPARSEABLE),
        (",,,", "169.254.1.1", 1, UNPARSEABLE),
        ("1.2.3.4," * 400, "169.254.1.1", 1, UNPARSEABLE),
        # No header at all: the socket's peer.
        (None, "192.0.2.10", 1, "192.0.2.10"),
    ],
)
def test_client_address(xff: str | None, remote: str, hops: int, expected: str) -> None:
    assert client_address(xff, remote, hops) == expected


# ---- two hops (module phr's default) and the public run.app URL ------------------------------
#
# Behind Firebase Hosting the header arrives as "<visitor>, <Hosting's address>": Hosting replaces
# whatever the visitor sent (measured, infra/envs/README.md P1). The run.app URL stays public, and
# there Cloud Run appends the caller to whatever the caller sent. These tests DOCUMENT what a hop
# count of 2 then does; the last two show the known limit (module phr's README, "The client
# address and the direct run.app URL").

VISITOR = "198.51.100.7"  # the real caller, as Hosting or Cloud Run saw it
HOSTING = "203.0.113.200"  # stands in for Hosting's own (changing) address


@pytest.mark.parametrize(
    ("xff", "expected"),
    [
        # Through Hosting: the visitor, whatever it sent (Hosting dropped its header).
        (f"{VISITOR}, {HOSTING}", VISITOR),
        # Direct at run.app with no header of its own: 1 entry, fewer than the hops. Every entry
        # present was appended by Cloud Run, so the left-most (the only one) is the caller: right.
        (VISITOR, VISITOR),
        # Direct at run.app with a made-up header: Cloud Run appends the caller to it, and the
        # second entry from the right, which the CALLER wrote, becomes the key.
        (f"192.0.2.1, {VISITOR}", "192.0.2.1"),
        (f"192.0.2.1, 192.0.2.2, {VISITOR}", "192.0.2.2"),
        # Text in that position: one shared bucket, not a fresh one per value.
        (f"not-an-address, {VISITOR}", UNPARSEABLE),
    ],
)
def test_two_hops_through_hosting_and_directly(xff: str, expected: str) -> None:
    assert client_address(xff, "169.254.1.1", 2) == expected
