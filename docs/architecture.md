# Architecture

## Single component

The whole tool is one service, the **OCI Ultimate Migration Tool**, running on a compute instance in OCI
(the *OCI Migration Tool VM*):

- a web UI (`/ui`) and REST API (`/api`) served by FastAPI/uvicorn over TLS on port 8443;
- a source client chosen at login: pyVmomi for vCenter/ESXi, or a small `httpx` REST client for OLVM
  (oVirt API 4 and the image proxy), Azure (Entra ID + ARM), AWS (SigV4: STS, EC2, EBS Direct) or
  Google Cloud (service-account JWT: Compute Engine, Cloud Build, Cloud Storage);
- the migration engine that provisions the OCI target, pulls the disks from the source (NFC, OLVM image
  transfer, Azure page blob, EBS Direct, GCS export tarball, or an OVA/OVF in Object Storage) and writes them onto OCI
  volumes attached to the Migration Tool itself.

There is no agent in the source environment and no shared secret: the source platform's own RBAC
decides who may export which VM; OCI IAM (instance principal + dynamic group policy) decides what the
Migration Tool may create.

## Authentication and sessions

- Each source has its own login (`POST /api/auth/login` for vCenter/ESXi, `/auth/azure/login`,
  `/auth/aws/login`, `/auth/gcp/login`). Credentials stay in memory on a `UserSession`; the browser gets
  an opaque HttpOnly, SameSite=strict cookie. `POST /api/auth/anonymous` (or a first visit) is enough
  for ISO, OVA, Remote Console and Setup. An optional UI password must be unlocked first
  (`POST /api/auth/unlock`) when one is configured.
- Logging in to a source replaces the previous source login. `GET /api/auth/me` reports which source
  the session holds (vCenter host, Azure tenant/subscriptions, AWS account/region, GCP project/bucket,
  or anonymous).
- Source inventory and job-create routes require the matching login (`/api/vms/*` and `POST /api/jobs`
  for VMware, `/api/azure/*` and `POST /api/jobs/azure` for Azure, and the AWS/GCP equivalents).
  `/api/jobs/*` (watch, cancel, console), `/api/oci/*` and `/api/setup/*` need a session cookie.
  `/api/health` and `/api/auth/config` are public.
- Sessions expire after `HELPER_SESSION_TTL_S` (default 8 h) of inactivity or on logout.
- A session that started a migration is **pinned** by the job: logout/expiry make the cookie unusable
  immediately, but the source connection is only closed after the job has finished, so NFC leases,
  export SAS tokens and snapshots can always be released. A new login (same or different user) can
  watch the job.

## Job lifecycle

`Job.kind` is `vmware`, `olvm`, `azure`, `aws`, `gcp`, `ova`, `ovaexport` or `iso`. Source-specific state
lives on the job (`Job.olvm` holds the engine host, cluster and disk ids; `Job.azure`, `Job.aws`, `Job.gcp`,
`Job.ova`, `Job.iso` hold capture mode, snapshot IDs, export SAS, GCS objects, and so on).
`Job.phase`: `QUEUED -> PROVISIONING -> EXPORTING -> FINALIZING -> COMPLETED | FAILED | CANCELLED`.
`Job.step`/`Job.message` carry the fine-grained progress; `Job.step_percent` is set while a step is backed
by an OCI work request with a `percentComplete` (the seed image import polls its `CreateImage` work
request between image state checks; reading it is best effort and needs `read work-requests`).
`Job.disks[]` holds the per-disk state
(`PENDING -> ATTACHED -> COPYING -> COPIED | FAILED`, with `bytes_received`, `bytes_written`,
`grains_written`, `attempts`, `stream_bytes` (size of the exported stream when the lease reports it),
`percent` and `throughput_bps` (received bytes/s over the last minute)).
`Job.transfer` aggregates the export phase: `percent` is what the runner reports (for VMware, the
same value sent to the NFC lease); `bytes_received` counts every byte pulled from the source
including retried attempts; `started_at`/`finished_at` bracket the copy.
`Job.summary` (derived on read, not stored) gives `duration_s`, `transfer_duration_s`,
`bytes_received`, `bytes_written` and `average_bps` for finished jobs; `Job.finished_at` is set when a
job reaches a terminal phase. Progress is persisted every 128 MiB or 2 seconds, whichever comes first.

`MigrationRunner` runs each job in its own thread; at most `HELPER_MAX_CONCURRENT_JOBS` of them hold a
migration slot at a time (adjustable on the *Setup* page at runtime - raising it starts queued jobs,
lowering it never interrupts a running one), the rest stay **QUEUED** ("Waiting for a free migration slot"):

1. **PROVISIONING** (`Provisioner.prepare`)
   - map guest OS -> seed image metadata, firmware -> `BIOS`/`UEFI_64`, device model -> launch
     options (paravirtualized unless *Maximum compatibility* / overrides), vCPU/RAM -> flex shape
     ([os-mapping.md](os-mapping.md));
   - `SeedImageService.get_or_create`: import a 1 GB placeholder stream-optimized VMDK as a custom
     image (`launchMode` PARAVIRTUALIZED, or EMULATED for IDE/E1000), pin its capability schema
     (firmware fixed; all boot volume and NIC types allowed), reuse by freeform tags on later jobs;
   - `LaunchInstance` from the seed image with explicit `launchOptions`, `shapeConfig`, optional
     `licensingConfigs` (Windows) and a boot volume sized for disk 0. The instance carries
     provenance freeform tags: `oci-umt-job`, `oci-umt-source-vcenter` (the vCenter the job was
     started against, `host[:port]`), `oci-umt-source-esxi-host`, `oci-umt-source-vm`,
     `oci-umt-source-moid` and `oci-umt-source-vm-details` (sizing: vCPU, RAM, disk count and
     capacities, NICs, guest OS, firmware/Secure Boot). Other sources use the same tags with their
     own identifiers (`oci-umt-source-azure`, AWS account/region, GCP project/zone, OVA object name);
   - create one block volume per additional disk and attach them to the target **while it is
     still running** from the seed image, as read/write *shareable* attachments (OCI only attaches
     data volumes to a `RUNNING` instance, and the target has to be stopped for the boot volume
     swap below). These attachments are kept, so the guest finds all its disks on its first boot.
     Emulated attachments (*Maximum compatibility*, `SCSI`/`IDE`) cannot be shareable and are
     hot-plugged in the finalize step instead;
   - stop the target (hard stop; the placeholder has no OS); detach its boot volume;
   - attach boot + data volumes to the migration tool (paravirtualized; the data volumes as the second
     shareable attachment). Data volumes get consistent device names (`/dev/oracleoci/oraclevd*`);
     OCI does not allow a device path for a boot volume attached as a data volume, so the migration tool
     snapshots `/sys/block`, attaches, and takes the one new disk of about the expected size (serialised
     across jobs).
   - A pending cancellation is honoured between provisioning steps.
2. **EXPORTING** — the source-specific copy. VMware uses NFC (below). Azure uses a read SAS on the
   disk or a snapshot (`disk/vhd_range_copy.py`). AWS snapshots each EBS volume and reads it with
   EBS Direct (`disk/ebs_range_copy.py`). Google Cloud snapshots each disk, exports it through Cloud
   Build to a GCS tarball and streams `disk.raw` (`disk/gcs_range_copy.py`). OVA import streams
   VMDKs from Object Storage (`disk/object_vmdk_copy.py`). See [how-it-works.md](how-it-works.md)
   for the per-source sequence.
   - **VMware:** re-check the power state; a VM that is still powered on (and whose job carries the
     operator's `power_off_source` confirmation) is shut down now (`vsphere/power.py`: `ShutdownGuest`
     when Tools runs, waiting `HELPER_GUEST_SHUTDOWN_TIMEOUT_S`, else/then `PowerOffVM_Task`); the
     outcome is stored as `job.power_off_result`;
   - `vm.ExportVm()` on the user's vCenter session, wait for the
     lease to be `ready`, keep it alive with `HttpNfcLeaseProgress` every
     `HELPER_LEASE_PROGRESS_INTERVAL_S`;
   - match lease `deviceUrl`s to the VM disks (controller/bus/unit key, then `disk-N.vmdk` target
     id, then order); rewrite the `*` host placeholder to the ESXi host the VM is registered on
     (`vm.runtime.host.name`, when the job was started with *Download the disks directly from the
     ESXi host*), else `HELPER_NFC_HOST_OVERRIDE`, else the vCenter host; the chosen host is
     recorded as `Job.nfc_host`;
   - per disk: HTTPS `GET` the stream-optimized VMDK in `HELPER_NFC_CHUNK_BYTES` chunks and feed it
     to `StreamOptimizedDecoder`, which inflates each grain and `pwrite()`s it at
     `lba * 512` on the attached volume (`BlockDeviceWriter`). All-zero grains are skipped
     (`HELPER_SKIP_ZERO_GRAINS`, fresh volumes read as zero). With *Decode and write on a separate
     thread* (`OciTarget.pipelined_decode`) the decoder runs behind a bounded queue of
     `HELPER_NFC_PIPELINE_DEPTH` chunks (`PipelinedDecoder`), so the socket keeps being read while
     grains are inflated and written; decoder/writer errors are re-raised on the download thread
     with their original type. A failure restarts the disk from the beginning, up to
     `HELPER_DISK_RETRY_ATTEMPTS` times; the lease is completed or aborted on exit.
   - **OLVM jobs** (`MigrationRunner._run_olvm`) replace the NFC part of this phase: a VM that is still
     up and whose job carries `power_off_source` is shut down (`shutdown`, then `stop` if it is not down
     within `HELPER_OLVM_SHUTDOWN_TIMEOUT_S`; `power_off_result` is `guest_shutdown`, `powered_off` or
     `already_off`). Each disk then gets an image transfer (`POST /imagetransfers`, `format=raw`). The
     copy reads `/extents` from the engine proxy URL (or the KVM host URL when
     `olvm_direct_from_host` is set) and writes the non-zero ranges
     (`disk/imageio_range_copy.py`, `HELPER_OLVM_RANGE_CHUNK_BYTES`, `HELPER_OLVM_RANGE_WORKERS`).
     OLVM 4.5 authorizes that download with the proxy URL itself. The transfer is finalized
     (`POST /imagetransfers/{id}/finalize`) on success and cancelled (`POST .../cancel`) on failure,
     retry or exit; a phase PUT is not used, because this engine answers 405 and would leave the disk locked.
   - **Azure jobs** (`MigrationRunner._run_azure`) replace the NFC part of this phase:
     - *deallocate* mode: a VM that is still running (or stopped but allocated) and whose job carries the
       operator's `power_off_source` confirmation is deallocated now (`POST .../deallocate`, polled up to
       `HELPER_AZURE_DEALLOCATE_TIMEOUT_S`; `power_off_result = deallocated`, or `already_off`); without
       the confirmation a running VM fails the job before any export. *snapshot* mode never touches the
       VM: `AzureDiskExport` creates one snapshot per disk (`PUT .../snapshots/<name>`, polled up to
       `HELPER_AZURE_SNAPSHOT_TIMEOUT_S`, `power_off_result = snapshotted`) and records their IDs in
       `Job.azure.snapshot_ids` as soon as each exists, so cleanup can find them;
     - `beginGetAccess` (read SAS, `HELPER_AZURE_SAS_DURATION_S`) on every disk or snapshot; the granted
       resource IDs and the expiry are stored in `Job.azure.sas_granted` / `sas_expires_at`;
     - per disk (`_copy_azure_disk`): `disk/vhd_range_copy.py` reads the blob length (minus the 512-byte
       VHD footer), lists the allocated page ranges (`GET ?comp=pagelist`, paginated with `marker`) and
       downloads them in `HELPER_AZURE_RANGE_CHUNK_BYTES` pieces with `HELPER_AZURE_RANGE_WORKERS` threads,
       each `pwrite()`-ing at its offset on the attached volume (`BlockDeviceWriter`, positional, so no
       ordering is needed). A single range is retried with backoff (3 tries) before the disk attempt
       fails; a 403 from the blob refreshes the SAS (`beginGetAccess` again) and continues; a disk attempt
       failure restarts the disk like the NFC path, up to `HELPER_DISK_RETRY_ATTEMPTS`. `disk.stream_bytes`
       is the allocated size, so `percent` is exact and `bytes_received == bytes_written`;
     - on exit (success, failure or cancel) `AzureDiskExport.close()` calls `endGetAccess` on every granted
       resource and deletes the snapshots it created, clearing the corresponding lists on the job.
   - guest fix-up (`guest/fixup.py`, Linux only, while the boot volume is still attached to the
     migration tool): the guest root is located and mounted once (`partx`, LVM activation with a filter on
     that disk, `find_root`, `/boot` from the guest's fstab) and the opt-in steps run on it -
     `initramfs.py` (chroot dracut with virtio drivers for kernels lacking them,
     `OciTarget.rebuild_initramfs`), `network.py` (NetworkManager wildcard DHCP keyfile /
     first-boot unit for legacy network-scripts / netplan / networkd drop-in, MAC-pinned udev rules
     disabled, SELinux labels via the guest's `setfiles`, `OciTarget.fix_network`) and the
     source-cloud clean-ups (`azure_cloud.py`, `aws_cloud.py`, `gcp_cloud.py`). Each step ends in
     its own `GuestFixup` (`Job.guest_fixup`, `Job.network_fixup`: done / not_needed / skipped /
     failed with a log) and never fails the migration.
3. **FINALIZING** (`Provisioner.finalize`)
   - detach all volumes from the migration tool (the data volumes stay attached to the target), attach
     the boot volume to the target instance, start it unless *start after migration* is off.
   - Data volumes that could not be pre-attached (emulated attachments) need a running instance:
     the target is started first and they are hot-plugged (with consistent device paths for Linux
     guests, without for Windows); with *start after migration* off it is then soft-stopped again.

Cancellation sets a flag checked between chunks and steps; the runner then runs
`Provisioner.cleanup` (terminate the instance, delete volumes, best effort) and the job ends
`CANCELLED`. A `FAILED` job keeps its OCI resources for inspection; *cancel* on a failed job runs
the same cleanup. Cloud-source jobs additionally release export access and snapshots (Azure SAS,
AWS snapshots, GCS objects and Cloud Build scratch) using the source session of the user who
cancels; when that session is gone the job message names what to release by hand. If it failed with
every disk `COPIED` and an instance launched (i.e. inside `finalize`),
`POST /api/jobs/{id}/finalize` (*Retry finalize* in the UI) re-runs `Provisioner.finalize` alone:
it is idempotent and needs no source session, so the copied data is not exported again.

### Restart behaviour

Jobs are persisted in SQLite (`HELPER_DB_PATH`). Because a running export depends on the user's
in-memory source session, jobs still in `PROVISIONING`/`EXPORTING`/`FINALIZING` when the Migration
Tool starts are marked `FAILED` ("migration tool restarted..."); cancel them from the UI to clean
up and start again. Azure / AWS / Google Cloud grants and snapshots survive the restart in the job
record, so cancelling after a fresh login to that source releases them. ISO jobs in `INSTALLING`
survive a restart (no source session is needed).

## Why no temporary storage

Every source is written onto the attached OCI volumes as the data arrives. Memory use is a few MB
(or workers × chunk size) per running disk; the OCI volumes are the only storage involved.

- **VMware** serves NFC exports as stream-optimized VMDKs: each (LBA, compressed 64 KB grain) record
  is self-describing, so the decoder writes the grain to its final position as soon as it arrives.
- **Azure** exports a managed disk as a fixed VHD page blob; allocated ranges are listed and fetched
  by offset (the 512-byte VHD footer is skipped).
- **AWS** lists allocated EBS snapshot blocks and fetches them with EBS Direct.
- **Google Cloud** streams `disk.raw` from the Cloud Build export tarball and skips all-zero blocks.
- **OVA/OVF** streams VMDK grains from Object Storage the same way as NFC.

## Firmware and seed images

OCI takes an instance's firmware and device model from its image. Platform images do not expose
those knobs, so the migration tool imports a placeholder VMDK as a custom image per
(firmware, OS, Secure Boot, launch mode) combination (imported as PARAVIRTUALIZED, or EMULATED for the
IDE/E1000 compatibility preset; `CUSTOM` cannot be requested through the import API, and OCI rejects a
paravirtualized launch from an EMULATED image as "mixing paravirtualized and emulated volumes", so reuse
also matches on the image's `launchMode`), applies a `ComputeImageCapabilitySchema` that fixes
`Compute.Firmware`, sets `Compute.SecureBoot` to whether the source used Secure Boot, allows every
`Storage.BootVolumeType` / `Network.AttachmentType`, and defaults `Storage.RemoteDataVolumeType` /
`Storage.LocalDataVolumeType` to the boot volume's device class (OCI resolves the data volume model from
the schema, not from the launch request: an IDE boot with a schema still defaulting the data volumes to
PARAVIRTUALIZED is refused as "mixing paravirtualized and emulated volumes"; a reused image with such a
stale schema is repaired before the launch), and launches from it with the job's explicit
`launchOptions`. A source with `efiSecureBootEnabled` is launched with a `platformConfig`
(`AMD_VM`, `INTEL_VM` or `GENERIC_BM` depending on the shape family) that has `isSecureBootEnabled`,
i.e. as a shielded instance. On VM shapes (and for Windows on bare metal) `isMeasuredBootEnabled` and
`isTrustedPlatformModuleEnabled` are set as well, because OCI rejects Secure Boot on its own there
("... Secure Boot, Measured Boot, and the Trusted Platform Module must be enabled"). Shapes without such
a platform config (Ampere) are refused before any resource is created. The seed's boot volume is replaced by the copied disk before the instance ever
boots. Seed images are tagged `oci-umt-seed=true` (plus `oci-umt-firmware`, `oci-umt-os`,
`oci-umt-secure-boot`; seeds from before the Secure Boot tag count as `false`) and can be removed with
`DELETE /api/seed-images`.

## Windows licensing

A Windows guest is registered as `operatingSystem=Windows` on the seed image and launched with
`licensingConfigs=[{type: WINDOWS, licenseType: BRING_YOUR_OWN_LICENSE | OCI_PROVIDED}]`; the
UI requires a choice before starting and lets you change it afterwards
(`POST /api/jobs/{id}/licensing` -> `UpdateInstance`).

## Security

- Credentials are never stored on disk; the Migration Tool holds the source session (vCenter cookie,
  Azure/AWS/GCP secrets and tokens, OLVM password and bearer token) per logged-in user in memory only. The browser may remember
  non-secret fields (last vCenter host, Azure tenant/client ID), never passwords or keys.
- The API is protected by the session cookie (HttpOnly, SameSite=strict, `Secure` unless
  `HELPER_COOKIE_SECURE=false` for local development). An optional UI password gates the whole UI.
- OCI access uses the Migration Tool's instance principal; the Terraform stack scopes the policy to a
  compartment (`policy_scope_compartment_ocid`).
- vCenter and OLVM TLS verification are chosen per login (*Verify the server certificate*). Azure, AWS and
  Google Cloud TLS is always verified (public CA certificates).
- Required source privileges are listed per platform in [how-it-works.md](how-it-works.md). Export
  access (NFC lease, OLVM image transfer, Azure SAS, AWS/GCP snapshots, GCS objects) is released when the job ends.
