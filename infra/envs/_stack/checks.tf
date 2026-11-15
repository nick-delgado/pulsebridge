# `check` blocks run on every plan and apply. A failing check is a WARNING: it does not stop the
# run, it tells the person running it. The root's checks.tf holds the patient check (so a test
# can drive it with override_module); this one is about cost.

check "e2_micro_free_tier_region" {
  assert {
    # The e2-micro free tier: one non-preemptible e2-micro per billing account, only in these
    # three regions (edge-vm README, "Cost"). Elsewhere the VM is billed by the hour.
    condition     = contains(["us-west1", "us-central1", "us-east1"], var.region)
    error_message = "region is outside the e2-micro free tier (us-west1, us-central1, us-east1): the edge VM will be billed by the hour (about $6 a month, edge-vm README, \"Cost\")."
  }
}

check "cloud_nat_is_billed_hourly" {
  assert {
    condition     = !var.enable_cloud_nat
    error_message = "enable_cloud_nat = true: Cloud NAT is billed by the hour (about $4.70 a month plus $0.045 per GiB, project-base README). Use it only if the edge VM cannot pull through Private Google Access."
  }
}
