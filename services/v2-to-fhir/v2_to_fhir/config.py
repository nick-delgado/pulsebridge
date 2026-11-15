"""Service configuration, read from environment variables (set by Terraform on Cloud Run).

Nothing here has a default that names a deployment: a clone must set its own values.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from v2_to_fhir.mapping import MappingConfig

# Environment variable -> what it is. Required unless listed in OPTIONAL.
VARIABLES = {
    "GCP_PROJECT_ID": "Project that holds the Healthcare dataset",
    "HEALTHCARE_LOCATION": "Region of the dataset, e.g. us-central1",
    "HEALTHCARE_DATASET_ID": "Healthcare dataset id",
    "FHIR_STORE_ID": "FHIR R4 store id (inside the dataset)",
    "HL7V2_STORE_ID": "HL7v2 store id (inside the dataset)",
    "PATIENT_ID": "The configured patient: PID-3.1 must equal it (the setup step's patient id)",
    "PATIENT_ASSIGNING_AUTHORITY": "Optional: PID-3.4.1 the matching identifier must carry",
}
OPTIONAL = {"PATIENT_ASSIGNING_AUTHORITY"}


class ConfigError(ValueError):
    """One or more variables are missing; the message names them (never their values)."""


@dataclass(frozen=True)
class ServiceConfig:
    project_id: str
    location: str
    dataset_id: str
    fhir_store_id: str
    hl7v2_store_id: str
    mapping: MappingConfig

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServiceConfig:
        env = os.environ if env is None else env
        values = {name: (env.get(name) or "").strip() for name in VARIABLES}
        missing = [n for n in VARIABLES if n not in OPTIONAL and not values[n]]
        if missing:
            raise ConfigError(f"missing environment variables: {', '.join(missing)}")
        return cls(
            project_id=values["GCP_PROJECT_ID"],
            location=values["HEALTHCARE_LOCATION"],
            dataset_id=values["HEALTHCARE_DATASET_ID"],
            fhir_store_id=values["FHIR_STORE_ID"],
            hl7v2_store_id=values["HL7V2_STORE_ID"],
            mapping=MappingConfig(
                patient_id=values["PATIENT_ID"],
                patient_assigning_authority=values["PATIENT_ASSIGNING_AUTHORITY"] or None,
            ),
        )
