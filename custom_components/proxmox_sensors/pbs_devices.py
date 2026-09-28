"""Persistent PBS device identities and conservative legacy registry transition."""

import logging
from urllib.parse import quote, unquote

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def pbs_device_identifier(kind, server_id, datastore):
    """Encode components separately, preserving datastore case and delimiters."""
    return f"pbs_{kind}:{quote(server_id.lower(), safe='')}:{quote(datastore, safe='')}"


def pbs_device_datastore(identifier, server_id):
    """Read scoped identities only for their owner; accept legacy devices too."""
    for kind in ("datastore", "maintenance"):
        prefix = f"pbs_{kind}:"
        if identifier.startswith(prefix):
            parts = identifier[len(prefix):].split(":")
            if len(parts) == 2 and unquote(parts[0]) == (server_id or "").lower():
                return unquote(parts[1])
            return None
        if identifier.startswith(f"{kind}_"):
            return identifier[len(kind) + 1:]
    return None


def pbs_parent_device(coordinator):
    """Return the current PBS parent device ID."""
    from homeassistant.helpers import device_registry as dr

    server_id = coordinator.config_entry.data["server_id"].lower()
    device = dr.async_get(coordinator.hass).async_get_device_by_identifier(
        (DOMAIN, f"pbs_server_{server_id}"),
        config_entry_id=coordinator.config_entry.entry_id,
    )
    return device.id if device else None


def known_pbs_inventory(hass, entry):
    from homeassistant.helpers import device_registry as dr

    devices = dr.async_get(hass)
    server_id = entry.data["server_id"].lower()
    parent = devices.async_get_device_by_identifier(
        (DOMAIN, f"pbs_server_{server_id}"), config_entry_id=entry.entry_id,
    )
    stores = set()
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        for domain, identifier in device.identifiers:
            if domain == DOMAIN and identifier.startswith("pbs_datastore:"):
                store = pbs_device_datastore(identifier, server_id)
                if store:
                    stores.add(store)
    return parent is not None, sorted(stores)


def reconcile_pbs_devices(hass, entry, datastores):
    """Keep exclusive device IDs; split shared devices by entity entry ownership.

    Never delete or claim the shared original, even after its entities move.
    Device-targeted automations on that original need a user's explicit choice.
    """
    from homeassistant.exceptions import ConfigEntryError
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    server_id = entry.data["server_id"].lower()
    def legacy_for_entry(identifiers):
        return devices.async_get_device_by_identifier(
            next(iter(identifiers)), config_entry_id=entry.entry_id
        )

    # Preflight the whole entry before any device mutation or platform setup.
    # Skipping explicit entity moves is insufficient: EntityPlatform will update
    # an existing entity's device_id from its new device_info during registration.
    for store in datastores:
        for kind in ("datastore", "maintenance"):
            identifiers = {(DOMAIN, f"{kind}_{store}")}
            legacy = legacy_for_entry(identifiers)
            if legacy and (legacy.identifiers != identifiers or legacy.connections):
                raise ConfigEntryError(
                    f"PBS {server_id}: legacy device {legacy.id} ({kind}: {store}) "
                    "has additional identities/connections. Device migration and "
                    "platform setup stopped; review this device before reloading"
                )

    parent = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"pbs_server_{server_id}")},
        name=f"PBS Server {server_id.upper()}",
        manufacturer="Proxmox", model="Backup Server",
    )
    for store in datastores:
        for kind, label, model in (
            ("datastore", "Datastore", "Backup Server Datastore"),
            ("maintenance", "Maintenance", "Proxmox Backup Server"),
        ):
            legacy_ids = {(DOMAIN, f"{kind}_{store}")}
            scoped_ids = {(DOMAIN, pbs_device_identifier(kind, server_id, store))}
            legacy = legacy_for_entry(legacy_ids)
            target = devices.async_get_device_by_identifier(
                next(iter(scoped_ids)), config_entry_id=entry.entry_id
            )
            rows = er.async_entries_for_device(entities, legacy.id, include_disabled_entities=True) if legacy else []
            owned = [row for row in rows if row.config_entry_id == entry.entry_id
                     and row.platform == DOMAIN]
            # Require both registry ownership and every entity to agree. Unknown
            # identifiers/connections may belong to another producer: keep them.
            exclusive = (legacy is not None
                         and legacy.config_entries == {entry.entry_id}
                         and len(owned) == len(rows)
                         and legacy.identifiers == legacy_ids
                         and not legacy.connections)
            if exclusive and target is None:
                target = devices.async_update_device(
                    legacy.id, new_identifiers=scoped_ids, via_device_id=parent.id,
                )
            if target is None:
                target = devices.async_get_or_create(
                    config_entry_id=entry.entry_id, identifiers=scoped_ids,
                    name=f"{label}: {store}", manufacturer="Proxmox", model=model,
                    via_device_id=parent.id,
                )
                if legacy and owned:
                    # Copy user metadata only to a new destination. Never replace
                    # customizations already present on a scoped device.
                    metadata = {key: getattr(legacy, key) for key in
                                ("name_by_user", "area_id", "labels")
                                if getattr(legacy, key, None) is not None}
                    if getattr(legacy, "disabled_by", None) == dr.DeviceEntryDisabler.USER:
                        metadata["disabled_by"] = dr.DeviceEntryDisabler.USER
                    if metadata:
                        devices.async_update_device(target.id, **metadata)
            else:
                devices.async_update_device(target.id, via_device_id=parent.id)
            if legacy and legacy.id != target.id and owned:
                for row in owned:
                    entities.async_update_entity(row.entity_id, device_id=target.id)
                _LOGGER.warning(
                    "PBS %s: moved %d entities from legacy device %s to %s. "
                    "Kept the original device; review automations targeting its device_id",
                    server_id, len(owned), legacy.id, target.id,
                )
