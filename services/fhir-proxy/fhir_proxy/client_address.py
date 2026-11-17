"""Which client a login attempt came from, for the per-address rate limit.

``X-Forwarded-For`` (XFF) is a comma-separated list. Each proxy **appends** the address it
received the connection from; it never removes what was already there. So only the entries at
the **right-hand end**, appended by proxies we run behind, can be trusted; everything to their
left was written by whoever sent the request and can be anything (a spoofed address, text).

What Google documents (checked 2026-11-15):

- The external Application Load Balancer "appends two IP addresses to the X-Forwarded-For
  header ... The IP address of the client that connects to the load balancer [and] the IP
  address of the load balancer's forwarding rule", giving ``EXISTING_VALUE, CLIENT_IP,
  LOAD_BALANCER_IP``, and warns that it "does not verify any IP addresses that precede
  CLIENT_IP, LOAD_BALANCER_IP" (https://docs.cloud.google.com/load-balancing/docs/https,
  "X-Forwarded-For header").
- Cloud Run's own documentation (container contract, HTTPS invocation) does not describe XFF.
  Its ``*.run.app`` front end appends exactly one entry, the address it saw, after whatever the
  client sent: ``<client-supplied>, ..., OBSERVED_IP``. Reported on the Google developer forum
  ("Cloud Run: which client IP can I safely use for rate limiting?") and measured on dev on
  17 November 2026 (infra/envs/README.md, P1: a made-up ``192.0.2.1`` arrived as 2 entries,
  ``192.0.2.1, 198.51.100.9`` as 3).

So ``TRUSTED_PROXY_HOPS`` counts trusted entries **from the right**, and the client is the
entry that many places from the end:

- ``1`` (this service's default): straight to ``*.run.app``; the last entry is the one Cloud
  Run appended.
- ``2``: behind one more proxy that adds one entry. Behind Firebase Hosting's rewrite, the only
  way the page reaches this service, the header arrives as ``<visitor>, <Hosting's address>``
  (measured on dev, 17 November 2026: Hosting replaces whatever the visitor sent), so module
  phr sets 2. A load balancer's ``CLIENT_IP, LOAD_BALANCER_IP`` pair has the same shape.
- ``0``: no proxy in front at all (local development); the socket's peer address is used.

If the header has fewer entries than the hops say, the left-most entry is used: every entry
present was then appended by a trusted proxy. An entry that is not an IP address (a spoofed
value cannot reach the trusted positions, but a misconfigured hop count could) puts the
request in one shared bucket, ``unparseable``, so text cannot create unlimited buckets.

A hop count cannot tell which path a request took. With 2, a request sent straight to the
``*.run.app`` URL, which stays public because Hosting calls it, is keyed as follows: with no
header of its own it carries 1 entry (the caller, appended by Cloud Run) and that entry is used,
which is right; with a header ``X`` of its own it arrives as ``X, <caller>`` and ``X``, which
the caller wrote, becomes the key (measured; with ``A, B`` it is ``B``). Such a caller can
therefore pick a fresh key per request, or a reviewer's address to fill that reviewer's bucket
(README, "Reviewer login"; module phr's README, "The client address and the direct run.app
URL").

IPv6 clients are grouped by their /64 network: one subscriber usually holds a whole /64, so
per-address counting of single IPv6 addresses would give an attacker 2^64 fresh budgets.
"""

from __future__ import annotations

import ipaddress

UNPARSEABLE = "unparseable"
MAX_HEADER_LENGTH = 2048
IPV6_PREFIX = 64


def _normalise(text: str) -> str:
    candidate = text.strip()
    # "[2001:db8::1]:443" and "203.0.113.5:443": strip a port if a proxy wrote one.
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1:
        candidate = candidate.split(":", 1)[0]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return UNPARSEABLE
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        network = ipaddress.IPv6Network(f"{address}/{IPV6_PREFIX}", strict=False)
        return str(network)
    return str(address)


def client_address(forwarded_for: str | None, remote_addr: str | None, trusted_hops: int) -> str:
    """The rate-limit key for one request (an IPv4 address or an IPv6 /64)."""
    if trusted_hops <= 0:
        return _normalise(remote_addr or "")
    if not forwarded_for or len(forwarded_for) > MAX_HEADER_LENGTH:
        # Behind Cloud Run the header is always present. Without it (or with a header too long
        # to have been built by proxies alone), fall back to the socket's peer.
        return _normalise(remote_addr or "") if not forwarded_for else UNPARSEABLE
    entries = [part for part in forwarded_for.split(",") if part.strip()]
    if not entries:
        return UNPARSEABLE
    index = max(len(entries) - trusted_hops, 0)
    return _normalise(entries[index])
