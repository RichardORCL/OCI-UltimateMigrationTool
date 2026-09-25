"""Helper configuration (environment driven, ``HELPER_`` prefix)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

from helper_app.branding import SEED_BUCKET_DEFAULT

PACKAGE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HELPER_", env_file=".env", extra="ignore")

    # HTTP
    listen_host: str = "0.0.0.0"
    listen_port: int = 8443
    tls_cert_file: Optional[str] = None
    tls_key_file: Optional[str] = None
    ui_dir: str = str(PACKAGE_DIR / "ui")  # static web UI shipped with the package

    # Web UI sessions (users log in with vCenter credentials)
    session_cookie_name: str = "vcoci_session"
    session_ttl_s: int = 8 * 3600  # idle timeout
    cookie_secure: bool = True  # set false only for plain-HTTP development

    # Optional defaults for the login page: the vCenter/ESXi server and the TLS-verification checkbox are
    # entered there per login (the Resource Manager stack does not set these); the values chosen at login
    # apply to the SOAP API and the NFC disk download of that session.
    vcenter_host: str = ""
    vcenter_port: int = 443
    vcenter_verify_ssl: bool = False

    # NFC export
    nfc_host_override: Optional[str] = None  # host substituted for '*' in lease URLs; default: session's vCenter
    nfc_chunk_bytes: int = 1024 * 1024
    nfc_pipeline_depth: int = 8  # chunks buffered between download and decode/write when a job opts in
    lease_progress_interval_s: int = 60
    lease_ready_timeout_s: int = 300
    disk_retry_attempts: int = 3
    # Powered-on source VMs are shut down right before the export: guest OS shutdown through VMware Tools,
    # hard power-off when Tools is not running or the guest has not stopped after this many seconds
    guest_shutdown_timeout_s: int = 300

    # Azure source: managed disks are exported through a read SAS (beginGetAccess) on the disk or on a
    # snapshot; the allocated page ranges are downloaded with parallel range requests.
    azure_sas_duration_s: int = 24 * 3600  # validity of the export SAS; renewed when a copy outlives it
    azure_range_workers: int = 4  # concurrent range downloads per disk
    azure_range_chunk_bytes: int = 8 * 1024 * 1024  # size of one range request
    azure_deallocate_timeout_s: int = 900
    azure_snapshot_timeout_s: int = 900  # snapshot creation (snapshot mode), per disk

    # GCP source: snapshots exported to a user-supplied GCS bucket, then copied with parallel range GETs.
    gcp_stop_timeout_s: int = 900
    gcp_snapshot_timeout_s: int = 900
    gcp_export_timeout_s: int = 7200  # Cloud Build disk export to GCS (gce_vm_image_export), per disk
    gcp_range_workers: int = 4
    gcp_range_chunk_bytes: int = 8 * 1024 * 1024

    # AWS source: EBS snapshots are read with the EBS Direct APIs (ListSnapshotBlocks / GetSnapshotBlock).
    aws_stop_timeout_s: int = 900
    aws_snapshot_timeout_s: int = 900
    aws_range_workers: int = 4

    # OLVM source: after the VM is shut down, disks are downloaded through the engine image proxy
    # (oVirt image transfer, raw extents). The tool VM must reach the engine HTTPS port and the
    # image proxy (typically 54323).
    olvm_shutdown_timeout_s: int = 300
    olvm_transfer_timeout_s: int = 300  # wait until an image transfer reports phase=transferring
    olvm_transfer_inactivity_s: int = 300  # engine-side lock timeout while a transfer is idle
    olvm_range_workers: int = 4
    olvm_range_chunk_bytes: int = 8 * 1024 * 1024

    # Hyper-V source: WinRM (5986/5985) for inventory and power, SMB (445) to read the VHD/VHDX files
    # after the VM is shut down. Unallocated blocks of a dynamic disk are skipped.
    hyperv_shutdown_timeout_s: int = 300
    hyperv_range_workers: int = 4
    hyperv_range_chunk_bytes: int = 8 * 1024 * 1024

    # Logging (both adjustable from the Setup page; changes persist in runtime_settings_path)
    log_level: str = "INFO"
    oci_log_requests: bool = False  # enable redacted OCI SDK diagnostic logging at DEBUG (no raw wire dumps)
    runtime_settings_path: str = "/var/lib/vc-oci-helper/runtime-settings.json"
    ui_password_hash_path: str = "/var/lib/vc-oci-helper/ui-password.hash"

    # OCI authentication: instance principals on the helper VM, config file for development
    oci_auth: Literal["instance_principal", "config_file", "mock"] = "instance_principal"
    oci_config_file: str = "~/.oci/config"
    oci_profile: str = "DEFAULT"

    # Identity of the helper itself; auto-discovered from the instance metadata service when empty
    instance_id: str = ""
    compartment_id: str = ""
    availability_domain: str = ""
    region: str = ""
    tenancy_id: str = ""

    # Seed image handling
    seed_bucket: str = SEED_BUCKET_DEFAULT
    seed_compartment_id: str = ""  # defaults to the helper compartment
    seed_disk_size_gb: int = 1

    # ISO based instances: the installer ISO in Object Storage is imported as a custom image with the
    # documented ``VMDK`` source type (OCI recognises the ISO content and boots it as installation media);
    # the token is configurable in case OCI introduces a dedicated type.
    iso_source_image_type: str = "VMDK"
    iso_image_compartment_id: str = ""  # where the imported ISO images live; defaults to the seed compartment

    # Instance defaults
    default_shape: str = "VM.Standard.E5.Flex"
    max_memory_gb_per_ocpu: int = 64
    min_volume_gb: int = 50
    device_prefix: str = "/dev/oracleoci/oraclevd"

    # Job execution
    db_path: str = "/var/lib/vc-oci-helper/jobs.sqlite3"
    max_concurrent_jobs: int = 2
    launch_timeout_s: int = 1800
    volume_timeout_s: int = 900
    image_import_timeout_s: int = 3600
    skip_zero_grains: bool = True

    # Remote (VNC) console of a migrated instance: OCI instance console connection + SSH tunnel bridged into
    # the browser.  The console connection is deleted after this long without a viewer.
    console_idle_timeout_s: int = 600
    console_connect_timeout_s: int = 120  # CreateInstanceConsoleConnection -> ACTIVE, and the SSH hops

    # Self-update from the git checkout the helper was installed from (source install on the helper VM)
    update_source_dir: str = "/opt/vc-oci/src"
    update_venv_dir: str = "/opt/vc-oci/venv"
    update_service: str = "vc-oci-helper"
    update_log_path: str = "/var/lib/vc-oci-helper/update.log"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
