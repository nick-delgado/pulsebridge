"""PulseBridge end-to-end smoke test (task T29).

One command that runs against a DEPLOYED stack through its public front door only (the
fhir-proxy behind the Firebase Hosting rewrite, ``<dashboard_url>/api``) and asserts from the
APIs. See ``tests/e2e/README.md`` for what it checks, what it writes and how to run it.

Modules:

- :mod:`pulsebridge_smoke.cli`: arguments, the session routes' checks, the exit status;
- :mod:`pulsebridge_smoke.session`: the signing key from Secret Manager and the session token
  signed with it (D16);
- :mod:`pulsebridge_smoke.http`: the HTTP transport (no redirects) and the API client (the
  ``__session`` cookie, ``Origin`` on a POST);
- :mod:`pulsebridge_smoke.redact`: keeps the signing key and the session token out of all output;
- :mod:`pulsebridge_smoke.report`: check results, the run's trail, the summary;
- :mod:`pulsebridge_smoke.hl7`: reads the HL7v2 message cuff-sim sent (hl7apy, via T07's parser);
- :mod:`pulsebridge_smoke.fhir`: the FHIR R4 rules a blood-pressure Observation must meet;
- :mod:`pulsebridge_smoke.cuff_path`: Part A, the cuff path (MLLP to FHIR to OpenEMR);
- :mod:`pulsebridge_smoke.first_sync`: Part B, health-sync's first-sync checks (real data only).
"""
