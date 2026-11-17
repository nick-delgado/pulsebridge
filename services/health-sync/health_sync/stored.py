"""What the FHIR store already holds for a range: read once, then write only what changed.

Every run re-requests three hours, so without this each window would be rewritten about twelve
times, and with versioning on each rewrite stores a new version of the Observation and of its
Provenance. Instead a run makes these FHIR searches (R4 search, standard parameters only):

1. ``Patient?identifier=urn:pulsebridge:patient|<PATIENT_ID>`` for the Patient's server id
   (the Observations refer to it as ``Patient/<id>``; the setup step wrote it, D11):
   :func:`find_patient_ids`, here.
2. ``Observation?subject=Patient/<id>&code=http://loinc.org|8867-4&date=ge<start>&date=lt<end>``
   with ``_elements`` (only the fields compared) and ``_count``, following the ``next`` links:
   the shared read, :func:`pulsebridge_gcp.stored.stored_windows` (since T35 the seeder uses
   the same code). What to write is then :func:`pulsebridge_fhir.stored.plan_writes`.

and :func:`count_stranded`, here, counts preliminary windows that have fallen behind every
future run (``status=preliminary&date=lt<range start>&_total=accurate&_count=0``).

If any search fails, the caller writes everything, as before: reading is an optimisation,
never a condition for writing. Values are compared, never logged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pulsebridge_fhir import codes
from pulsebridge_fhir.stored import StoredReadError, search_instant, search_token


class SearchClient(Protocol):
    def search(self, fhir_store: str, resource_type: str, params: object) -> dict: ...


def find_patient_ids(client: SearchClient, store: str, patient_id: str) -> list[str]:
    """The server ids of the Patients with our identifier: normally exactly one (the setup
    step's). None means no Patient yet; more than one is the duplicate case T06's README warns
    about (the conditional creates will then fail with 412 until it is fixed by hand)."""
    bundle = client.search(
        store,
        "Patient",
        [("identifier", search_token(codes.PB_PATIENT_ID, patient_id)), ("_elements", "id")],
    )
    ids = [
        entry["resource"]["id"]
        for entry in bundle.get("entry") or []
        if isinstance(entry, dict)
        and isinstance(entry.get("resource"), dict)
        and isinstance(entry["resource"].get("id"), str)
    ]
    return ids


def count_stranded(client: SearchClient, store: str, patient_ref: str, before: datetime) -> int:
    """Stored ``preliminary`` windows that start before ``before`` (the earliest range start).
    No later run will request them again, so they stay preliminary until a backfill covers
    them.

    The count is ``Bundle.total`` of a searchset with no entries. R4 offers two ways to ask for
    that: ``_summary=count``, which Cloud Healthcare refuses with 400 ("_summary argument is not
    supported yet", seen on the first dev deploy by fhir-proxy), and ``_count=0`` (a page of
    zero resources) with ``_total=accurate`` (R4 search's ``_total``: an exact total rather than
    an estimate), which is what fhir-proxy's ``/status`` counts with since then. A missing,
    negative or non-integer ``total`` raises, so a broken count is never reported as zero."""
    bundle = client.search(
        store,
        "Observation",
        [
            ("subject", patient_ref),
            ("code", search_token(codes.LOINC, codes.HEART_RATE["code"])),
            ("status", "preliminary"),
            ("date", f"lt{search_instant(before)}"),
            ("_total", "accurate"),
            ("_count", "0"),
        ],
    )
    total = bundle.get("total") if isinstance(bundle, dict) else None
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise StoredReadError("count search has no total")
    return total
