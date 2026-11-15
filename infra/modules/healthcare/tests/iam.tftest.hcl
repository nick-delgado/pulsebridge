# Store-level bindings for the application service accounts. The input is module identity's
# map as it is: hyphenated account names, including keys this module does not use.
# Three stores: the main FHIR store (health data), the audit FHIR store (D15) and the HL7v2
# store. Whole-table assertions include the store ID, so a binding on the wrong store fails.

mock_provider "google" {
  override_data {
    target = data.google_project.this
    values = { number = "123456789012" }
  }
  override_resource {
    target = google_healthcare_fhir_store.this
    values = { id = "projects/example-project/locations/us-central1/datasets/pulsebridge/fhirStores/pulsebridge-fhir" }
  }
  override_resource {
    target = google_healthcare_fhir_store.audit
    values = { id = "projects/example-project/locations/us-central1/datasets/pulsebridge/fhirStores/pulsebridge-fhir-audit" }
  }
  override_resource {
    target = google_healthcare_hl7_v2_store.this
    values = { id = "projects/example-project/locations/us-central1/datasets/pulsebridge/hl7V2Stores/pulsebridge-hl7v2" }
  }
}

variables {
  project_id = "example-project"
}

run "identity_map_gets_exactly_the_expected_roles" {
  command = apply

  variables {
    # Shaped like module identity's service_account_emails output, extra keys included.
    store_iam_service_accounts = {
      "health-sync"       = "health-sync@example-project.iam.gserviceaccount.com"
      "seeder"            = "seeder@example-project.iam.gserviceaccount.com"
      "v2-to-fhir"        = "v2-to-fhir@example-project.iam.gserviceaccount.com"
      "fhir-proxy"        = "fhir-proxy@example-project.iam.gserviceaccount.com"
      "edge-vm"           = "edge-vm@example-project.iam.gserviceaccount.com"
      "cuff-sim"          = "cuff-sim@example-project.iam.gserviceaccount.com"
      "scheduler-invoker" = "scheduler-invoker@example-project.iam.gserviceaccount.com"
      "pubsub-push"       = "pubsub-push@example-project.iam.gserviceaccount.com"
    }
  }

  assert {
    condition = {
      for k, b in google_healthcare_fhir_store_iam_member.app : k => "${b.fhir_store_id} ${b.member} ${b.role}"
      } == {
      "health-sync" = "projects/example-project/locations/us-central1/datasets/pulsebridge/fhirStores/pulsebridge-fhir serviceAccount:health-sync@example-project.iam.gserviceaccount.com roles/healthcare.fhirResourceEditor"
      "seeder"      = "projects/example-project/locations/us-central1/datasets/pulsebridge/fhirStores/pulsebridge-fhir serviceAccount:seeder@example-project.iam.gserviceaccount.com roles/healthcare.fhirResourceEditor"
      "v2-to-fhir"  = "projects/example-project/locations/us-central1/datasets/pulsebridge/fhirStores/pulsebridge-fhir serviceAccount:v2-to-fhir@example-project.iam.gserviceaccount.com roles/healthcare.fhirResourceEditor"
      "fhir-proxy"  = "projects/example-project/locations/us-central1/datasets/pulsebridge/fhirStores/pulsebridge-fhir serviceAccount:fhir-proxy@example-project.iam.gserviceaccount.com roles/healthcare.fhirResourceReader"
    }
    error_message = "Main FHIR store bindings must be exactly: health-sync, seeder and v2-to-fhir editor, fhir-proxy reader (no cuff-sim, D15)."
  }
  assert {
    condition = {
      for k, b in google_healthcare_fhir_store_iam_member.audit : k => "${b.fhir_store_id} ${b.member} ${b.role}"
      } == {
      "cuff-sim"   = "projects/example-project/locations/us-central1/datasets/pulsebridge/fhirStores/pulsebridge-fhir-audit serviceAccount:cuff-sim@example-project.iam.gserviceaccount.com roles/healthcare.fhirResourceEditor"
      "fhir-proxy" = "projects/example-project/locations/us-central1/datasets/pulsebridge/fhirStores/pulsebridge-fhir-audit serviceAccount:fhir-proxy@example-project.iam.gserviceaccount.com roles/healthcare.fhirResourceReader"
    }
    error_message = "Audit FHIR store bindings must be exactly: cuff-sim editor, fhir-proxy reader, on the audit store."
  }
  assert {
    condition = {
      for k, b in google_healthcare_hl7_v2_store_iam_member.app : k => "${b.hl7_v2_store_id} ${b.member} ${b.role}"
      } == {
      "v2-to-fhir" = "projects/example-project/locations/us-central1/datasets/pulsebridge/hl7V2Stores/pulsebridge-hl7v2 serviceAccount:v2-to-fhir@example-project.iam.gserviceaccount.com roles/healthcare.hl7V2Consumer"
      "fhir-proxy" = "projects/example-project/locations/us-central1/datasets/pulsebridge/hl7V2Stores/pulsebridge-hl7v2 serviceAccount:fhir-proxy@example-project.iam.gserviceaccount.com roles/healthcare.hl7V2Consumer"
      "edge-vm"    = "projects/example-project/locations/us-central1/datasets/pulsebridge/hl7V2Stores/pulsebridge-hl7v2 serviceAccount:edge-vm@example-project.iam.gserviceaccount.com roles/healthcare.hl7V2Ingest"
      "cuff-sim"   = "projects/example-project/locations/us-central1/datasets/pulsebridge/hl7V2Stores/pulsebridge-hl7v2 serviceAccount:cuff-sim@example-project.iam.gserviceaccount.com roles/healthcare.hl7V2Ingest"
    }
    error_message = "HL7v2 store bindings must be exactly: v2-to-fhir and fhir-proxy consumer, edge-vm and cuff-sim ingest."
  }
}

run "unknown_keys_are_ignored" {
  command = apply

  variables {
    store_iam_service_accounts = {
      "scheduler-invoker" = "scheduler-invoker@example-project.iam.gserviceaccount.com"
      "pubsub-push"       = "pubsub-push@example-project.iam.gserviceaccount.com"
    }
  }

  assert {
    condition     = length(google_healthcare_fhir_store_iam_member.app) == 0 && length(google_healthcare_fhir_store_iam_member.audit) == 0 && length(google_healthcare_hl7_v2_store_iam_member.app) == 0
    error_message = "Keys this module does not use must create no binding."
  }
}

run "old_underscore_keys_create_nothing" {
  command = apply

  variables {
    store_iam_service_accounts = {
      health_sync = "health-sync@example-project.iam.gserviceaccount.com"
      v2_to_fhir  = "v2-to-fhir@example-project.iam.gserviceaccount.com"
      fhir_proxy  = "fhir-proxy@example-project.iam.gserviceaccount.com"
      edge_vm     = "edge-vm@example-project.iam.gserviceaccount.com"
      cuff_sim    = "cuff-sim@example-project.iam.gserviceaccount.com"
    }
  }

  assert {
    condition     = length(google_healthcare_fhir_store_iam_member.app) == 0 && length(google_healthcare_fhir_store_iam_member.audit) == 0 && length(google_healthcare_hl7_v2_store_iam_member.app) == 0
    error_message = "The old underscore keys must create no binding."
  }
}

run "seeder_gets_fhir_editor_only" {
  command = apply

  variables {
    store_iam_service_accounts = {
      "seeder" = "seeder@example-project.iam.gserviceaccount.com"
    }
  }

  assert {
    condition     = keys(google_healthcare_fhir_store_iam_member.app) == ["seeder"] && google_healthcare_fhir_store_iam_member.app["seeder"].role == "roles/healthcare.fhirResourceEditor"
    error_message = "seeder must get exactly FHIR resource editor on the FHIR store."
  }
  assert {
    condition     = length(google_healthcare_hl7_v2_store_iam_member.app) == 0 && length(google_healthcare_fhir_store_iam_member.audit) == 0
    error_message = "seeder must get nothing on the HL7v2 store or the audit store."
  }
}

run "one_account_gets_only_its_own_binding" {
  command = apply

  variables {
    store_iam_service_accounts = {
      "edge-vm" = "edge-vm@example-project.iam.gserviceaccount.com"
    }
  }

  assert {
    condition     = length(google_healthcare_fhir_store_iam_member.app) == 0 && length(google_healthcare_fhir_store_iam_member.audit) == 0
    error_message = "edge-vm alone must create no binding on either FHIR store."
  }
  assert {
    condition     = keys(google_healthcare_hl7_v2_store_iam_member.app) == ["edge-vm"] && google_healthcare_hl7_v2_store_iam_member.app["edge-vm"].role == "roles/healthcare.hl7V2Ingest"
    error_message = "edge-vm alone must create exactly one HL7v2 ingest binding."
  }
}

# T32 / D15: the cuff simulator writes one AuditEvent per send attempt to the AUDIT store, and
# has no binding at all on the main store. Its only other binding is HL7v2 ingest.
run "cuff_sim_writes_only_to_the_audit_store" {
  command = apply

  variables {
    store_iam_service_accounts = {
      "cuff-sim" = "cuff-sim@example-project.iam.gserviceaccount.com"
    }
  }

  assert {
    condition     = length(google_healthcare_fhir_store_iam_member.app) == 0
    error_message = "cuff-sim must have no binding on the main FHIR store, which holds health data (D15)."
  }
  assert {
    condition = {
      for k, b in google_healthcare_fhir_store_iam_member.audit : k => "${b.fhir_store_id} ${b.member} ${b.role}"
      } == {
      "cuff-sim" = "projects/example-project/locations/us-central1/datasets/pulsebridge/fhirStores/pulsebridge-fhir-audit serviceAccount:cuff-sim@example-project.iam.gserviceaccount.com roles/healthcare.fhirResourceEditor"
    }
    error_message = "cuff-sim alone must get exactly one FHIR binding: roles/healthcare.fhirResourceEditor on the audit store."
  }
  assert {
    condition = {
      for k, b in google_healthcare_hl7_v2_store_iam_member.app : k => "${b.hl7_v2_store_id} ${b.member} ${b.role}"
      } == {
      "cuff-sim" = "projects/example-project/locations/us-central1/datasets/pulsebridge/hl7V2Stores/pulsebridge-hl7v2 serviceAccount:cuff-sim@example-project.iam.gserviceaccount.com roles/healthcare.hl7V2Ingest"
    }
    error_message = "cuff-sim alone must get exactly one HL7v2 binding: roles/healthcare.hl7V2Ingest on the HL7v2 store."
  }
}

# fhir-proxy is the public front door (D10), so it is reader on both FHIR stores, consumer on
# the HL7v2 store, and nothing else. Exact comparison: any other role (editor, admin) fails.
run "fhir_proxy_is_read_only_on_both_fhir_stores" {
  command = apply

  variables {
    store_iam_service_accounts = {
      "fhir-proxy" = "fhir-proxy@example-project.iam.gserviceaccount.com"
    }
  }

  assert {
    condition = {
      for k, b in google_healthcare_fhir_store_iam_member.app : k => "${b.fhir_store_id} ${b.role}"
      } == {
      "fhir-proxy" = "projects/example-project/locations/us-central1/datasets/pulsebridge/fhirStores/pulsebridge-fhir roles/healthcare.fhirResourceReader"
    }
    error_message = "fhir-proxy must be exactly fhirResourceReader on the main FHIR store."
  }
  assert {
    condition = {
      for k, b in google_healthcare_fhir_store_iam_member.audit : k => "${b.fhir_store_id} ${b.role}"
      } == {
      "fhir-proxy" = "projects/example-project/locations/us-central1/datasets/pulsebridge/fhirStores/pulsebridge-fhir-audit roles/healthcare.fhirResourceReader"
    }
    error_message = "fhir-proxy must be exactly fhirResourceReader on the audit FHIR store; AuditEvents are written by cuff-sim (D12, D15)."
  }
  assert {
    condition = {
      for k, b in google_healthcare_hl7_v2_store_iam_member.app : k => b.role
    } == { "fhir-proxy" = "roles/healthcare.hl7V2Consumer" }
    error_message = "fhir-proxy must have only hl7V2Consumer on the HL7v2 store."
  }
}

# Only cuff-sim and fhir-proxy may appear on the audit store, whichever accounts are passed.
run "no_other_workload_on_the_audit_store" {
  command = apply

  variables {
    store_iam_service_accounts = {
      "health-sync" = "health-sync@example-project.iam.gserviceaccount.com"
      "seeder"      = "seeder@example-project.iam.gserviceaccount.com"
      "v2-to-fhir"  = "v2-to-fhir@example-project.iam.gserviceaccount.com"
      "edge-vm"     = "edge-vm@example-project.iam.gserviceaccount.com"
    }
  }

  assert {
    condition     = length(google_healthcare_fhir_store_iam_member.audit) == 0
    error_message = "health-sync, seeder, v2-to-fhir and edge-vm must get nothing on the audit store."
  }
}
