# Behaviour tests for project-base. They run with `terraform test` from the module directory.
#
# mock_provider replaces the real google provider: no credentials are read and no API is called.
# Every run uses `command = plan`, so nothing is created even in the mock.
#
# In a plan, attributes the provider computes (id, self_link) are unknown, so a plain plan test
# cannot check that a subnet or firewall rule is attached to *this* network. The override_resource
# blocks below give the network and subnets fixed, known ids at plan time (override_during = plan,
# Terraform 1.11+). Every resource that references them then carries a known value the tests can
# compare, and a resource wired to another network (such as "default") shows up as a mismatch.

mock_provider "google" {}

override_resource {
  target          = google_compute_network.this
  override_during = plan
  values = {
    id        = "projects/example-project-123/global/networks/pulsebridge-vpc"
    self_link = "https://www.googleapis.com/compute/v1/projects/example-project-123/global/networks/pulsebridge-vpc"
  }
}

override_resource {
  target          = google_compute_subnetwork.edge
  override_during = plan
  values = {
    id        = "projects/example-project-123/regions/us-central1/subnetworks/pulsebridge-edge"
    self_link = "https://www.googleapis.com/compute/v1/projects/example-project-123/regions/us-central1/subnetworks/pulsebridge-edge"
  }
}

override_resource {
  target          = google_compute_subnetwork.serverless
  override_during = plan
  values = {
    id        = "projects/example-project-123/regions/us-central1/subnetworks/pulsebridge-serverless"
    self_link = "https://www.googleapis.com/compute/v1/projects/example-project-123/regions/us-central1/subnetworks/pulsebridge-serverless"
  }
}

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
    condition     = !contains(keys(google_project_service.this), "iap.googleapis.com")
    error_message = "The IAP API must not be enabled while enable_iap_ssh is false."
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

run "iap_api_enabled_only_with_iap_ssh" {
  command = plan

  variables {
    enabled_apis   = ["run.googleapis.com", "compute.googleapis.com"]
    enable_iap_ssh = true
  }

  assert {
    condition     = toset(keys(google_project_service.this)) == toset(["run.googleapis.com", "compute.googleapis.com", "iap.googleapis.com"])
    error_message = "With enable_iap_ssh, exactly the listed APIs plus iap.googleapis.com must be enabled."
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
    condition     = google_compute_router.nat[0].network == "projects/example-project-123/global/networks/pulsebridge-vpc"
    error_message = "The Cloud Router must be attached to this module's VPC."
  }

  assert {
    condition     = google_compute_router_nat.this[0].router == google_compute_router.nat[0].name
    error_message = "Cloud NAT must be configured on this module's Cloud Router."
  }

  assert {
    condition     = google_compute_router_nat.this[0].source_subnetwork_ip_ranges_to_nat == "LIST_OF_SUBNETWORKS"
    error_message = "NAT must translate only the listed subnet, not every subnet."
  }

  assert {
    condition     = length(google_compute_router_nat.this[0].subnetwork) == 1 && one(google_compute_router_nat.this[0].subnetwork).name == "https://www.googleapis.com/compute/v1/projects/example-project-123/regions/us-central1/subnetworks/pulsebridge-edge"
    error_message = "The only subnet NAT translates must be the edge subnet."
  }
}

# --- Network -------------------------------------------------------------------------------------

run "custom_vpc_and_subnet_attachment" {
  command = plan

  assert {
    condition     = google_compute_network.this.auto_create_subnetworks == false
    error_message = "The VPC must be custom mode (no auto-created subnets)."
  }

  # Both subnets must sit in this module's VPC, not in "default" or any other network.
  assert {
    condition = alltrue([
      google_compute_subnetwork.edge.network == "projects/example-project-123/global/networks/pulsebridge-vpc",
      google_compute_subnetwork.serverless.network == "projects/example-project-123/global/networks/pulsebridge-vpc",
    ])
    error_message = "Both subnets must be attached to this module's VPC."
  }

  assert {
    condition     = google_compute_subnetwork.edge.private_ip_google_access == true
    error_message = "The edge subnet needs Private Google Access so the VM can reach Google APIs and Artifact Registry without NAT."
  }

  assert {
    condition     = google_compute_subnetwork.serverless.private_ip_google_access == true
    error_message = "The serverless subnet needs Private Google Access."
  }

  assert {
    condition     = google_compute_subnetwork.edge.ip_cidr_range == "10.10.0.0/24" && google_compute_subnetwork.serverless.ip_cidr_range == "10.10.1.0/24"
    error_message = "Subnets must use the configured CIDR ranges."
  }

  assert {
    condition     = google_compute_subnetwork.edge.region == "us-central1" && google_compute_subnetwork.serverless.region == "us-central1"
    error_message = "Subnets must be in the configured region."
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

  # source_ranges only filter INGRESS rules; on an EGRESS rule they are not the source of
  # traffic at all, so a rule that silently became EGRESS would no longer mean what it says.
  assert {
    condition     = alltrue([for r in google_compute_firewall.allow : r.direction == "INGRESS"]) && google_compute_firewall.deny_all_ingress.direction == "INGRESS"
    error_message = "Every firewall rule in this module must be an INGRESS rule."
  }

  # Every rule must protect this module's VPC; a rule on another network protects nothing here.
  assert {
    condition = alltrue(concat(
      [for r in google_compute_firewall.allow : r.network == "projects/example-project-123/global/networks/pulsebridge-vpc"],
      [google_compute_firewall.deny_all_ingress.network == "projects/example-project-123/global/networks/pulsebridge-vpc"],
    ))
    error_message = "Every firewall rule must be attached to this module's VPC."
  }

  assert {
    condition     = google_compute_firewall.deny_all_ingress.priority == 65534 && one(google_compute_firewall.deny_all_ingress.deny).protocol == "all" && length(google_compute_firewall.deny_all_ingress.allow) == 0
    error_message = "An explicit lowest-priority deny-all ingress rule must exist and allow nothing."
  }

  assert {
    condition     = google_compute_firewall.deny_all_ingress.source_ranges == toset(["0.0.0.0/0"])
    error_message = "The deny-all rule must cover every IPv4 source."
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

  # The checks that matter most, repeated with every optional rule switched on.
  assert {
    condition     = alltrue([for r in google_compute_firewall.allow : !contains(r.source_ranges, "0.0.0.0/0") && !contains(r.source_ranges, "::/0")])
    error_message = "No allow rule may accept traffic from anywhere, even with optional rules on."
  }

  assert {
    condition     = alltrue([for r in google_compute_firewall.allow : r.direction == "INGRESS" && r.network == "projects/example-project-123/global/networks/pulsebridge-vpc"])
    error_message = "Every allow rule, including IAP SSH, must be INGRESS on this module's VPC."
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
