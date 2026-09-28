"""Resolve existing PVE node devices and authorize manual removal of stale devices."""

from dataclasses import replace
import re

from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import async_get_platforms

from ..const import DOMAIN, CONF_NODE, CONF_PLATFORM_TYPE
from .pve_local_identity import PveLocalIdentityError, node_device_identifier


def resolve_node_device_context(context, entry, registry):
    if not context.is_legacy:
        return context
    node = entry.data.get(CONF_NODE)
    if not isinstance(node, str) or not node:
        raise PveLocalIdentityError("Missing configured PVE node")
    canonical = f"proxmox_node_{node.lower()}"
    matches = {}
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        identifiers = sorted(
            value for domain, value in device.identifiers
            if domain == DOMAIN and isinstance(value, str) and value.lower() == canonical
        )
        if identifiers:
            matches[device.id] = identifiers[0]
    if len(matches) > 1:
        raise PveLocalIdentityError(
            "Ambiguous legacy PVE node devices: " + ", ".join(sorted(matches))
        )
    return replace(context, node_identifier=next(iter(matches.values()), canonical))


def can_remove_pve_device(hass, entry, device):
    if (entry.data.get(CONF_PLATFORM_TYPE) != "PVE"
            or device.config_entry_id != entry.entry_id):
        return False
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    if devices.async_get(device.id) != device:
        return False
    if er.async_entries_for_device(registry, device.id, include_disabled_entities=True):
        return False
    if dr.async_entries_for_parent_device(devices, device.id):
        return False
    if any(other.via_device_id == device.id for other in devices.async_get_devices()):
        return False
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = runtime.get("coordinator")
    if (not runtime.get("services_ready") or runtime.get("unloading")
            or coordinator is None or not coordinator.last_update_success):
        return False
    context = getattr(coordinator, "pve_local_identity_context", None)
    if context is None or not device.identifiers or device.connections:
        return False
    if (DOMAIN, node_device_identifier(context, entry.data[CONF_NODE])) in device.identifiers:
        return False
    platforms = async_get_platforms(hass, DOMAIN)
    if not platforms:
        return False
    try:
        for platform in platforms:
            for entity in platform.entities.values():
                attached = getattr(entity, "device_entry", None)
                row = getattr(entity, "registry_entry", None)
                if (getattr(attached, "id", None) == device.id
                        or getattr(row, "device_id", None) == device.id):
                    return False
                info = entity.device_info
                if info and (device.identifiers.intersection(info.get("identifiers", ()))
                             or info.get("via_device_id") == device.id):
                    return False
    except Exception:
        return False
    node = entry.data[CONF_NODE]
    retired_node = f"pve_{node}_node_{node}"
    for domain, identifier in device.identifiers:
        if domain != DOMAIN or not isinstance(identifier, str):
            return False
        if identifier == retired_node:
            continue
        guest = re.fullmatch(r"proxmox_(vm|ct)_.+_([0-9]+)_v1", identifier)
        if guest is None:
            return False
        data = coordinator.data or {}
        resources = data.get("cluster_resources")
        if data.get("cluster_resources_ok") is not True or not isinstance(resources, list):
            return False
        kind, vmid = guest.groups()
        local = data.get("vms" if kind == "vm" else "cts")
        if not isinstance(local, dict):
            return False
        for resource in resources + list(local.values()):
            if not isinstance(resource, dict):
                return False
            if str(resource.get("vmid")) == vmid:
                return False
    return True
