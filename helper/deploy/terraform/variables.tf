# ----------------------------------------------------------------------------- injected by Resource Manager
variable "tenancy_ocid" {
  type = string
}

variable "region" {
  type = string
}

variable "compartment_ocid" {
  description = "Compartment for the OCI Migration Tool VM, the seed image bucket and (by default) the seed images"
  type        = string
}

# ----------------------------------------------------------------------------- placement
variable "availability_domain" {
  description = "AD of the OCI Migration Tool VM. Target instances can only be created in this AD (boot volumes are AD-local)."
  type        = string
}

variable "network_compartment_ocid" {
  description = "Compartment of the VCN and subnet (and where the network security group is created); empty = same as compartment_ocid"
  type        = string
  default     = ""
}

variable "vcn_ocid" {
  description = "VCN containing the migration tool VM subnet"
  type        = string
}

variable "subnet_ocid" {
  description = "Subnet for the OCI Migration Tool VM; must route to vCenter (VPN/FastConnect) and be reachable from the administrators' browsers"
  type        = string
}

variable "assign_public_ip" {
  description = "Give the migration tool VM a public IP (only when administrators reach the web UI over the internet)"
  type        = bool
  default     = false
}

variable "allowed_source_cidrs" {
  description = "CIDRs allowed to reach the web UI (TCP 8443) and SSH (TCP 22): the administrators' networks"
  type        = list(string)
}

# The vCenter/ESXi server and whether to verify its TLS certificate are entered on the login page of the
# web UI (per login), so the stack has no vCenter settings.

# ----------------------------------------------------------------------------- instance
variable "helper_display_name" {
  type    = string
  default = "oci-migration-tool"
}

variable "helper_shape" {
  type    = string
  default = "VM.Standard.E5.Flex"
}

variable "helper_ocpus" {
  type    = number
  default = 2
}

variable "helper_memory_gb" {
  type    = number
  default = 16
}

variable "ssh_public_key" {
  description = "SSH public key for the opc user"
  type        = string
}

# ----------------------------------------------------------------------------- migration tool service
variable "seed_bucket_name" {
  description = "Object Storage bucket used while importing seed custom images"
  type        = string
  default     = "oci-umt-seed-images"
}

variable "default_target_shape" {
  description = "Flex shape used for migrated instances"
  type        = string
  default     = "VM.Standard.E5.Flex"
}

variable "max_concurrent_jobs" {
  description = "Number of migrations the migration tool runs in parallel (each needs its volumes attached; 32 attachment slots in total)"
  type        = number
  default     = 2
}

variable "source_git_url" {
  description = "Git repository containing the migration tool; cloned on the VM and installed with pip"
  type        = string
  default     = "https://github.com/RichardORCL/OCI-UltimateMigrationTool.git"
}

variable "source_git_ref" {
  description = "Branch, tag or commit to install"
  type        = string
  default     = "main"
}

# ----------------------------------------------------------------------------- IAM
variable "create_iam" {
  description = "Create the dynamic group, policy and tag namespace (requires tenancy administrator rights). Disable if an administrator created them separately."
  type        = bool
  default     = true
}

variable "policy_scope_compartment_ocid" {
  description = "Compartment in which the migration tool may create target instances and volumes (empty = whole tenancy)"
  type        = string
  default     = ""
}
