from __future__ import annotations

from .guest_migration_plan import plan_guest_identity_migration


def migration_inventory(hass, source_entry, cluster_entry, rows, devices, domain):
    entries = tuple(hass.config_entries.async_entries(domain))
    return plan_guest_identity_migration(source_entry, cluster_entry, entries, rows, devices)


def _device_identifiers(device, update, domain):
    identifiers = set(device.identifiers)
    old = (domain, update.old_identity)
    if old not in identifiers:
        return None
    identifiers.remove(old)
    identifiers.add((domain, update.new_identity))
    return identifiers


def validate_migration_plan(plan, entity_registry, device_registry, domain):
    if not plan.updates or plan.blocking_exclusions:
        return False
    for update in plan.entity_updates:
        row = entity_registry.async_get(update.record_id)
        if (row is None or row.config_entry_id != update.config_entry_id
                or row.unique_id != update.old_identity):
            return False
        target = entity_registry.async_get_entity_id(row.domain, domain, update.new_identity)
        if target and target != update.record_id:
            return False
    for update in plan.device_updates:
        device = device_registry.async_get(update.record_id)
        owners = getattr(device, "config_entries", None) if device is not None else None
        singular = getattr(device, "config_entry_id", None) if device is not None else None
        owned = ((set(owners) == {update.config_entry_id} and singular in (None, update.config_entry_id))
                 if owners is not None else singular == update.config_entry_id)
        if not owned or _device_identifiers(device, update, domain) is None:
            return False
        target = device_registry.async_get_device_by_identifier((domain, update.new_identity))
        if target and target.id != device.id:
            return False
    return True


def execute_migration_plan(plan, entity_registry, device_registry, domain, activate=None):
    if not validate_migration_plan(plan, entity_registry, device_registry, domain):
        return False
    changed_entities = []
    changed_devices = []
    try:
        for update in plan.entity_updates:
            entity_registry.async_update_entity(update.record_id, new_unique_id=update.new_identity)
            changed_entities.append(update)
        for update in plan.device_updates:
            device = device_registry.async_get(update.record_id)
            previous = set(device.identifiers)
            identifiers = _device_identifiers(device, update, domain)
            changed_devices.append((update, previous))
            device_registry.async_update_device(update.record_id, new_identifiers=identifiers)
        if activate is not None and not activate():
            raise RuntimeError("activation_failed")
    except Exception:
        for update, identifiers in reversed(changed_devices):
            device_registry.async_update_device(update.record_id, new_identifiers=identifiers)
        for update in reversed(changed_entities):
            entity_registry.async_update_entity(update.record_id, new_unique_id=update.old_identity)
        return False
    return True
