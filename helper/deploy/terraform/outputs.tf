locals {
  helper_ip = var.assign_public_ip ? oci_core_instance.helper.public_ip : oci_core_instance.helper.private_ip
}

output "helper_instance_id" {
  value = oci_core_instance.helper.id
}

output "helper_private_ip" {
  value = oci_core_instance.helper.private_ip
}

output "helper_public_ip" {
  value = oci_core_instance.helper.public_ip
}

output "helper_ui_url" {
  description = "Web UI of the OCI Ultimate Migration Tool"
  value       = "https://${local.helper_ip}:8443/"
}

output "availability_domain" {
  description = "Target instances must be created in this AD"
  value       = var.availability_domain
}

output "seed_bucket" {
  value = oci_objectstorage_bucket.seed.name
}

output "next_steps" {
  value = <<-EOT
    1. Wait ~3 minutes for cloud-init, then: curl -k https://${local.helper_ip}:8443/api/health
    2. Make sure the migration tool VM can reach your vCenter Server or ESXi host: from the VM, curl -k https://<vcenter>/sdk
    3. Open https://${local.helper_ip}:8443/ in a browser, accept the self-signed certificate, enter the vCenter/ESXi
       address (tick "Verify the server certificate" only for a CA-signed certificate) and log in with an account
       that has read access to the inventory and "Allow disk access" / "Export" on the VMs to migrate.
    4. Pick a VM under "Source VMs" and start the migration.
  EOT
}
