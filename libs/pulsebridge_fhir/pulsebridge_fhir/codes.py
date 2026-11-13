"""Code systems, codes and units used by the builders.

Every constant names the standard it comes from. The base R4 Vital Signs profiles
(http://hl7.org/fhir/StructureDefinition/vitalsigns, .../heartrate, .../bp) fix several of these
values; where a profile fixes a value, the comment says so.
"""

from typing import Final

# --- Code systems (canonical URIs defined by HL7) ------------------------------------------------

LOINC: Final = "http://loinc.org"
UCUM: Final = "http://unitsofmeasure.org"
OBSERVATION_CATEGORY: Final = "http://terminology.hl7.org/CodeSystem/observation-category"
V2_IDENTIFIER_TYPE: Final = "http://terminology.hl7.org/CodeSystem/v2-0203"  # HL7v2 table 0203
PROVENANCE_PARTICIPANT_TYPE: Final = (
    "http://terminology.hl7.org/CodeSystem/provenance-participant-type"
)
PROVENANCE_ENTITY_ROLE: Final = "http://hl7.org/fhir/provenance-entity-role"
DEVICE_NAME_TYPE: Final = "http://hl7.org/fhir/device-nametype"

# --- Profiles claimed in Observation.meta.profile -------------------------------------------------
# Claiming the base profiles makes the HL7 validator (task T08) check them, not only the resource.

PROFILE_HEART_RATE: Final = "http://hl7.org/fhir/StructureDefinition/heartrate"
PROFILE_BLOOD_PRESSURE: Final = "http://hl7.org/fhir/StructureDefinition/bp"

# --- PulseBridge's own namespaces -----------------------------------------------------------------
# Identifier systems and local code systems owned by this software, not by any deployment or
# person: every clone uses the same URNs, so nothing here identifies the author. A deployer who
# wants systems under their own domain can change them in one place.

PB_PATIENT_ID: Final = "urn:pulsebridge:patient"
PB_GOOGLE_HEALTH_USER_ID: Final = "urn:pulsebridge:google-health-user-id"
PB_DEVICE_ID: Final = "urn:pulsebridge:device"
PB_OBSERVATION_ID: Final = "urn:pulsebridge:observation"
PB_SOURCE_PLATFORM: Final = "urn:pulsebridge:source-platform"
PB_RECORDING_METHOD: Final = "urn:pulsebridge:recording-method"
PB_SOURCE_RECORD_PREFIX: Final = (
    "urn:pulsebridge:source-record:"  # + record type, e.g. hl7v2-message
)


# --- Codings ------------------------------------------------------------------------------------
# Displays are the exact display text from the code system, because the HL7 validator checks them.


def coding(system: str, code: str, display: str | None = None) -> dict:
    """A FHIR Coding: one code from one code system."""
    out = {"system": system, "code": code}
    if display is not None:
        out["display"] = display
    return out


# Observation.category, slice VSCat: the vital-signs profile fixes system and code.
VITAL_SIGNS_CATEGORY: Final = coding(OBSERVATION_CATEGORY, "vital-signs", "Vital Signs")

# LOINC codes. The heartrate profile fixes 8867-4; the bp profile fixes 85354-9 for the panel and
# 8480-6 / 8462-4 for its two required components.
HEART_RATE: Final = coding(LOINC, "8867-4", "Heart rate")
BP_PANEL: Final = coding(LOINC, "85354-9", "Blood pressure panel with all children optional")
BP_SYSTOLIC: Final = coding(LOINC, "8480-6", "Systolic blood pressure")
BP_DIASTOLIC: Final = coding(LOINC, "8462-4", "Diastolic blood pressure")

# Heart-rate window minimum and maximum (components of the heart-rate Observation).
#
# LOINC has no "minimum" or "maximum" variant of 8867-4 itself. Its timed variants (8873-2
# "Heart rate 24 hour maximum", 8883-1 "Heart rate 24 hour minimum", and 1, 8, 10, 12 hour codes)
# carry a fixed duration in the TIME axis and so are wrong for a five-minute window. The codes
# below use TIME = "XXX^max" / "XXX^min": the interval is stated elsewhere, which here is
# Observation.effectivePeriod. Their METHOD is "Pedometer": they belong to LOINC's pedometer
# tracking family (panel 55413-9, with 55425-3 mean heart rate and 55423-8 step count), the codes
# used for wrist-worn activity trackers. That method is the one imperfect fit; it is the closest
# LOINC offers for a min/max over an arbitrary interval.
# Verification: confirmed from secondary listings of the LOINC table (findacode.com, the NLM VSAC
# "Heart Rate" value set, the HL7 Physical Activity IG) through web search; loinc.org itself was not
# reachable from the build environment. See README.md, "Heart-rate component codes".
HEART_RATE_MAX: Final = coding(LOINC, "55426-1", "Heart rate unspecified time maximum by Pedometer")
HEART_RATE_MIN: Final = coding(LOINC, "96935-2", "Heart rate unspecified time minimum by Pedometer")

# UCUM units. The heartrate profile fixes valueQuantity.code to "/min" and the bp profile fixes the
# components to "mm[Hg]"; both are in the required ucum-vitals-common value set. OpenEMR also
# requires exactly these codes (spike S1).
UCUM_PER_MINUTE: Final = "/min"
UCUM_MM_HG: Final = "mm[Hg]"
UNIT_DISPLAY: Final = {UCUM_PER_MINUTE: "beats/minute", UCUM_MM_HG: "mmHg"}

# Patient.identifier.type "PT" (Patient external identifier). OpenEMR stores an identifier only if
# it carries this type (spike S1, finding 4).
IDENTIFIER_TYPE_PT: Final = coding(V2_IDENTIFIER_TYPE, "PT", "Patient external identifier")

# Provenance.agent.type codes.
AGENT_PERFORMER: Final = coding(PROVENANCE_PARTICIPANT_TYPE, "performer", "Performer")
AGENT_CUSTODIAN: Final = coding(PROVENANCE_PARTICIPANT_TYPE, "custodian", "Custodian")
AGENT_ASSEMBLER: Final = coding(PROVENANCE_PARTICIPANT_TYPE, "assembler", "Assembler")

# Device.deviceName.type
DEVICE_NAME_USER_FRIENDLY: Final = "user-friendly-name"
