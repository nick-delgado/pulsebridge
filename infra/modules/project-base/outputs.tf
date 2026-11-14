# Outputs are the module's contract with its consumers: edge-vm (T21) and phr (T22) read the
# network, subnets, tag and ports from here; the CI and build pipeline read the repository URLs.

output "enabled_apis" {
  description = "Service names of the APIs this module enabled."
  value       = sort([for s in google_project_service.this : s.service])
}

output "network_id" {
  description = "ID of the VPC network (projects/<project>/global/networks/<name>)."
  value       = google_compute_network.this.id
}

output "network_name" {
  description = "Name of the VPC network. Cloud Run's Direct VPC egress (`vpc_access.network_interfaces.network`) takes this."
  value       = google_compute_network.this.name
}

output "network_self_link" {
  description = "Self link of the VPC network, for resources that require a URL."
  value       = google_compute_network.this.self_link
}

output "edge_subnet_id" {
  description = "ID of the edge subnet, for the edge VM's network interface (T21)."
  value       = google_compute_subnetwork.edge.id
}

output "edge_subnet_name" {
  description = "Name of the edge subnet."
  value       = google_compute_subnetwork.edge.name
}

output "edge_subnet_cidr" {
  description = "IPv4 range of the edge subnet."
  value       = google_compute_subnetwork.edge.ip_cidr_range
}

output "serverless_subnet_id" {
  description = "ID of the subnet Cloud Run services join with Direct VPC egress (T22: cuff simulator and fhir-proxy)."
  value       = google_compute_subnetwork.serverless.id
}

output "serverless_subnet_name" {
  description = "Name of the serverless subnet. Cloud Run's `vpc_access.network_interfaces.subnetwork` takes this; set `vpc_access.egress = \"PRIVATE_RANGES_ONLY\"`."
  value       = google_compute_subnetwork.serverless.name
}

output "serverless_subnet_cidr" {
  description = "IPv4 range of the serverless subnet: the only source allowed to reach the edge VM's MLLP and HTTPS ports."
  value       = google_compute_subnetwork.serverless.ip_cidr_range
}

output "edge_vm_network_tag" {
  description = "Network tag the edge VM must carry for the firewall rules to apply (when edge_vm_service_account_email is null)."
  value       = local.edge_vm_network_tag
}

output "firewall_target" {
  description = "What the allow rules target: {type = \"network_tag\" or \"service_account\", value = tag or email}."
  value = var.edge_vm_service_account_email == null ? {
    type  = "network_tag"
    value = local.edge_vm_network_tag
    } : {
    type  = "service_account"
    value = var.edge_vm_service_account_email
  }
}

output "mllp_port" {
  description = "TCP port opened from the serverless subnet to the edge VM for MLLP."
  value       = var.mllp_port
}

output "openemr_https_port" {
  description = "TCP port opened from the serverless subnet to the edge VM for OpenEMR HTTPS."
  value       = var.openemr_https_port
}

output "firewall_rule_names" {
  description = "Names of the firewall rules this module created (allow rules and the explicit deny-all)."
  value       = concat(sort([for r in google_compute_firewall.allow : r.name]), [google_compute_firewall.deny_all_ingress.name])
}

output "cloud_nat_enabled" {
  description = "Whether Cloud NAT exists. When false, the edge VM has no internet access unless it gets an external IP."
  value       = var.enable_cloud_nat
}

output "docker_repository_url" {
  description = "Registry path for service images: <region>-docker.pkg.dev/<project>/<repository>."
  value       = "${google_artifact_registry_repository.services.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.services.repository_id}"
}

output "dockerhub_mirror_url" {
  description = "Registry path of the Docker Hub pull-through cache, or null when it is not created. Official images live under `library/`, e.g. <url>/library/mariadb@sha256:..."
  value       = var.create_dockerhub_mirror ? "${google_artifact_registry_repository.dockerhub[0].location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.dockerhub[0].repository_id}" : null
}
