# Mapping tables

Implemented in `helper/helper_app/oci/mapping.py`. Every source (VMware, OLVM, Azure, AWS, Google Cloud,
OVA) is reduced to the same `VmSpec` / OS metadata before a seed image is chosen. VMware supplies a
vSphere `guestId`; OLVM inventory maps the oVirt `os.type` (`rhel_9x64`, `ol_8x64`, `windows_2022`, …)
onto the same guest ids, and Azure, AWS and Google Cloud inventory code synthesizes an equivalent guest id
from the image/AMI/license so the table below still applies.

## Guest OS -> seed image metadata

| Source guest id (vSphere `guestId`, or the equivalent produced for Azure / AWS / GCP) | OCI `operatingSystem` | `operatingSystemVersion` |
| --- | --- | --- |
| `oracleLinuxN_64Guest` | Oracle Linux | N |
| `rhelN_64Guest` | Red Hat Enterprise Linux | N |
| `centosN_64Guest` | CentOS | N |
| `rockylinux*` / `almalinux*` | Rocky Linux / AlmaLinux | from `guestFullName` (e.g. "Rocky Linux 9"), else **user selects** (8 / 9 / 10) |
| `ubuntu64Guest` | Ubuntu | from `guestFullName` (e.g. "Ubuntu 24.04 LTS"), else **user selects** (18.04 / 20.04 / 22.04 / 24.04 / 26.04) |
| `debianN_64Guest` | Debian | N |
| `slesN_64Guest` | SUSE Linux Enterprise Server | N |
| any Windows guest whose display name says `Server 20xx [R2]` | Windows | Server 20xx [R2] Standard (the year the source reports) |
| `windows2022srvNext_64Guest` | Windows | Server 2025 Standard |
| `windows2025srv*` / `2022srv*` / `2019srv*` | Windows | Server 2025/2022/2019 Standard |
| `windows2019srvNext_64Guest` | Windows | Server 2022 Standard |
| `windows9Server64Guest` | Windows | Server 2016 Standard |
| `windows8Server64Guest` | Windows | Server 2012 R2 Standard |
| `windows9_64Guest` / `windows11_64Guest` (or "Microsoft Windows 10/11" in the display name) | Windows | `Windows10` / `Windows11`. `CreateImage` rejects these, so the seed is imported without OS metadata and then registered with `UpdateImage` (the same two-step procedure Oracle documents for Windows 10/11 imports). Client editions must be BYOL: OCI provides no licenses for them. |
| other `windows*` | Windows | **user selects** (defaults to Server 2019 Standard) |
| anything else | Custom Linux | first number in `guestFullName` (ignoring `(64-bit)`) |

### When the source does not name the release

Some guest ids carry no release (`ubuntu64Guest`, `rockylinux_64Guest`, `almalinux_64Guest`,
`fedora64Guest`, ...) and the source may show only e.g. "Ubuntu Linux (64-bit)" for a powered-off VM.
The inspection then returns `os.version_detected = false` together with `os.version_choices` (the
releases OCI has platform images / documented custom-image support for, see `OS_VERSION_CHOICES` in
`mapping.py`), and the export page shows a required *Guest OS version* dropdown. The choice is sent
as `target.operating_system_version`; creating the job refuses without it. For guests whose release
*was* detected the dropdown is shown pre-selected so a wrong detection can still be corrected. The
value only affects the OCI image metadata (`operatingSystem` / `operatingSystemVersion`) and the seed
image identity - it does not change how the disks are copied.

vSphere identifies a new Windows Server release as `<previous>srvNext` until the next major vSphere
release: `windows2019srvNext_64Guest` is Windows Server 2022 (vSphere 7.0 U2+) and
`windows2022srvNext_64Guest` is Windows Server 2025 (vSphere 8.0 U2+). Because that encoding is easy to
misread, the release year in `guestFullName` takes precedence over the `guestId` table.

Windows detection also looks at `guestFullName`, so `otherGuest` VMs running Windows are still
registered as Windows (and require a license type).

## Launch options

Firmware, Secure Boot and device model come from the source inventory (vSphere `config.firmware` /
`efiSecureBootEnabled`, OLVM `bios.type`, Azure Hyper-V generation / Trusted Launch, AWS boot mode,
GCP Shielded VM, or the OVA/ISO form). They map to the same OCI launch options:

| Source | OCI `LaunchOptions` |
| --- | --- |
| `config.firmware = bios` | `firmware = BIOS` |
| `config.firmware = efi` | `firmware = UEFI_64` |
| `bootOptions.efiSecureBootEnabled` | shielded instance (`platformConfig.isSecureBootEnabled`, plus Measured Boot + TPM on VM shapes) |
| any disk controller (IDE, LSI Logic, PVSCSI, SATA, NVMe) | `bootVolumeType = PARAVIRTUALIZED` |
| any NIC model (e1000, e1000e, vmxnet3, ...) | `networkType = PARAVIRTUALIZED` |
| *Maximum compatibility* checkbox | `IDE` + `E1000` |
| explicit overrides | win over everything |
| data volumes | `remoteDataVolumeType` follows the boot volume's device class (`PARAVIRTUALIZED`; `SCSI` for IDE/SCSI; `ISCSI` for iSCSI) because OCI refuses to mix paravirtualized and emulated volumes in one instance; the target attachments use the same class (paravirtualized/iSCSI: attached read/write shareable before the first boot; emulated: hot-plugged after the start) |
| Linux guest | seed image schema `Storage.ConsistentVolumeNaming = true`; data volumes attached with `device=/dev/oracleoci/oraclevdX` |
| Windows guest | seed image schema `Storage.ConsistentVolumeNaming = false`; data volumes attached without a device path (OCI rejects it for Windows) |

`isConsistentVolumeNamingEnabled` is never sent in `LaunchOptions`: OCI rejects any value that differs from the image schema ("Overriding ConsistentVolumeNamingEnabled in LaunchOptions is not supported").

Seed images exist per (firmware, OS, Secure Boot, import launch mode): a paravirtualized launch cannot use a seed imported as `EMULATED` and vice versa, so the *Maximum compatibility* preset gets its own `-emulated` seed.

## Shape

`ocpus = max(1, ceil(vCPU / 2))`, `memory_gb = clamp(ceil(RAM_MB / 1024), 1 x ocpus, 64 x ocpus)` on the
selected flex shape (`HELPER_DEFAULT_SHAPE`, default `VM.Standard.E5.Flex`).

## Volumes

Boot volume size = disk 0 capacity rounded up to GB, minimum 50 GB. Each additional disk becomes a
block volume with the same rule. Extra space stays unpartitioned inside the guest.

All volumes of a migration are created with the performance tier chosen under *Advanced: disk
transfer* (`volume_vpus_per_gb`): 10 VPU/GB (Balanced, default), 20 (Higher Performance) or 30
(Ultra High Performance). The tier can be changed in OCI after the migration.
