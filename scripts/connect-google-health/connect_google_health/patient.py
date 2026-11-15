"""The Google Health identity call and the one-time Patient write (decision D11).

**Identity.** ``GET https://health.googleapis.com/v4/users/me/identity`` with the new access
token answers ``{"name", "healthUserId", "legacyUserId"}`` (spike S3). ``healthUserId`` is
Google's stable identifier for the person; it becomes the Patient's secondary identifier
(``urn:pulsebridge:google-health-user-id``). It is never printed.

**Patient.** The setup step is the only writer of the full Patient record: identifier, official
name, gender, birth date and the Google user id. It uses T06's ``patient_setup_transaction``,
which writes the Patient as a FHIR **conditional update**: ``PUT
Patient?identifier=urn:pulsebridge:patient|<PATIENT_ID>``. The server searches for a Patient with
that identifier; if it finds one it replaces it with this record, if it finds none it creates
it. Running the script again therefore writes the same Patient, never a second one. (The ingest
services write the Patient as a conditional *create* instead, which never overwrites; see the
``pulsebridge_fhir`` README.)

**No Device.** T06 keys the Device on the patient and the device's display name, and
``health-sync`` names it from ``dataSource.device.displayName`` of the newest raw data point, as
it reads it. Setup knows nothing more about the watch than that name, and reading it here would
be one more health-data call whose result has to match health-sync's exactly, or a second watch
Device appears. So setup writes no Device (``devices=()``), and health-sync creates it on the
first sync, which is what the T06 README recommends when setup does not read the name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from pulsebridge_fhir import (
    FhirInputError,
    PatientInput,
    build_patient,
    check_complete_patient,
    patient_setup_transaction,
)
from pulsebridge_gcp import HealthcareApiError, HealthcareClient, RetryableError
from pulsebridge_gcp.transport import Transport

IDENTITY_URL = "https://health.googleapis.com/v4/users/me/identity"
HTTP_TIMEOUT_SECONDS = 30.0
_USER_ID = re.compile(r"[!-~]{1,200}", re.ASCII)


class SetupError(Exception):
    """``str()`` is a sentence safe to print: no demographics, ids or tokens."""


@dataclass(frozen=True)
class Demographics:
    """From configuration (flags or environment variables), never from code."""

    patient_id: str
    family: str
    given: str
    gender: str
    birth_date: date

    def patient_input(self, google_health_user_id: str | None) -> PatientInput:
        return PatientInput(
            self.patient_id,
            family=self.family,
            given=(self.given,),
            gender=self.gender,
            birth_date=self.birth_date,
            google_health_user_id=google_health_user_id,
        )


def check_demographics(demographics: Demographics) -> None:
    """Run T06's own completeness check before the OAuth flow starts, so a typo in the
    configuration is found before anything is stored. The message names the field only."""
    try:
        check_complete_patient(build_patient(demographics.patient_input(None)).resource)
    except FhirInputError as exc:
        raise SetupError(f"Patient configuration is not usable: {_field_only(exc)}") from None


def _field_only(exc: FhirInputError) -> str:
    # FhirInputError messages name the input and the field and sometimes quote the value
    # (e.g. "gender 'x' is not one of ..."). Keep the part before any quote.
    text = str(exc)
    return text.split("'", 1)[0].rstrip(" :") or "invalid value"


def fetch_health_user_id(transport: Transport, access_token: str) -> str:
    """``healthUserId`` from the identity call."""
    try:
        response = transport.request(
            "GET",
            IDENTITY_URL,
            params=None,
            body=None,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except RetryableError:
        raise SetupError("The Google Health identity call got no answer (network).") from None
    if response.status != 200:
        raise SetupError(f"The Google Health identity call answered HTTP {response.status}.")
    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    user_id = body.get("healthUserId") if isinstance(body, dict) else None
    if not isinstance(user_id, str) or not _USER_ID.fullmatch(user_id):
        raise SetupError("The Google Health identity call returned no usable healthUserId.")
    return user_id


def setup_bundle(demographics: Demographics, health_user_id: str) -> dict:
    """T06's setup transaction: the full Patient as a conditional update, and no Device."""
    return patient_setup_transaction(demographics.patient_input(health_user_id), devices=())


def write_patient(client: HealthcareClient, fhir_store: str, bundle: dict) -> str:
    """``executeBundle``; returns ``created`` or ``updated`` from the entry's response status
    (R4 transaction-response: ``201`` when the conditional update created, ``200`` when it
    replaced)."""
    try:
        response = client.execute_bundle(fhir_store, bundle)
    except RetryableError as exc:
        raise SetupError(f"The FHIR store did not answer ({exc}).") from None
    except HealthcareApiError as exc:
        # str() of these names only the operation and the HTTP status (pulsebridge_gcp README).
        raise SetupError(f"The FHIR store refused the Patient write ({exc}).") from None
    entries = response.get("entry") if isinstance(response, dict) else None
    status = ""
    if isinstance(entries, list) and entries and isinstance(entries[0], dict):
        status = str((entries[0].get("response") or {}).get("status", ""))
    if status.startswith("201"):
        return "created"
    if status.startswith("200"):
        return "updated"
    raise SetupError("The FHIR store's answer did not confirm the Patient write.")
