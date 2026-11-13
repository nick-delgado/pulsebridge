# Variable validation tests: each run feeds one bad value and expects that variable's validation
# block to reject it. A run fails if the plan succeeds, or fails for a different reason.

mock_provider "google" {}

variables {
  project_id = "example-project-123"
}

run "accepts_defaults" {
  command = plan
}

run "rejects_bad_project_id" {
  command = plan
  variables {
    project_id = "Example_Project"
  }
  expect_failures = [var.project_id]
}

run "rejects_zone_as_region" {
  command = plan
  variables {
    region = "us-central1-a"
  }
  expect_failures = [var.region]
}

run "rejects_region_without_number" {
  command = plan
  variables {
    region = "uscentral"
  }
  expect_failures = [var.region]
}

run "rejects_invalid_edge_cidr" {
  command = plan
  variables {
    edge_subnet_cidr = "10.10.0.300/24"
  }
  expect_failures = [var.edge_subnet_cidr]
}

run "rejects_edge_cidr_without_prefix" {
  command = plan
  variables {
    edge_subnet_cidr = "10.10.0.0"
  }
  expect_failures = [var.edge_subnet_cidr]
}

run "rejects_serverless_subnet_smaller_than_26" {
  command = plan
  variables {
    serverless_subnet_cidr = "10.10.1.0/28"
  }
  expect_failures = [var.serverless_subnet_cidr]
}

run "accepts_serverless_subnet_of_exactly_26" {
  command = plan
  variables {
    serverless_subnet_cidr = "10.10.1.0/26"
  }
}

run "rejects_overlapping_subnets" {
  command = plan
  variables {
    edge_subnet_cidr       = "10.10.0.0/16"
    serverless_subnet_cidr = "10.10.1.0/24"
  }
  expect_failures = [var.serverless_subnet_cidr]
}

run "accepts_adjacent_subnets" {
  command = plan
  variables {
    edge_subnet_cidr       = "10.10.0.0/24"
    serverless_subnet_cidr = "10.10.1.0/26"
  }
}

run "rejects_port_out_of_range" {
  command = plan
  variables {
    mllp_port = 70000
  }
  expect_failures = [var.mllp_port]
}

run "rejects_non_service_account_email" {
  command = plan
  variables {
    edge_vm_service_account_email = "someone@example.com"
  }
  expect_failures = [var.edge_vm_service_account_email]
}

run "rejects_bad_name_prefix" {
  command = plan
  variables {
    name_prefix = "Pulse_Bridge"
  }
  expect_failures = [var.name_prefix]
}
