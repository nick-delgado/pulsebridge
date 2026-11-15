# THIS FILE IS IDENTICAL IN infra/envs/dev AND infra/envs/prod.
#
# The cuff path breaks SILENTLY if module ingestion (v2-to-fhir) and module phr (cuff-sim,
# fhir-proxy) get different patients: cuff-sim stamps its patient into PID-3 of every message and
# v2-to-fhir refuses a PID-3 that is not its own (and, when an authority is set, a PID-3.4 that
# differs). module.stack reports exactly what it passed to each (output patient_wiring); this
# check compares them on every plan and apply. A `check` warns, it does not stop the run; the
# per-service planned values are also compared by infra/envs/_stack/tests/plan_scan.py.
check "one_patient_for_the_cuff_path" {
  assert {
    condition = (
      module.stack.patient_wiring.ingestion == module.stack.patient_wiring.phr
      && module.stack.patient_wiring.ingestion.assigning_authority != null
    )
    error_message = "module ingestion and module phr were given different patients (or no assigning authority): v2-to-fhir would refuse every cuff reading. Both must come from the one patient_id and patient_assigning_authority variables."
  }
}
