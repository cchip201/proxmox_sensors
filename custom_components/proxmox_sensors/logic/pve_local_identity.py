"""Persistent identity helpers for local PVE resources."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from ..const import (
    CONF_PLATFORM_TYPE,
    PVE_IDENTITY_ID,
    PVE_LOCAL_IDENTITY_VERSION,
)

LOCAL_IDENTITY_VERSION = 1


class PveLocalIdentityError(ValueError):
    pass


@dataclass(frozen=True)
class PveLocalIdentityContext:
    version: int | None
    identity_id: str | None
    node_identifier: str | None = None

    @property
    def is_legacy(self) -> bool:
        return self.version is None


def _mapping(entry, name):
    value = getattr(entry, name, {}) or {}
    return value if isinstance(value, dict) else dict(value)


def normalize_pve_identity_id(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError):
        return None
    canonical = str(parsed)
    return canonical if parsed.version == 4 and value == canonical else None


def new_pve_identity_id(entries=()) -> str:
    reserved = {
        value
        for entry in entries
        if (value := _mapping(entry, "data").get(PVE_IDENTITY_ID)) is not None
    }
    while True:
        candidate = str(uuid4())
        if candidate not in reserved:
            return candidate


def pve_local_identity_context(entry, entries=None) -> PveLocalIdentityContext:
    data = _mapping(entry, "data")
    options = _mapping(entry, "options")
    data_has_version = PVE_LOCAL_IDENTITY_VERSION in data
    data_has_identity = PVE_IDENTITY_ID in data
    if PVE_LOCAL_IDENTITY_VERSION in options or PVE_IDENTITY_ID in options:
        raise PveLocalIdentityError("PVE local identity cannot be stored in options")
    if not data_has_version and not data_has_identity:
        return PveLocalIdentityContext(None, None)
    if not data_has_version or not data_has_identity:
        raise PveLocalIdentityError("Incomplete PVE local identity")
    version = data.get(PVE_LOCAL_IDENTITY_VERSION)
    if type(version) is not int or version != LOCAL_IDENTITY_VERSION:
        raise PveLocalIdentityError("Unsupported PVE local identity version")
    identity_id = normalize_pve_identity_id(data.get(PVE_IDENTITY_ID))
    if identity_id is None:
        raise PveLocalIdentityError("Invalid PVE local identity UUID")
    if entries is not None:
        entry_id = getattr(entry, "entry_id", None)
        duplicates = [
            candidate
            for candidate in entries
            if getattr(candidate, "entry_id", None) != entry_id
            and _mapping(candidate, "data").get(CONF_PLATFORM_TYPE) == "PVE"
            and _mapping(candidate, "data").get(PVE_IDENTITY_ID) == identity_id
        ]
        if duplicates:
            raise PveLocalIdentityError("Duplicate PVE local identity UUID")
    return PveLocalIdentityContext(LOCAL_IDENTITY_VERSION, identity_id)


def coordinator_pve_local_identity_context(coordinator) -> PveLocalIdentityContext:
    context = getattr(coordinator, "pve_local_identity_context", None)
    return context or pve_local_identity_context(coordinator.config_entry)


def local_entity_unique_id(context, server_id, raw_unique_id) -> str:
    namespace = server_id if context.is_legacy else context.identity_id
    return f"pve_{namespace}_{raw_unique_id}".lower().replace(" ", "_")


def node_device_identifier(context, node) -> str:
    if identifier := getattr(context, "node_identifier", None):
        return identifier
    node = str(node)
    if context.is_legacy:
        return f"proxmox_node_{node}"
    return f"proxmox_node_{context.identity_id}_{node.lower()}"


def storage_device_identifier(context, node, storage) -> str:
    node = str(node)
    if context.is_legacy:
        return f"proxmox_storage_{node}_{storage}"
    return f"proxmox_storage_{context.identity_id}_{node.lower()}_{storage}"


def disks_group_identifier(context, node) -> str:
    node = str(node)
    if context.is_legacy:
        return f"proxmox_disks_group_{node}"
    return f"proxmox_disks_group_{context.identity_id}_{node.lower()}"


def mounted_disks_identifier(context, node) -> str:
    node = str(node)
    if context.is_legacy:
        return f"mounted_disks_{node}"
    return f"mounted_disks_{context.identity_id}_{node.lower()}"
