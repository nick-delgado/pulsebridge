"""Service configuration, read from environment variables (set by Terraform on Cloud Run, T22).

Everything is checked at start-up, so a bad value fails the deploy instead of every request.
Nothing here has a default that names a deployment: a clone sets its own project, stores, host
and patient. The defaults that do exist are software names (T07's ``CUFFSIM`` / ``PULSEBRIDGE``)
and operating limits (timeouts, the rate limit).

Error messages name the variable, never its value: some values (the patient identifier and name)
are personal data in a real deployment.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

# The service's own name: the AuditEvent's sender (agent) and observer (T31). It names software,
# not a person or a deployment, so it is fixed like T07's default MSH-3.
SERVICE_NAME = "cuff-sim"


@dataclass(frozen=True)
class Variable:
    description: str
    default: str | None = None  # None: required


# Environment variable -> what it is (README table for T22).
VARIABLES: dict[str, Variable] = {
    "GCP_PROJECT_ID": Variable("Project that holds the Healthcare dataset"),
    "HEALTHCARE_LOCATION": Variable("Region of the dataset, e.g. us-central1"),
    "HEALTHCARE_DATASET_ID": Variable("Healthcare dataset id"),
    "HL7V2_STORE_ID": Variable("HL7v2 store id: where readings go (both transports)"),
    "AUDIT_FHIR_STORE_ID": Variable(
        "Audit FHIR store id (same dataset): where send-attempt AuditEvents go (D15)"
    ),
    "MLLP_HOST": Variable("Host name or IP address of the MLLP adapter on the edge VM"),
    "MLLP_PORT": Variable("TCP port of the MLLP adapter, 1 to 65535", "2575"),
    "MLLP_TIMEOUT_SECONDS": Variable(
        "One deadline for the whole MLLP exchange: connect, write and the complete ACK frame", "5"
    ),
    "HTTPS_TIMEOUT_SECONDS": Variable("Seconds allowed for one HTTPS ingest or audit write", "10"),
    "PATIENT_ID": Variable("PID-3.1: the configured patient's identifier (the setup step's id)"),
    "PATIENT_ASSIGNING_AUTHORITY": Variable("PID-3.4: the system that issued PATIENT_ID"),
    "PATIENT_FAMILY_NAME": Variable("PID-5.1: the configured patient's family name"),
    "PATIENT_GIVEN_NAME": Variable("PID-5.2: the configured patient's given name"),
    "SENDING_APPLICATION": Variable("MSH-3: sending application", "CUFFSIM"),
    "SENDING_FACILITY": Variable("MSH-4: sending facility", "PULSEBRIDGE"),
    "RATE_LIMIT_PER_MINUTE": Variable(
        "Sends allowed per minute per instance (token refill rate), 1 to 600", "6"
    ),
    "RATE_LIMIT_BURST": Variable("Sends allowed back to back per instance (bucket size)", "3"),
}

# Bounds on the operating limits. Every wait the service makes is bounded by these.
MAX_TIMEOUT_SECONDS = 60.0
MAX_RATE_PER_MINUTE = 600
MAX_BURST = 100

# HL7v2 text values from configuration: printable ASCII without a backslash (T07's rule: MSH-18
# is empty, so the receiver assumes ASCII, and "\\" is HL7's escape character), at most this many
# characters. The limits are ours and below what hl7apy enforces (199 for PID-3.1 and PID-5, 20
# for PID-3.4 and the HD.1 of MSH-3/MSH-4), so a value that is too long is refused here, by name,
# and hl7apy never gets to raise an error that quotes it.
TEXT_LIMITS = {
    "PATIENT_ID": 64,  # also T31/T06's identifier, so kept short
    "PATIENT_ASSIGNING_AUTHORITY": 20,
    "PATIENT_FAMILY_NAME": 80,
    "PATIENT_GIVEN_NAME": 80,
    "SENDING_APPLICATION": 20,
    "SENDING_FACILITY": 20,
}
_HL7_TEXT = re.compile(r"[\x20-\x5b\x5d-\x7e]+", re.ASCII)

# A DNS host name (RFC 1123 labels); IP addresses are checked with ipaddress instead.
_HOST_NAME = re.compile(
    r"(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*",
    re.ASCII,
)


class ConfigError(ValueError):
    """A variable is missing or unusable. The message names variables, never their values."""


@dataclass(frozen=True)
class ServiceConfig:
    project_id: str
    location: str
    dataset_id: str
    hl7v2_store_id: str
    audit_fhir_store_id: str
    mllp_host: str
    mllp_port: int
    mllp_timeout: float
    https_timeout: float
    patient_id: str
    patient_assigning_authority: str
    patient_family_name: str
    patient_given_name: str
    sending_application: str
    sending_facility: str
    rate_limit_per_minute: int
    rate_limit_burst: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServiceConfig:
        env = os.environ if env is None else env
        values: dict[str, str] = {}
        missing = []
        for name, var in VARIABLES.items():
            value = (env.get(name) or "").strip()
            if not value:
                if var.default is None:
                    missing.append(name)
                    continue
                value = var.default
            values[name] = value
        if missing:
            raise ConfigError(f"missing environment variables: {', '.join(missing)}")
        for name, limit in TEXT_LIMITS.items():
            _text(name, values[name], limit)
        if values["AUDIT_FHIR_STORE_ID"] == values["HL7V2_STORE_ID"]:
            # Not strictly impossible (FHIR and HL7v2 stores have separate namespaces), but
            # almost certainly a copy-paste mistake in the deployment.
            raise ConfigError("AUDIT_FHIR_STORE_ID must differ from HL7V2_STORE_ID")
        return cls(
            project_id=values["GCP_PROJECT_ID"],
            location=values["HEALTHCARE_LOCATION"],
            dataset_id=values["HEALTHCARE_DATASET_ID"],
            hl7v2_store_id=values["HL7V2_STORE_ID"],
            audit_fhir_store_id=values["AUDIT_FHIR_STORE_ID"],
            mllp_host=_host("MLLP_HOST", values["MLLP_HOST"]),
            mllp_port=_int("MLLP_PORT", values["MLLP_PORT"], 1, 65535),
            mllp_timeout=_seconds("MLLP_TIMEOUT_SECONDS", values["MLLP_TIMEOUT_SECONDS"]),
            https_timeout=_seconds("HTTPS_TIMEOUT_SECONDS", values["HTTPS_TIMEOUT_SECONDS"]),
            patient_id=values["PATIENT_ID"],
            patient_assigning_authority=values["PATIENT_ASSIGNING_AUTHORITY"],
            patient_family_name=values["PATIENT_FAMILY_NAME"],
            patient_given_name=values["PATIENT_GIVEN_NAME"],
            sending_application=values["SENDING_APPLICATION"],
            sending_facility=values["SENDING_FACILITY"],
            rate_limit_per_minute=_int(
                "RATE_LIMIT_PER_MINUTE", values["RATE_LIMIT_PER_MINUTE"], 1, MAX_RATE_PER_MINUTE
            ),
            rate_limit_burst=_int("RATE_LIMIT_BURST", values["RATE_LIMIT_BURST"], 1, MAX_BURST),
        )


def _int(name: str, text: str, low: int, high: int) -> int:
    if not re.fullmatch(r"[0-9]{1,6}", text, re.ASCII):
        raise ConfigError(f"{name} must be a whole number from {low} to {high}")
    value = int(text)
    if not low <= value <= high:
        raise ConfigError(f"{name} must be a whole number from {low} to {high}")
    return value


def _seconds(name: str, text: str) -> float:
    # Plain decimal only: float() would also accept "inf", "nan" and "1e9".
    if not re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,3})?", text, re.ASCII):
        raise ConfigError(f"{name} must be a number of seconds, above 0 and at most 60")
    value = float(text)
    if not 0 < value <= MAX_TIMEOUT_SECONDS:
        raise ConfigError(f"{name} must be a number of seconds, above 0 and at most 60")
    return value


def _text(name: str, text: str, limit: int) -> None:
    if len(text) > limit or not _HL7_TEXT.fullmatch(text):
        raise ConfigError(
            f"{name} must be 1 to {limit} characters of printable ASCII without a backslash"
        )


def _host(name: str, text: str) -> str:
    try:
        ipaddress.ip_address(text)
        return text
    except ValueError:
        pass
    if not _HOST_NAME.fullmatch(text):
        raise ConfigError(f"{name} must be a host name or an IP address")
    return text
