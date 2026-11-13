# Behaviour tests for project-base. They run with `terraform test` from the module directory.
#
# mock_provider replaces the real google provider: no credentials are read and no API is called.
# Every run uses `command = plan`, so nothing is created even in the mock. The assertions check
# the planned configuration of each resource.

mock_provider "google" {}

variables {
  # Synthetic values: no real project is involved.
  project_id = "example-project-123"
}

# --- APIs ----------------------------------------------------------------------------------------

run "default_api_list_covers_stack_and_excludes_identity_platform" {
  command = plan

  assert {
    condition     = !contains(keys(google_project_service.this), "identitytoolkit.googleapis.com")
    error_message = "Identity Platform must not be enabled by default (D9)."
  }

  assert {
    condition = length(setsubtract([
      "run.googleapis.com",
      "healthcare.googleapis.com",
      "pubsub.googleapis.com",
      "secretmanager.googleapis.com",
      "compute.googleapis.com",
      "artifactregistry.googleapis.com",
      "cloudscheduler.googleapis.com",
      "cloudbuild.googleapis.com",
      "iam.googleapis.com",
    ], keys(google_project_service.this))) == 0
    error_message = "The default API list is missing a service the stack needs."
  }

  assert {
    condition     = alltrue([for s in google_project_service.this : s.disable_on_destroy == false && s.disable_dependent_services == false])
    error_message = "APIs must stay enabled on destroy."
  }
}

run "api_list_variable_drives_enabled_services" {
  command = plan

  variables {
    enabled_apis = ["run.googleapis.com", "compute.googleapis.com"]
  }

  assert {
    condition     = toset(keys(google_project_service.this)) == toset(["run.googleapis.com", "compute.googleapis.com"])
    error_message = "Exactly the APIs in enabled_apis must be enabled, no more and no fewer."
  }

  assert {
    condition     = google_project_service.this["run.googleapis.com"].service == "run.googleapis.com" && google_project_service.this["run.googleapis.com"].project == "example-project-123"
    error_message = "Each google_project_service must enable its own API in the given project."
  }
}

run "rejects_identity_platform_in_api_list" {
  command = plan

  variables {
    enabled_apis = ["run.googleapis.com", "identitytoolkit.googleapis.com"]
  }

  expect_failures = [var.enabled_apis]
}

run "rejects_malformed_api_name" {
  command = plan

  variables {
    enabled_apis = ["run"]
  }

  expect_failures = [var.enabled_apis]
}

# --- Cloud NAT -----------------------------------------------------------------------------------

run "nat_off_by_default" {
  command = plan

  assert {
    condition     = length(google_compute_router.nat) == 0 && length(google_compute_router_nat.this) == 0
    error_message = "Cloud Router and Cloud NAT must not exist when enable_cloud_nat is false (the default)."
  }

  assert {
    condition     = output.cloud_nat_enabled == false
    error_message = "cloud_nat_enabled output must report false by default."
  }
}

run "nat_created_when_flag_true" {
  command = plan

  variables {
    enable_cloud_nat = true
  }

  assert {
    condition     = length(google_compute_router.nat) == 1 && length(google_compute_router_nat.this) == 1
    error_message = "Cloud Router and Cloud NAT must exist when enable_cloud_nat is true."
  }

  assert {
    condition     = google_compute_router_nat.this[0].source_subnetwork_ip_ranges_to_nat == "LIST_OF_SUBNETWORKS"
    error_message = "NAT must translate only the listed (edge) subnet, not every subnet."
  }
}

# --- Network -------------------------------------------------------------------------------------

run "custom_vpc_with_private_google_access" {
  command = plan

  assert {
    condition     = google_compute_network.this.auto_create_subnetworks == false
    error_message = "The VPC must be custom mode (no auto-created subnets)."
  }

  assert {
    condition     = google_compute_network.this.name != "default"
    error_message = "The module must not use the project's default network."
  }

  assert {
    condition     = google_compute_subnetwork.edge.private_ip_google_access == true
    error_message = "The edge subnet needs Private Google Access so the VM can reach Google APIs and Artifact Registry without NAT."
  }

  assert {
    condition     = google_compute_subnetwork.edge.ip_cidr_range == "10.10.0.0/24" && google_compute_subnetwork.serverless.ip_cidr_range == "10.10.1.0/24"
    error_message = "Subnets must use the configured CIDR ranges."
  }
}

# --- Firewall ------------------------------------------------------------------------------------

run "firewall_default_posture" {
  command = plan

  # With the defaults only the Cloud-Run-to-edge rule allows anything.
  assert {
    condition     = toset(keys(google_compute_firewall.allow)) == toset(["allow-serverless-to-edge"])
    error_message = "By default the only allow rule must be Cloud Run to the edge VM."
  }

  assert {
    condition     = alltrue([for r in google_compute_firewall.allow : !contains(r.source_ranges, "0.0.0.0/0") && !contains(r.source_ranges, "::/0")])
    error_message = "No allow rule may accept traffic from anywhere (0.0.0.0/0 or ::/0)."
  }

  assert {
    condition     = google_compute_firewall.deny_all_ingress.priority == 65534 && one(google_compute_firewall.deny_all_ingress.deny).protocol == "all" && length(google_compute_firewall.deny_all_ingress.allow) == 0
    error_message = "An explicit lowest-priority deny-all ingress rule must exist and allow nothing."
  }
}

run "mllp_and_https_open_only_from_serverless_subnet" {
  command = plan

  assert {
    condition     = google_compute_firewall.allow["allow-serverless-to-edge"].source_ranges == toset(["10.10.1.0/24"])
    error_message = "The MLLP/HTTPS rule's only source must be the serverless subnet range."
  }

  assert {
    condition     = toset(one(google_compute_firewall.allow["allow-serverless-to-edge"].allow).ports) == toset(["2575", "443"])
    error_message = "The rule must open exactly the MLLP port (2575) and OpenEMR HTTPS (443)."
  }

  assert {
    condition     = one(google_compute_firewall.allow["allow-serverless-to-edge"].allow).protocol == "tcp"
    error_message = "MLLP and HTTPS are TCP."
  }

  assert {
    condition     = google_compute_firewall.allow["allow-serverless-to-edge"].target_tags == toset(["pulsebridge-edge-vm"]) && output.edge_vm_network_tag == "pulsebridge-edge-vm"
    error_message = "Without a service account, the rule must target the edge VM network tag the module outputs."
  }
}

run "firewall_targets_service_account_when_given" {
  command = plan

  variables {
    edge_vm_service_account_email = "edge-vm@example-project-123.iam.gserviceaccount.com"
    enable_iap_ssh                = true
  }

  assert {
    condition = alltrue([for r in google_compute_firewall.allow :
      r.target_service_accounts == toset(["edge-vm@example-project-123.iam.gserviceaccount.com"]) && r.target_tags == null
    ])
    error_message = "With a service account email, every allow rule must target that service account and no tag."
  }

  assert {
    condition     = output.firewall_target.type == "service_account"
    error_message = "firewall_target output must report the service account target."
  }
}

run "iap_ssh_is_limited_to_iap_range" {
  command = plan

  variables {
    enable_iap_ssh = true
  }

  assert {
    condition     = google_compute_firewall.allow["allow-iap-ssh-to-edge"].source_ranges == toset(["35.235.240.0/20"]) && toset(one(google_compute_firewall.allow["allow-iap-ssh-to-edge"].allow).ports) == toset(["22"])
    error_message = "SSH must be open only to Identity-Aware Proxy's TCP forwarding range."
  }

  # The check that matters most, repeated with every optional rule switched on.
  assert {
    condition     = alltrue([for r in google_compute_firewall.allow : !contains(r.source_ranges, "0.0.0.0/0") && !contains(r.source_ranges, "::/0")])
    error_message = "No allow rule may accept traffic from anywhere, even with optional rules on."
  }
}

# --- Artifact Registry ---------------------------------------------------------------------------

run "artifact_registry_repositories" {
  command = plan

  assert {
    condition     = google_artifact_registry_repository.services.format == "DOCKER" && google_artifact_registry_repository.services.mode == "STANDARD_REPOSITORY"
    error_message = "The services repository must be a standard Docker repository."
  }

  assert {
    condition     = output.docker_repository_url == "us-central1-docker.pkg.dev/example-project-123/pulsebridge-services"
    error_message = "docker_repository_url must be <region>-docker.pkg.dev/<project>/<repository>."
  }

  assert {
    condition     = length(google_artifact_registry_repository.dockerhub) == 1 && google_artifact_registry_repository.dockerhub[0].mode == "REMOTE_REPOSITORY" && one(one(google_artifact_registry_repository.dockerhub[0].remote_repository_config).docker_repository).public_repository == "DOCKER_HUB"
    error_message = "By default a Docker Hub remote repository must exist so the VM can pull without NAT."
  }
}

run "dockerhub_mirror_can_be_turned_off" {
  command = plan

  variables {
    create_dockerhub_mirror = false
  }

  assert {
    condition     = length(google_artifact_registry_repository.dockerhub) == 0 && output.dockerhub_mirror_url == null
    error_message = "No Docker Hub mirror may exist when create_dockerhub_mirror is false."
  }
}
