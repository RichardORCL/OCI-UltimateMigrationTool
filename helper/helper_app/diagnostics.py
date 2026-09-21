"""Diagnostics bundle for a job: everything needed to analyse a failure, as one text block.

Shown behind the *Copy diagnostics* button of the job view.  Contains the helper identity, the
complete job record and the relevant lines of the service journal (lines mentioning the job id,
warnings/errors and tracebacks since the job was created).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Callable

from helper_app import __version__
from helper_app.config import Settings
from helper_app.models import Job
from helper_app.oci.clients import HelperIdentity
from helper_app.redaction import redact
from helper_app.updater import Runner, _default_runner

JOURNAL_MAX_LINES = 400
_LEVEL_RE = re.compile(r"\b(WARNING|ERROR|CRITICAL|Traceback)\b")


def journal_excerpt(job: Job, service: str, run: Runner = _default_runner) -> str:
    """Relevant service journal lines since one minute before the job was created."""
    since = (job.created_at - timedelta(minutes=1)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    rc, out = run(["journalctl", "-u", service, "--since", since, "--no-pager", "-o", "short-iso", "--utc"], 30.0)
    if rc != 0:
        return f"(journal not available: {out.strip() or rc})"
    keep: list[str] = []
    in_traceback = False
    for line in out.splitlines():
        # journal lines: "<timestamp> <host> <unit>[pid]: <message>"
        message = line.split("]: ", 1)[1] if "]: " in line else line
        continuation = message.startswith((" ", "\t")) or message.startswith(("File ", "Traceback"))
        if job.id in message or _LEVEL_RE.search(message):
            keep.append(line)
            in_traceback = "Traceback" in message or " ERROR " in line
        elif in_traceback and (continuation or not message.startswith("20")):
            keep.append(line)  # traceback body / exception line following an ERROR
            if not continuation and re.match(r"^\w+(\.\w+)*(Error|Exception|Cancelled)\b", message):
                in_traceback = False
        else:
            in_traceback = False
    if len(keep) > JOURNAL_MAX_LINES:
        keep = [f"... {len(keep) - JOURNAL_MAX_LINES} earlier lines omitted ..."] + keep[-JOURNAL_MAX_LINES:]
    return "\n".join(keep) or "(no matching journal lines)"


def _num(v) -> str:
    return "-" if v is None else f"{v:.0f}"


def _fixup_lines(job: Job) -> str:
    blocks = []
    for name, fx, enabled in (("guest fixup", job.guest_fixup, job.target.rebuild_initramfs),
                              ("network fixup", job.network_fixup, job.target.fix_network),
                              ("azure fixup", job.azure_fixup,
                               job.kind == "azure" and job.target.azure_cleanup),
                              ("gcp fixup", job.gcp_fixup,
                               job.kind == "gcp" and job.target.gcp_cleanup),
                              ("aws fixup", job.aws_fixup,
                               job.kind == "aws" and job.target.aws_cleanup)):
        head = f"  {name}={fx.status if fx else '-'} (enabled={enabled}): {fx.detail if fx else '-'}"
        extra = ([f"    kernels: {', '.join(fx.kernels)}"] if fx and fx.kernels else [])
        extra += [f"    - {line}" for line in fx.log] if fx else []
        blocks.append("\n".join([head, *extra]))
    return "\n".join(blocks)


def _source_lines(job: Job, settings: Settings) -> list[str]:
    if job.vm is not None and job.azure is not None:
        az = job.azure
        return [
            f"  source Azure VM: {job.vm.name} ({job.vm.moid}) guest={job.vm.guest_id} firmware={job.vm.firmware} "
            f"cpu={job.vm.num_cpu} mem_mb={job.vm.memory_mb} disks={len(job.vm.disks)} "
            f"power_off_source={job.power_off_source} power_off_result={job.power_off_result or '-'}",
            f"  azure: tenant={az.tenant_id} subscription={az.subscription_id} resource_group={az.resource_group} "
            f"location={az.location or '-'} vm_size={az.vm_size or '-'} capture_mode={az.capture_mode} "
            f"snapshots={','.join(s.rsplit('/', 1)[-1] for s in az.snapshot_ids) or '-'} "
            f"sas_granted={len(az.sas_granted)} "
            f"sas_expires_at={az.sas_expires_at.isoformat() if az.sas_expires_at else '-'} "
            f"range_workers={settings.azure_range_workers} chunk_bytes={settings.azure_range_chunk_bytes}",
            _fixup_lines(job),
        ]
    if job.vm is not None and job.gcp is not None:
        g = job.gcp
        return [
            f"  source GCP VM: {job.vm.name} ({job.vm.moid}) guest={job.vm.guest_id} firmware={job.vm.firmware} "
            f"cpu={job.vm.num_cpu} mem_mb={job.vm.memory_mb} disks={len(job.vm.disks)} "
            f"power_off_source={job.power_off_source} power_off_result={job.power_off_result or '-'}",
            f"  gcp: project={g.project_id} zone={g.zone} machine_type={g.machine_type or '-'} "
            f"bucket={g.export_bucket} prefix={g.export_prefix} capture_mode={g.capture_mode} "
            f"snapshots={','.join(g.snapshot_names) or '-'} objects={','.join(g.gcs_objects) or '-'} "
            f"range_workers={settings.gcp_range_workers} chunk_bytes={settings.gcp_range_chunk_bytes}",
            _fixup_lines(job),
        ]
    if job.vm is not None and job.aws is not None:
        a = job.aws
        return [
            f"  source AWS VM: {job.vm.name} ({job.vm.moid}) guest={job.vm.guest_id} firmware={job.vm.firmware} "
            f"cpu={job.vm.num_cpu} mem_mb={job.vm.memory_mb} disks={len(job.vm.disks)} "
            f"power_off_source={job.power_off_source} power_off_result={job.power_off_result or '-'}",
            f"  aws: account={a.account_id} region={a.region} instance={a.instance_id} "
            f"type={a.instance_type or '-'} capture_mode={a.capture_mode} "
            f"volumes={','.join(a.volume_ids) or '-'} snapshots={','.join(a.snapshot_ids) or '-'} "
            f"range_workers={settings.aws_range_workers}",
            _fixup_lines(job),
        ]
    if job.vm is not None:
        return [
            f"  source VM: {job.vm.name} ({job.vm.moid}) guest={job.vm.guest_id} firmware={job.vm.firmware} "
            f"cpu={job.vm.num_cpu} mem_mb={job.vm.memory_mb} disks={len(job.vm.disks)} "
            f"vcenter={job.vcenter_host or '-'} esxi_host={job.vm.host_name or '-'} "
            f"power_off_source={job.power_off_source} power_off_result={job.power_off_result or '-'}",
            f"  nfc download: host={job.nfc_host or '-'} direct_to_esxi={job.target.nfc_direct_to_esxi} "
            f"pipelined_decode={job.target.pipelined_decode} chunk_bytes={settings.nfc_chunk_bytes} "
            f"pipeline_depth={settings.nfc_pipeline_depth}",
            _fixup_lines(job),
        ]
    iso = job.iso
    if iso is not None:
        return [
            f"  source ISO: {iso.key} size={iso.size_bytes} etag={iso.etag or '-'} os={iso.operating_system} "
            f"{iso.operating_system_version} firmware={iso.firmware} secure_boot={iso.secure_boot} "
            f"boot_disk_gb={iso.boot_disk_gb} image_type={settings.iso_source_image_type}",
        ]
    ova = job.ova
    if ova is not None:
        lines = [
            f"  source OVA: {ova.key} size={ova.size_bytes} etag={ova.etag or '-'} os={ova.operating_system} "
            f"{ova.operating_system_version} firmware={ova.firmware} secure_boot={ova.secure_boot} "
            f"boot_volume_id={job.boot_volume_id or '-'} ova_image_id={job.ova_image_id or '-'} "
            f"staging={job.ova_staging_prefix or '-'}",
        ]
        return lines
    xe = job.ova_export
    if xe is not None:
        atts = ", ".join(
            f"{s.display_name or s.volume_id}{' (boot)' if s.is_boot else ''} {s.attachment_id or '-'}"
            for s in xe.sources
        ) or "-"
        return [
            f"  source OCI instance: {xe.instance_name or '-'} ({xe.instance_id}) "
            f"shape={xe.shape or '-'} ocpus={xe.ocpus or '-'} memory_gb={xe.memory_gb or '-'} "
            f"os={xe.operating_system or '-'} {xe.operating_system_version or ''} "
            f"firmware={xe.firmware or '-'} secure_boot={xe.secure_boot} "
            f"was_running={xe.was_running} include_data_volumes={xe.include_data_volumes}",
            f"  destination: {xe.namespace}/{xe.bucket}/{xe.prefix or ''} "
            f"ovf={xe.ovf_object or '-'} mf={xe.manifest_object or '-'} "
            f"objects={','.join(xe.objects) or '-'}",
            f"  attachments: {atts}",
        ]
    return ["  source: -"]


def collect(job: Job, settings: Settings, ident: HelperIdentity, commit: str,
            run: Runner = _default_runner, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> str:
    """The text block copied to the clipboard."""
    disks = "\n".join(
        f"  disk {d.index} {d.label or ''} {d.capacity_bytes} bytes boot={d.is_boot} status={d.status.value} "
        f"attempts={d.attempts} received={d.bytes_received} written={d.bytes_written} volume={d.volume_id or '-'} "
        f"device={d.device or '-'}" + (f"\n    error: {d.error}" if d.error else "")
        for d in job.disks
    )
    lo = job.launch_options
    header = [
        "=== OCI Ultimate Migration Tool - job diagnostics ===",
        f"generated: {now().isoformat(timespec='seconds')}",
        f"migration tool: version {__version__} commit {commit or '-'}",
        f"migration tool VM: instance {ident.instance_id} compartment {ident.compartment_id} "
        f"region {ident.region} AD {ident.availability_domain}",
        f"settings: vcenter_default={settings.vcenter_host or '-'} "
        f"nfc_host_override={settings.nfc_host_override or '-'} seed_bucket={settings.seed_bucket} "
        f"seed_compartment={settings.seed_compartment_id or '(migration tool VM)'} "
        f"max_concurrent_jobs={settings.max_concurrent_jobs} disk_retry_attempts={settings.disk_retry_attempts}",
        "",
        f"job {job.id}: phase={job.phase.value} kind={job.kind} step={job.step or '-'}"
        + (f" step_percent={job.step_percent}" if job.step_percent is not None else ""),
        f"  created {job.created_at.isoformat(timespec='seconds')} by {job.created_by or '-'}; "
        f"updated {job.updated_at.isoformat(timespec='seconds')}",
        f"  message: {job.message or '-'}",
        f"  error: {job.error or '-'}",
        *_source_lines(job, settings),
        f"  target: compartment={job.target.compartment_id} AD={job.target.availability_domain} "
        f"subnet={job.target.subnet_id} shape={job.target.shape or '(default)'} "
        f"ocpus={job.target.ocpus or 'auto'} memory_gb={job.target.memory_gb or 'auto'} "
        f"os_version={job.target.operating_system_version or 'detected'} "
        f"private_ip={job.target.private_ip or 'dhcp'} public_ip={job.target.assign_public_ip} "
        f"license={job.target.windows_license_type} "
        f"volume_vpus_per_gb={job.target.volume_vpus_per_gb}",
        f"  launch options: {lo.firmware} boot={lo.boot_volume_type.value} nic={lo.network_type.value} "
        f"secure_boot={lo.secure_boot}" if lo
        else "  launch options: -",
        f"  instance={job.instance_id or '-'} seed_image={job.seed_image_id or '-'} "
        f"iso_image={job.iso_image_id or '-'} boot_volume={job.boot_volume_id or '-'}",
        f"  transfer: percent={job.transfer.percent} received={job.transfer.bytes_received} "
        f"written={job.transfer.bytes_written} "
        f"duration_s={_num(job.transfer.duration_s)} average_bps={_num(job.transfer.average_bps)} "
        f"job_duration_s={_num(job.summary.duration_s)}",
        "  disks:",
        disks or "  (none)",
        "",
        "--- job record (JSON) ---",
        json.dumps(job.model_dump(mode="json"), indent=2, sort_keys=True),
        "",
        f"--- migration tool journal ({settings.update_service}) ---",
        journal_excerpt(job, settings.update_service, run),
    ]
    return redact("\n".join(header) + "\n")
