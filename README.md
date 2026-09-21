# OCI Ultimate Migration Tool

The **OCI Ultimate Migration Tool** brings virtual machines into Oracle Cloud Infrastructure. It runs as a
single VM in your OCI tenancy with a web UI: log in to the source platform, pick the VMs, choose where they
should land in OCI, and follow the progress. Every migration ends in a ready-to-run OCI compute instance with
the original disks, firmware settings and sizing.

**Migrate from**

- **VMware** - vCenter or a standalone ESXi host
- **Microsoft Azure**
- **Amazon Web Services** - EC2 instances
- **Google Cloud** - Compute Engine VMs
- **Manual import** - an OVA/OVF (or VMDK) uploaded to OCI Object Storage

**Extra features**

- **Create OCI instance from ISO** - boot a new instance from an installer ISO and install the OS through a Remote console session
- **OCI Remote Console** - open the VNC console of any compute instance in your browser
- **Export from OCI to OVA/OVF** - turn an OCI instance into an OVA/OVF package in Object Storage

## What you can use it for

- **Lift-and-shift** of Linux and Windows VMs, single or multi-disk, BIOS or UEFI, with Secure Boot where the
  source used it.
- **Several sources with one tool VM**: the vCenter/ESXi address, Azure service principal, AWS access key or
  Google service account are entered at login; nothing about the sources is configured in the deployment.
- **Short downtime**: the source VM is only stopped right before its disks are copied, after you confirm.
  Azure, AWS and Google VMs can also be copied from snapshots while they keep running.
- **Batch migrations**: run several migrations in parallel, queue more, and see progress, throughput and
  diagnostics per job.
- **First-boot troubleshooting**: open the remote console of a migrated instance directly from its job.
- **Bare metal and Arm**: the ISO installer supports x86 and Ampere A1/A2/A4 shapes, virtual machine or bare metal.

Not in scope: live migration with delta sync, VMware Workstation/Fusion, Hyper-V or KVM sources, and
guest-side reconfiguration (IP addresses, drivers). See [docs/limitations.md](docs/limitations.md).

## Quick start

[![Deploy to Oracle Cloud](https://oci-resourcemanager-plugin.plugins.oci.oraclecloud.com/latest/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https://github.com/RichardORCL/OCI-UltimateMigrationTool/raw/main/oci-ultimate-migration-tool-stack.zip)

1. **Deploy** the tool VM with the button above. Resource Manager opens *Create stack* with the stack
   preloaded; choose the compartment, the subnet the VM should live in and the IP ranges that may use the
   web UI, then apply. Manual Terraform deployment and all settings are described in
   [docs/install-helper.md](docs/install-helper.md).
2. **Open the web UI** at `https://<tool-vm-ip>:8443/` and accept the self-signed certificate. On the first
   visit you can protect the UI with a password (optional; change it later under *Setup*).
3. **Pick what you want to do** on the start page: a source platform to migrate from, or one of the extra
   features. Each card explains what it needs, and the *(i)* buttons show how to prepare the source
   credentials.

## Networking

The tool VM is meant to run in a private subnet without a public IP. Its subnet needs a route to OCI
services (Service Gateway and/or NAT gateway), and, for VMware sources, to vCenter/ESXi over your
VPN/FastConnect; cloud sources are reached over the internet. Administrators reach the web UI on
port 8443. The full list of flows and ports is in
[docs/how-it-works.md](docs/how-it-works.md#networking).

## Documentation

- [docs/install-helper.md](docs/install-helper.md) - deployment, IAM, configuration reference
- [docs/how-it-works.md](docs/how-it-works.md) - migration mechanism, supported sources, network flows
- [docs/architecture.md](docs/architecture.md) - internals of the migration pipeline
- [docs/os-mapping.md](docs/os-mapping.md) - guest OS, launch option and shape mapping tables
- [docs/limitations.md](docs/limitations.md) - known limitations and troubleshooting

## License

[UPL 1.0](LICENSE). The browser-side VNC client is [noVNC](https://github.com/novnc/noVNC) (MPL-2.0) with
pako (MIT), redistributed unmodified under `helper/helper_app/ui/vendor/novnc`; see
[THIRD_PARTY_LICENSES.txt](THIRD_PARTY_LICENSES.txt).
