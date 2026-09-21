# Installing the OCI Migration Tool VM

The OCI Ultimate Migration Tool runs on one VM in your tenancy, the *OCI Migration Tool VM*.

## Prerequisites

- An OCI tenancy with a VCN/subnet that administrators' browsers can reach on 8443 (through a VPN, or
  via a public IP on the Migration Tool VM).
- Permissions to create instances, block volumes, custom images, an Object Storage bucket, a dynamic
  group, a policy and a tag namespace (or an administrator who creates the IAM parts for you, see
  `create_iam`).
- The Migration Tool VM must be able to reach GitHub to clone the tool's repository
  [RichardORCL/OCI-UltimateMigrationTool](https://github.com/RichardORCL/OCI-UltimateMigrationTool)
  (and PyPI to install its dependencies). Point `source_git_url` at your own fork if you maintain one.
- The Migration Tool VM is designed for a **private subnet without a public IP**. Its subnet needs a
  route to the Oracle Services Network through a Service Gateway (*All Services in Oracle
  Services Network*: OCI APIs, Object Storage, the instance console connection service used by
  *Remote console*, Oracle yum) and a NAT gateway for GitHub/PyPI and for cloud sources (Azure, AWS,
  Google Cloud). See the networking table in [how-it-works.md](how-it-works.md#networking).
- **Source reachability** depends on what you migrate:
  - **VMware**: the subnet must route to vCenter or the standalone ESXi host (VPN or FastConnect;
    HTTPS on 443). Each operator needs an account with read access to the inventory and
    `VirtualMachine.Provisioning.ExportOVF` (*Allow disk access*) on the VMs.
  - **Azure**: internet reach to `login.microsoftonline.com`, `management.azure.com` and the blob
    endpoints. Each operator needs an Entra ID app registration with a client secret (service
    principal) that has *Reader* on the subscriptions and the disk-export actions listed in
    [how-it-works.md](how-it-works.md#microsoft-azure).
  - **AWS**: internet reach to `sts`, `ec2` and `ebs` in the chosen region. Each operator needs an IAM
    access key with the actions listed in [how-it-works.md](how-it-works.md#amazon-ec2).
  - **Google Cloud**: internet reach to Google APIs and Cloud Storage. Each operator needs a service
    account JSON key plus a GCS export bucket, with the roles listed in
    [how-it-works.md](how-it-works.md#google-cloud-compute-engine).
  - **OVA/ISO**: Object Storage in the same tenancy (the stack policy already covers the seed bucket
    and ISO picker).

Nothing about the source platforms is configured in the stack; credentials are entered on the start
page and kept in memory for the session.

The Terraform in `helper/deploy/terraform` is a self-contained
[Resource Manager](https://docs.oracle.com/en-us/iaas/Content/ResourceManager/home.htm) stack (it ships
a `schema.yaml` for the console form) and also works with a local `terraform apply`.

## 1. Option A - deploy with Resource Manager (recommended)

1. Get the stack zip. Either click the **Deploy to Oracle Cloud** button in the
   [README](https://github.com/RichardORCL/OCI-UltimateMigrationTool#quick-start) (opens *Create stack*
   with the zip preloaded, skip step 2), download the committed stack zip from that repository, or
   rebuild it locally with `helper/deploy/package_stack.sh` (`package_stack.ps1` on Windows), which
   writes it to the repository root.
   Pass `--create <compartment-ocid>` (`-CreateInCompartment`) to create the stack straight from the
   OCI CLI instead of uploading it.
2. In the console: **Developer Services > Resource Manager > Stacks > Create stack > My configuration >
   .zip file**, upload the zip.
3. Fill in the form:
   - *Placement*: compartment for the VM, seed bucket and seed images, and the availability domain
     (target instances land in the same AD).
   - *Network*: the network compartment (defaults to the placement compartment; pick the compartment
     that holds your VCN when networking is managed separately), VCN, subnet, whether to assign a
     public IP, and the CIDRs of the administrators' networks allowed to reach the web UI. The
     network security group is created in the network compartment.
   - *OCI Migration Tool VM*: instance name, shape/OCPUs/memory and your SSH public key.
   - *Migration tool service*: git URL + ref to install, seed bucket, default target shape, number of
     parallel migrations.
   - *IAM*: keep **Create IAM resources** on unless an administrator already created the dynamic
     group/policy/tag namespace; optionally limit the compartment where the Migration Tool may
     create target instances.
4. Run **Plan**, then **Apply**. Outputs show the UI URL, the availability domain and next steps.

To upgrade the Migration Tool use the **Setup** page in the web UI (see below). Changing the git ref
in the stack alone does not upgrade an existing VM (cloud-init changes are ignored).

## 1. Option B - local Terraform

```bash
cd helper/deploy/terraform
cp terraform.tfvars.example terraform.tfvars   # edit values, incl. tenancy_ocid/region/compartment_ocid/subnet_ocid
terraform init && terraform apply
terraform output helper_ui_url    # prints the UI URL
```

## 2. What the stack creates

- an Oracle Linux 9 flex instance with paravirtualized storage/network and consistent device naming
  (`/dev/oracleoci/oraclevd*`), tagged so the stack policy can find it;
- a network security group (in the network compartment, `network_compartment_ocid`) allowing TCP 8443
  (web UI) and 22 (SSH) from `allowed_source_cidrs`, all egress;
- the `oci-umt-seed-images` Object Storage bucket used while importing seed images;
- (when `create_iam = true`) the `oci-umt` tag namespace, a dynamic group matching the tagged instance
  in the Migration Tool compartment, and a policy granting it `manage instance-family` /
  `manage volume-family` / `use virtual-network-family` in `policy_scope_compartment_ocid` (default:
  tenancy), plus `manage instance-images` / `compute-image-capability-schema` / volume attachments in
  its own compartment, object access to the seed bucket and `PAR_MANAGE` on that bucket (the image
  import service reads the placeholder through a pre-authenticated request it creates on the
  Migration Tool's behalf); for *Create OCI instance based on ISO* additionally `read buckets` /
  `read objects` in the policy scope (the ISO picker) and
  `manage buckets ... where request.permission = 'PAR_MANAGE'` there (the ISO import reads the object
  through a PAR as well);
- cloud-init that writes the Migration Tool environment file (OCI settings, bucket, default shape,
  concurrency), installs the Migration Tool (git clone + `pip install`), generates a self-signed
  certificate, opens 8443 in firewalld and starts the Migration Tool service.

**Target instances can only be created in the Migration Tool's AD** because boot volumes are AD-local;
deploy one stack per AD if you need more.

When `create_iam = false`, an administrator must create beforehand: tag namespace `oci-umt` with key
`role`; a dynamic group matching the Migration Tool instance in its compartment; and the policy
statements listed in `main.tf`.

## 3. Verify

```bash
curl -k https://<tool-vm-ip>:8443/api/health          # {"status":"ok", ...}
```

Then open `https://<tool-vm-ip>:8443/` in a browser and accept the self-signed certificate. The first
visit asks whether to protect the web UI with a password (you can also set, change or remove it later
on **Setup**). If a password is set, unlock with it before the start page.

The start page lists every option (VMware, Azure, AWS, Google Cloud, OVA import, ISO, Remote Console,
export to OVF). Pick one and follow its login or form. Source credentials are entered there, not in
the stack; one Migration Tool VM can serve several sources and several accounts. The *(i)* buttons
next to the cloud cards show how to prepare the service principal, IAM user or service account.

From the Migration Tool VM you can confirm outbound reach to a source, for example:

```bash
# VMware
ssh opc@<tool-vm-ip> curl -k -o /dev/null -w '%{http_code}\n' https://<vcenter-or-esxi>/sdk
# Azure
curl -sI https://login.microsoftonline.com | head -1
curl -sI https://management.azure.com | head -1
# AWS (replace the region)
curl -sI https://ec2.<region>.amazonaws.com | head -1
# Google Cloud
curl -sI https://compute.googleapis.com | head -1
```

To replace the self-signed certificate, put your own TLS certificate and key in the Migration Tool
config directory and restart the service.

## Configuration reference

Settings use the `HELPER_` environment prefix. They are written by the stack; the *Setup* page can
override logging, concurrency and the session timeout at runtime.

| Variable | Default | Description |
| --- | --- | --- |
| `HELPER_VCENTER_HOST` / `HELPER_VCENTER_PORT` | – / 443 | Optional: vCenter/ESXi host pre-filled on the VMware login page (not set by the stack) |
| `HELPER_VCENTER_VERIFY_SSL` | `false` | Optional: default state of *Verify the server certificate* on the VMware login page |
| `HELPER_NFC_HOST_OVERRIDE` | session's vCenter | VMware: host substituted for `*` in lease URLs. The per-VM *Download the disks directly from the ESXi host* option takes precedence |
| `HELPER_NFC_CHUNK_BYTES` | `1048576` | VMware: download chunk size |
| `HELPER_NFC_PIPELINE_DEPTH` | `8` | VMware: chunks buffered when a job uses *Decode and write on a separate thread* |
| `HELPER_LEASE_PROGRESS_INTERVAL_S` / `HELPER_LEASE_READY_TIMEOUT_S` | 60 / 300 | VMware: lease keep-alive interval / time to wait for the lease |
| `HELPER_DISK_RETRY_ATTEMPTS` | `3` | Attempts per disk (each restarts from the beginning) |
| `HELPER_GUEST_SHUTDOWN_TIMEOUT_S` | `300` | VMware: how long to wait for the guest OS shutdown before a hard power-off |
| `HELPER_AZURE_SAS_DURATION_S` | `86400` | Azure: validity requested for the export SAS; renewed when a copy outlives it |
| `HELPER_AZURE_RANGE_WORKERS` | `4` | Azure: parallel range downloads per disk |
| `HELPER_AZURE_RANGE_CHUNK_BYTES` | `8388608` | Azure: size of one range request |
| `HELPER_AZURE_DEALLOCATE_TIMEOUT_S` | `900` | Azure deallocate mode: how long to wait for the VM to deallocate |
| `HELPER_AZURE_SNAPSHOT_TIMEOUT_S` | `900` | Azure snapshot mode: how long to wait for each snapshot |
| `HELPER_SESSION_TTL_S` | `28800` | Idle timeout of web sessions (5 min - 7 days); changeable on *Setup* |
| `HELPER_COOKIE_SECURE` | `true` | Set `false` only for plain-HTTP development |
| `HELPER_MAX_CONCURRENT_JOBS` | `2` | Migrations copying disks at the same time (1-16, further jobs queue); changeable on *Setup* |
| `HELPER_OCI_AUTH` | `instance_principal` | `config_file` for local development |
| `HELPER_INSTANCE_ID`, `HELPER_COMPARTMENT_ID`, `HELPER_AVAILABILITY_DOMAIN`, `HELPER_REGION`, `HELPER_TENANCY_ID` | auto | Discovered from the instance metadata service when empty |
| `HELPER_SEED_BUCKET` | `oci-umt-seed-images` | Bucket for seed image imports |
| `HELPER_SEED_COMPARTMENT_ID` | Migration Tool compartment | Where seed images are kept |
| `HELPER_ISO_IMAGE_COMPARTMENT_ID` | seed compartment | Where custom images imported from installer ISOs are kept |
| `HELPER_ISO_SOURCE_IMAGE_TYPE` | `VMDK` | `sourceImageType` sent to `CreateImage` for an ISO |
| `HELPER_DEFAULT_SHAPE` | `VM.Standard.E5.Flex` | Flex shape for target instances |
| `HELPER_MIN_VOLUME_GB` | `50` | Minimum OCI volume size |
| `HELPER_DEVICE_PREFIX` | `/dev/oracleoci/oraclevd` | Consistent device path prefix |
| `HELPER_LAUNCH_TIMEOUT_S` / `HELPER_VOLUME_TIMEOUT_S` / `HELPER_IMAGE_IMPORT_TIMEOUT_S` | 1800 / 900 / 3600 | Waiter timeouts |
| `HELPER_SKIP_ZERO_GRAINS` | `true` | Do not write all-zero grains (fresh volumes read as zero) |
| `HELPER_CONSOLE_IDLE_TIMEOUT_S` / `HELPER_CONSOLE_CONNECT_TIMEOUT_S` | 600 / 120 | *Remote console*: idle delete and connect timeouts |
| `HELPER_DB_PATH` | on the Migration Tool VM | Job database |
| `HELPER_TLS_CERT_FILE` / `HELPER_TLS_KEY_FILE` | – | TLS material for 8443 |
| `HELPER_LOG_LEVEL` | `INFO` | Migration Tool log level (service journal); changeable on *Setup* |
| `HELPER_OCI_LOG_REQUESTS` | `false` | Dump every OCI SDK request/response including bodies; changeable on *Setup* |
| `HELPER_RUNTIME_SETTINGS_PATH` | on the Migration Tool VM | Where *Setup* page changes are persisted |
| `HELPER_UI_PASSWORD_HASH_PATH` | on the Migration Tool VM | Optional web UI password (scrypt hash). Missing/empty = no lock. Offered on first use and changeable on *Setup* |
| `HELPER_UPDATE_SOURCE_DIR` / `HELPER_UPDATE_VENV_DIR` | on the Migration Tool VM | Git checkout and virtualenv used by the self-update |
| `HELPER_UPDATE_SERVICE` / `HELPER_UPDATE_LOG_PATH` | the Migration Tool service / the update log | service restarted after an update; update log shown in the UI |

## Updating the Migration Tool

The **Setup** tab compares the installed commit with the head of the branch the Migration Tool was
installed from (`source_git_ref`) and offers **Update now**. The update fetches the new version,
installs it and restarts the Migration Tool service. All web sessions end with the restart; the page
waits for the new version and returns to the start page (or the UI password prompt if one is set).
The update is refused while migrations are running (the restart would abort them) unless you confirm
**Update anyway** on Setup. After a forced restart, cancel each failed job so its OCI volumes are
put back.

## Maintenance

- Seed images accumulate one per firmware/OS combination. Delete them from the *Setup* tab, with
  `DELETE /api/seed-images` (logged in) or from the console (tag `oci-umt-seed=true`).
- ISO images accumulate one per ISO object / firmware / device model and are kept for reuse. Delete
  them from the *Setup* tab, with `DELETE /api/iso-images` or from the console (tag `oci-umt-iso=true`);
  instances already launched keep running.
- Jobs are stored in `HELPER_DB_PATH`. A failed job leaves its OCI resources in place for inspection;
  *Clean up OCI resources* in the job view (`POST /api/jobs/{id}/cancel`) terminates the instance and
  deletes the volumes.
- After a restart of the service, jobs that were running are marked `FAILED` (their source session is
  gone); clean them up and start again. For Azure / AWS / Google Cloud jobs, cancel them from a
  browser that is logged in to that source so export access, snapshots and staging objects are
  released; otherwise the job message lists what to clean up by hand.
- The job history can be trimmed from the *Setup* tab: *Delete failed jobs* removes `FAILED` and
  `CANCELLED` records, *Delete all jobs* every finished record. Running or queued jobs are never
  deleted, and only the records go - OCI resources of a failed job are not cleaned up by this.
- The Migration Tool supports up to 32 attached volumes at once, which bounds
  `HELPER_MAX_CONCURRENT_JOBS`.
- **Web UI password:** the first visit offers to set one; change or remove it on *Setup*. If you
  forget it, SSH to the VM, remove the UI password hash file and restart the Migration Tool service.
