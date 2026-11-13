"""Transaction Bundle: many writes that succeed or fail together.

A Bundle of ``type: transaction`` is posted to the FHIR base URL; the server applies every entry
or none (R4 http.html#transaction). Entries refer to each other by their ``fullUrl``
(``urn:uuid:…``), and the server rewrites those references to the real ids when it commits, so a
new Observation can point at a Patient created in the same request.
"""

from collections.abc import Iterable, Iterator

from pulsebridge_fhir.errors import FhirInputError
from pulsebridge_fhir.resources import BuiltResource


def _references(node: object) -> Iterator[str]:
    """Every ``reference`` string anywhere inside a resource."""
    if isinstance(node, dict):
        for name, value in node.items():
            if name == "reference" and isinstance(value, str):
                yield value
            else:
                yield from _references(value)
    elif isinstance(node, list):
        for item in node:
            yield from _references(item)


def build_transaction_bundle(resources: Iterable[BuiltResource]) -> dict:
    """A transaction Bundle that writes each resource with ``PUT``.

    Strict checks, because the server would reject the whole transaction anyway and its error
    would be harder to trace back:

    - no two entries share a ``fullUrl`` or a request URL (two writes to the same resource in one
      transaction are an error in R4);
    - every ``urn:uuid`` reference points at an entry in this Bundle (a dangling bundle-local
      reference cannot be resolved by the server).

    The Bundle has no ``id`` or ``timestamp``, so the same input gives byte-identical output.
    """
    resources = list(resources)
    if not resources:
        raise FhirInputError("bundle: no resources to write")
    full_urls: set[str] = set()
    request_urls: set[str] = set()
    for built in resources:
        if built.full_url in full_urls:
            raise FhirInputError(f"bundle: duplicate entry {built.full_url}")
        if built.request_url in request_urls:
            raise FhirInputError(f"bundle: two entries write {built.request_url}")
        full_urls.add(built.full_url)
        request_urls.add(built.request_url)
    for built in resources:
        for ref in _references(built.resource):
            if ref.startswith("urn:uuid:") and ref not in full_urls:
                raise FhirInputError(
                    f"bundle: {built.resource_type} {built.full_url} refers to {ref}, "
                    "which is not in the bundle"
                )
    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [built.bundle_entry() for built in resources],
    }
