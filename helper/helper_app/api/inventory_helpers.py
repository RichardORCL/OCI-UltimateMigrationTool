"""Shared inventory cache and guest metadata construction for cloud providers."""

from collections.abc import Callable
from time import monotonic
from typing import TypeVar, cast

from helper_app.models import GuestOsMapping, VmSpec
from helper_app.oci.mapping import map_guest_os, os_version_choices
from helper_app.sessions import UserSession

T = TypeVar("T")
VM_LIST_CACHE_S = 30.0


def cached_inventory(session: UserSession, key: str, refresh: bool, fetch: Callable[[], T]) -> T:
    cached = cast(tuple[float, T] | None, session.cache.get(key))
    now = monotonic()
    if not refresh and cached is not None and now - cached[0] < VM_LIST_CACHE_S:
        return cached[1]
    result = fetch()
    session.cache[key] = (now, result)
    return result


def guest_mapping(spec: VmSpec) -> GuestOsMapping:
    meta = map_guest_os(spec.guest_id, spec.guest_full_name)
    return GuestOsMapping(
        operating_system=meta.operating_system,
        operating_system_version=meta.operating_system_version,
        version_detected=meta.version_detected,
        version_choices=os_version_choices(meta),
    )
