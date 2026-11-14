"""The one exception the library raises for bad input."""


class FhirInputError(ValueError):
    """Input cannot be mapped to a valid FHIR resource.

    The message names the input and the field, for example
    ``heart-rate window: beatsPerMinuteMax (301) is outside 1 to 300``, so a caller such as
    ``v2-to-fhir`` can record it as the reason for an application-level ``error`` outcome.
    Subclasses ValueError, so ``except ValueError`` also catches it.
    """
