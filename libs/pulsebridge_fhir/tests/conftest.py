"""Shared test helpers for pulsebridge_fhir. All data is synthetic."""

import pytest

# Rules of the base R4 vital-signs profile (http://hl7.org/fhir/StructureDefinition/vitalsigns,
# FHIR 4.0.1), written out from its StructureDefinition. They are the spec, not a restatement
# of our code; the HL7 validator in T08 checks the same rules again.
VS_CATEGORY_SYSTEM = "http://terminology.hl7.org/CodeSystem/observation-category"
UCUM = "http://unitsofmeasure.org"
# ValueSet ucum-vitals-common, bound "required" to Observation.component.value[x].
UCUM_VITALS_COMMON = {
    "%", "cm", "[in_i]", "kg", "g", "[lb_av]", "Cel", "[degF]", "mm[Hg]", "/min", "kg/m2", "m2",
}  # fmt: skip


def _check_quantity(q: dict, where: str) -> None:
    for name in ("value", "unit", "system", "code"):
        assert name in q, f"{where}.{name} is required by the vital-signs profiles"
    assert q["system"] == UCUM, f"{where}.system must be UCUM"
    assert isinstance(q["value"], int | float) and not isinstance(q["value"], bool)


def check_vital_signs_profile(obs: dict) -> None:
    """Assert an Observation meets the base R4 vital-signs profile."""
    assert obs["resourceType"] == "Observation"
    assert obs.get("status") in {"registered", "preliminary", "final", "amended", "corrected"}
    vscat = [
        c
        for cat in obs.get("category", [])
        for c in cat.get("coding", [])
        if c.get("system") == VS_CATEGORY_SYSTEM and c.get("code") == "vital-signs"
    ]
    assert len(vscat) >= 1, "category slice VSCat (vital-signs) is required"
    assert obs.get("code", {}).get("coding"), "code is required"
    assert obs.get("subject", {}).get("reference"), "subject is required"
    has_dt = "effectiveDateTime" in obs
    has_period = "effectivePeriod" in obs
    assert has_dt != has_period, "exactly one effective[x], dateTime or Period"
    if has_dt:
        assert len(obs["effectiveDateTime"]) >= 10, "vs-1: precise to the day"
    # vs-2: no component and no hasMember -> a value or a dataAbsentReason.
    if not obs.get("component") and not obs.get("hasMember"):
        assert "valueQuantity" in obs or "dataAbsentReason" in obs
    if "valueQuantity" in obs:
        _check_quantity(obs["valueQuantity"], "valueQuantity")
    for i, comp in enumerate(obs.get("component", [])):
        assert comp.get("code", {}).get("coding"), f"component[{i}].code is required"
        # vs-3: a component has a value or a dataAbsentReason.
        assert "valueQuantity" in comp or "dataAbsentReason" in comp
        if "valueQuantity" in comp:
            _check_quantity(comp["valueQuantity"], f"component[{i}].valueQuantity")
            assert comp["valueQuantity"]["code"] in UCUM_VITALS_COMMON


@pytest.fixture
def vital_signs_profile():
    return check_vital_signs_profile
