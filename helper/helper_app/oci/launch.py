"""Pieces of ``LaunchInstanceDetails`` shared by the VMware migration and the ISO based instances."""

from __future__ import annotations

import logging
import re

from helper_app.oci.clients import OciClients, OciError
from helper_app.oci.mapping import PLATFORM_AMD_VM, PLATFORM_GENERIC_BM, PLATFORM_INTEL_VM, platform_config_type

log = logging.getLogger(__name__)

_HOSTNAME_RE = re.compile(r"[^a-z0-9-]")


def hostname_label(name: str) -> str | None:
    label = re.sub(r"-+", "-", _HOSTNAME_RE.sub("-", name.lower())).strip("-")[:63].strip("-")
    return label or None


def free_hostname_label(c: OciClients, subnet_id: str, display: str) -> str | None:
    """DNS labels are unique per subnet; a clash makes the launch fail asynchronously ("Hostname ... is
    already used in subnet"), so pick ``name``, ``name-2``, ``name-3``, ... against the subnet's private IPs.
    Falls back to the plain label when the subnet cannot be listed."""
    import oci

    base = hostname_label(display)
    if not base:
        return None
    try:
        ips = oci.pagination.list_call_get_all_results(c.network.list_private_ips, subnet_id=subnet_id).data
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot list private IPs of subnet %s to check the hostname label: %s", subnet_id, exc)
        return base
    taken = {ip.hostname_label.lower() for ip in ips if getattr(ip, "hostname_label", None)}
    if base not in taken:
        return base
    for n in range(2, 1000):
        suffix = f"-{n}"
        candidate = base[: 63 - len(suffix)].rstrip("-") + suffix
        if candidate not in taken:
            log.info("hostname label %s is already used in subnet %s; using %s", base, subnet_id, candidate)
            return candidate
    return None


def secure_boot_platform_config(shape: str, windows: bool, what: str = "the source VM boots with UEFI Secure Boot"):
    """``platform_config`` that turns Secure Boot on for the given shape (OCI calls this a shielded
    instance).

    On VM shapes OCI insists that Secure Boot, Measured Boot and the (virtual) TPM are enabled together;
    sending Secure Boot alone is rejected (for Windows images with "Invalid platform configuration for
    instances secured with Credential Guard ...").  Bare metal allows the three independently, but Windows
    is held to the same all-or-nothing rule there, while Measured Boot on Linux is VM-only."""
    import oci.core.models as M

    kind = platform_config_type(shape)
    all_three = kind != PLATFORM_GENERIC_BM or windows
    flags = dict(is_secure_boot_enabled=True, is_measured_boot_enabled=all_three,
                 is_trusted_platform_module_enabled=all_three)
    if kind == PLATFORM_AMD_VM:
        return M.AmdVmLaunchInstancePlatformConfig(**flags)
    if kind == PLATFORM_INTEL_VM:
        return M.IntelVmLaunchInstancePlatformConfig(**flags)
    if kind == PLATFORM_GENERIC_BM:
        return M.GenericBmLaunchInstancePlatformConfig(**flags)
    raise OciError(
        f"{what}, but shape {shape} cannot launch a shielded instance; "
        "choose an x86 shape (e.g. VM.Standard.E4/E5.Flex, VM.Standard3.Flex, VM.Optimized3.Flex)"
    )
