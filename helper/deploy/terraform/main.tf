terraform {
  required_version = ">= 1.3"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 5.0"
    }
    time = {
      source  = "hashicorp/time"
      version = ">= 0.9"
    }
  }
}

# In Resource Manager the provider is authenticated automatically; locally use ~/.oci/config.
provider "oci" {
  region       = var.region
  tenancy_ocid = var.tenancy_ocid
}

locals {
  network_compartment = var.network_compartment_ocid != "" ? var.network_compartment_ocid : var.compartment_ocid
  policy_scope  = var.policy_scope_compartment_ocid != "" ? "compartment id ${var.policy_scope_compartment_ocid}" : "tenancy"
  tag_namespace = "UltimateMigrationTool"
  tag_role_key  = "role"
  dynamic_group = "${var.helper_display_name}-dg"
}

# ---------------------------------------------------------------------------- image
data "oci_core_images" "ol9" {
  compartment_id           = var.compartment_ocid
  operating_system         = "Oracle Linux"
  operating_system_version = "9"
  shape                    = var.helper_shape
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

# ---------------------------------------------------------------------------- network security
# The NSG lives with the VCN in the network compartment (which may differ from the VM's compartment).
resource "oci_core_network_security_group" "helper" {
  compartment_id = local.network_compartment
  vcn_id         = var.vcn_ocid
  display_name   = "${var.helper_display_name}-nsg"
}

resource "oci_core_network_security_group_security_rule" "ui_ingress" {
  for_each                  = toset(var.allowed_source_cidrs)
  network_security_group_id = oci_core_network_security_group.helper.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = each.value
  source_type               = "CIDR_BLOCK"
  description               = "web UI / API from the administrators' networks"
  tcp_options {
    destination_port_range {
      min = 8443
      max = 8443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "ssh_ingress" {
  for_each                  = toset(var.allowed_source_cidrs)
  network_security_group_id = oci_core_network_security_group.helper.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = each.value
  source_type               = "CIDR_BLOCK"
  description               = "SSH administration"
  tcp_options {
    destination_port_range {
      min = 22
      max = 22
    }
  }
}

resource "oci_core_network_security_group_security_rule" "egress_all" {
  network_security_group_id = oci_core_network_security_group.helper.id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination               = "0.0.0.0/0"
  destination_type          = "CIDR_BLOCK"
  description               = "vCenter (SOAP + NFC on 443), Azure (Entra ID, ARM, blob endpoints on 443), OCI APIs, Object Storage, package repositories"
}

# ---------------------------------------------------------------------------- object storage
data "oci_objectstorage_namespace" "ns" {
  compartment_id = var.tenancy_ocid
}

resource "oci_objectstorage_bucket" "seed" {
  compartment_id = var.compartment_ocid
  namespace      = data.oci_objectstorage_namespace.ns.namespace
  name           = var.seed_bucket_name
  access_type    = "NoPublicAccess"
  freeform_tags  = { "oci-umt" = "seed-images" }
}

# ---------------------------------------------------------------------------- IAM (instance principal)
resource "oci_identity_tag_namespace" "migration_tool" {
  count          = var.create_iam ? 1 : 0
  compartment_id = var.compartment_ocid
  name           = local.tag_namespace
  description    = "Resources used by OCI Ultimate Migration Tool"
}

resource "oci_identity_tag" "role" {
  count            = var.create_iam ? 1 : 0
  tag_namespace_id = oci_identity_tag_namespace.migration_tool[0].id
  name             = local.tag_role_key
  description      = "Role of the resource in the OCI Ultimate Migration Tool workflow"
}

resource "oci_identity_dynamic_group" "helper" {
  count          = var.create_iam ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = local.dynamic_group
  description    = "OCI Migration Tool VM"
  matching_rule  = "ALL {instance.compartment.id = '${var.compartment_ocid}', tag.${local.tag_namespace}.${local.tag_role_key}.value = 'helper'}"
}

resource "oci_identity_policy" "helper" {
  count          = var.create_iam ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = "${var.helper_display_name}-policy"
  description    = "Permissions needed by the OCI Migration Tool VM"
  statements = [
    # target instances, volumes and attachments; instance-family also covers the instance console connections
    # and the instance listing of the "OCI Remote Console" page (Resource Search only returns resources the
    # migration tool VM may read anyway, so the policy scope bounds the search as well)
    "Allow dynamic-group ${local.dynamic_group} to manage instance-family in ${local.policy_scope}",
    "Allow dynamic-group ${local.dynamic_group} to manage volume-family in ${local.policy_scope}",
    "Allow dynamic-group ${local.dynamic_group} to use virtual-network-family in ${local.policy_scope}",
    "Allow dynamic-group ${local.dynamic_group} to read compartments in tenancy",
    "Allow dynamic-group ${local.dynamic_group} to inspect compartments in tenancy",
    # the migration tool VM's own attachments live in its compartment
    "Allow dynamic-group ${local.dynamic_group} to manage volume-attachments in compartment id ${var.compartment_ocid}",
    # seed custom images and their capability schemas (global schemas are readable by any authenticated principal)
    "Allow dynamic-group ${local.dynamic_group} to manage instance-images in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${local.dynamic_group} to manage compute-image-capability-schema in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${local.dynamic_group} to read work-requests in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${local.dynamic_group} to manage objects in compartment id ${var.compartment_ocid} where target.bucket.name = '${var.seed_bucket_name}'",
    "Allow dynamic-group ${local.dynamic_group} to read buckets in compartment id ${var.compartment_ocid}",
    # the image import service reads the placeholder through a pre-authenticated request it creates on the
    # caller's behalf; without PAR_MANAGE on the bucket the import fails silently and the image is deleted
    "Allow dynamic-group ${local.dynamic_group} to manage buckets in compartment id ${var.compartment_ocid} where all {target.bucket.name = '${var.seed_bucket_name}', request.permission = 'PAR_MANAGE'}",
    "Allow dynamic-group ${local.dynamic_group} to read objectstorage-namespaces in tenancy",
    # "Create OCI instance based on ISO": the ISO picker lists buckets and objects anywhere in the policy scope,
    # and the image import reads the ISO the same way it reads the seed placeholder (through a PAR the
    # import service creates as the migration tool VM), so PAR_MANAGE is needed on the ISO buckets as well
    "Allow dynamic-group ${local.dynamic_group} to read buckets in ${local.policy_scope}",
    "Allow dynamic-group ${local.dynamic_group} to read objects in ${local.policy_scope}",
    "Allow dynamic-group ${local.dynamic_group} to manage objects in ${local.policy_scope}",
    "Allow dynamic-group ${local.dynamic_group} to manage buckets in ${local.policy_scope} where request.permission = 'PAR_MANAGE'",
  ]
  depends_on = [oci_identity_dynamic_group.helper]
}

# IAM objects are created in the home region and replicated asynchronously. Launching the instance
# with the freshly created defined tag right away can fail with a tag namespace not found error,
# so give the replication time to reach the Compute service in the target region.
resource "time_sleep" "iam_propagation" {
  count           = var.create_iam ? 1 : 0
  create_duration = "120s"
  depends_on      = [oci_identity_tag.role, oci_identity_policy.helper]
}

# ---------------------------------------------------------------------------- migration tool VM
resource "oci_core_instance" "helper" {
  availability_domain = var.availability_domain
  compartment_id      = var.compartment_ocid
  display_name        = var.helper_display_name
  shape               = var.helper_shape

  shape_config {
    ocpus         = var.helper_ocpus
    memory_in_gbs = var.helper_memory_gb
  }

  source_details {
    source_type             = "image"
    source_id               = data.oci_core_images.ol9.images[0].id
    boot_volume_size_in_gbs = 50
  }

  create_vnic_details {
    subnet_id        = var.subnet_ocid
    assign_public_ip = var.assign_public_ip
    nsg_ids          = [oci_core_network_security_group.helper.id]
    hostname_label   = var.helper_display_name
  }

  launch_options {
    boot_volume_type                    = "PARAVIRTUALIZED"
    network_type                        = "PARAVIRTUALIZED"
    remote_data_volume_type             = "PARAVIRTUALIZED"
    is_consistent_volume_naming_enabled = true
  }

  # The tag selects the instance into the dynamic group. When create_iam is false the namespace
  # "UltimateMigrationTool" with key "role" must already exist in the tenancy.
  defined_tags = { "${local.tag_namespace}.${local.tag_role_key}" = "helper" }

  metadata = {
    ssh_authorized_keys = var.ssh_public_key
    user_data = base64encode(templatefile("${path.module}/cloud-init.yaml", {
      source_git_url      = var.source_git_url
      source_git_ref      = var.source_git_ref
      seed_bucket         = var.seed_bucket_name
      default_shape       = var.default_target_shape
      max_concurrent_jobs = var.max_concurrent_jobs
      region              = var.region
      tenancy_ocid        = var.tenancy_ocid
    }))
  }

  lifecycle {
    precondition {
      condition     = var.source_git_url != ""
      error_message = "source_git_url is required."
    }
    ignore_changes = [source_details[0].source_id, metadata]
  }

  depends_on = [oci_identity_policy.helper, oci_identity_tag.role, time_sleep.iam_propagation]
}
