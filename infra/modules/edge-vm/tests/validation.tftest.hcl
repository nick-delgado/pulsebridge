# Input validation tests: each run sets one bad value and expects exactly that variable (or the
# instance precondition) to fail. A validation that stops rejecting its bad value makes the run
# fail, because the expected failure does not happen.

mock_provider "google" {}

variables {
  project_id            = "example-project-123"
  zone                  = "us-central1-a"
  edge_subnet_id        = "projects/example-project-123/regions/us-central1/subnetworks/pulsebridge-edge"
  network_tag           = "pulsebridge-edge-vm"
  service_account_email = "pulsebridge-edge-vm@example-project-123.iam.gserviceaccount.com"
  dockerhub_mirror_url  = "us-central1-docker.pkg.dev/example-project-123/pulsebridge-dockerhub"
  hl7v2_store_flags = {
    hl7_v2_project_id  = "example-project-123"
    hl7_v2_location_id = "us-central1"
    hl7_v2_dataset_id  = "example-dataset"
    hl7_v2_store_id    = "example-hl7v2-store"
  }
  secret_files = {
    "mariadb-root-password" = "projects/example-project-123/secrets/pulsebridge-db-root-password"
  }
}

run "valid_inputs_plan" {
  command = plan
}

run "adapter_image_tag_is_rejected" {
  command = plan
  variables {
    mllp_adapter_image = "gcr.io/cloud-healthcare-containers/mllp-adapter:latest"
  }
  expect_failures = [var.mllp_adapter_image]
}

run "adapter_image_without_digest_is_rejected" {
  command = plan
  variables {
    mllp_adapter_image = "gcr.io/cloud-healthcare-containers/mllp-adapter"
  }
  expect_failures = [var.mllp_adapter_image]
}

run "mariadb_tag_only_is_rejected" {
  command = plan
  variables {
    mariadb_image = "library/mariadb:11.8"
  }
  expect_failures = [var.mariadb_image]
}

run "default_compute_account_is_rejected" {
  command = plan
  variables {
    service_account_email = "123456789012-compute@developer.gserviceaccount.com"
  }
  expect_failures = [var.service_account_email]
}

run "store_path_instead_of_short_name_is_rejected" {
  command = plan
  variables {
    hl7v2_store_flags = {
      hl7_v2_project_id  = "example-project-123"
      hl7_v2_location_id = "us-central1"
      hl7_v2_dataset_id  = "example-dataset"
      hl7_v2_store_id    = "projects/example-project-123/locations/us-central1/datasets/example-dataset/hl7V2Stores/example-hl7v2-store"
    }
  }
  expect_failures = [var.hl7v2_store_flags]
}

run "missing_mariadb_secret_is_rejected" {
  command = plan
  variables {
    secret_files = {
      "openemr-db-password" = "projects/example-project-123/secrets/pulsebridge-openemr-db-password"
    }
  }
  expect_failures = [var.secret_files]
}

run "secret_with_version_is_rejected" {
  command = plan
  variables {
    secret_files = {
      "mariadb-root-password" = "projects/example-project-123/secrets/pulsebridge-db-root-password/versions/1"
    }
  }
  expect_failures = [var.secret_files]
}

run "non_cos_image_is_rejected" {
  command = plan
  variables {
    boot_image = "projects/debian-cloud/global/images/family/debian-12"
  }
  expect_failures = [var.boot_image]
}

run "zone_outside_subnet_region_is_rejected" {
  command = plan
  variables {
    zone = "us-east1-b"
  }
  expect_failures = [google_compute_instance.this]
}

run "region_instead_of_zone_is_rejected" {
  command = plan
  variables {
    zone = "us-central1"
  }
  expect_failures = [var.zone]
}

run "bad_mirror_url_is_rejected" {
  command = plan
  variables {
    dockerhub_mirror_url = "docker.io/library"
  }
  expect_failures = [var.dockerhub_mirror_url]
}

run "bad_hook_name_is_rejected" {
  command = plan
  variables {
    hooks = { "Open EMR" = "true" }
  }
  expect_failures = [var.hooks]
}
