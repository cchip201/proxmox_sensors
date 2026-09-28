"""Replication summaries and stable job measurements on existing guest devices."""

from datetime import datetime, timezone
import logging
import math
import re

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfTime
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send

from ..logic.guest_cleanup import cleanup_excluded_guest_devices
from ..const import DOMAIN, CONF_PLATFORM_TYPE
from ..logic.guest_selection import (
    _entry_cluster_id, get_entry_guest_selection,
    get_effective_guest_selections, selection_guest_ids,
)
from ..logic.guest_identity import replication_job_unique_id, replication_summary_unique_id
from ..logic.cluster_scope import associated_cluster_for_pve, associated_pves_for_cluster
from .cluster import ProxmoxClusterBaseSensor

_LOGGER = logging.getLogger(__name__)


SENSOR_TYPES = {
    "last_sync": SensorDeviceClass.TIMESTAMP,
    "next_sync": SensorDeviceClass.TIMESTAMP,
    "duration": SensorDeviceClass.DURATION,
}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value):
    """Require Unix seconds in [2000, 2100), excluding relative/sentinel values."""
    number = _number(value)
    if number is None or not 946684800 <= number < 4102444800:
        return None
    return datetime.fromtimestamp(number, timezone.utc)


def _job_status(job):
    if not job.get("runtime_present"):
        return "unknown"
    failures = _number(job.get("fail_count"))
    if "error" in job or (failures is not None and failures > 0):
        return "error"
    return "ok" if failures == 0 else "unknown"


def _aggregate(jobs):
    states = [_job_status(job) for job in jobs]
    failed = states.count("error")
    return ("error" if failed else "unknown" if "unknown" in states else "ok"), failed


def _jobs(data):
    return data.get("cluster_replication_jobs", {})


def _vmtype(job, data):
    if job.get("vmtype") in ("qemu", "lxc"):
        return job["vmtype"]
    for resource in data.get("cluster_resources", []):
        if (str(resource.get("vmid")) == str(job.get("guest"))
                and resource.get("type") in ("qemu", "lxc")):
            return resource["type"]
    return None


def _job_details(job, data):
    details = dict(job)
    if "vmtype" not in details and (vmtype := _vmtype(job, data)):
        details["vmtype"] = vmtype
    return details


def _signal(scope):
    return f"{DOMAIN}_replication_updated_{scope}"


class GuestReplicationMixin:
    """Refresh guest attributes from CLUSTER without changing guest ownership."""

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        scope = (self._identity_context.scope if self._identity_context
                 and self._identity_context.use_scoped_identity else self._cluster_id)
        if scope:
            self.async_on_remove(async_dispatcher_connect(
                self.hass, _signal(scope), self.async_write_ha_state
            ))

    def _replication_attributes(self):
        if not self._cluster_id and not (
            self._identity_context and self._identity_context.use_scoped_identity
        ):
            return {}
        guest = getattr(self, "_vm_id", getattr(self, "_ct_id", None))
        expected_type = "qemu" if hasattr(self, "_vm_id") else "lxc"
        owner = self.coordinator.config_entry
        entries = self.hass.config_entries.async_entries(DOMAIN)
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_PLATFORM_TYPE) != "CLUSTER":
                continue
            if self._identity_context and self._identity_context.use_scoped_identity:
                if associated_cluster_for_pve(owner, entries) is not entry:
                    continue
            elif str(entry.data.get("cluster_name", "")).lower() != self._cluster_id:
                continue
            stored = self.hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            coordinator = stored.get("coordinator")
            if coordinator is None:
                continue
            data = coordinator.data or {}
            jobs = [_job_details(job, data) for job in _jobs(data).values()
                    if str(job.get("guest")) == str(guest)
                    and _vmtype(job, data) in (None, expected_type)]
            if not jobs:
                return {}
            if not coordinator.last_update_success:
                jobs = [{**job, "runtime_fresh": False, "inventory_fresh": False}
                        for job in jobs]
            status, failed = _aggregate(jobs)
            return {
                "replication_status": status,
                "replication_jobs": len(jobs),
                "replication_failed_jobs": failed,
                "replication": jobs,
            }
        return {}


class _ExistingDeviceReplicationSensor(ProxmoxClusterBaseSensor):
    """Attach a resolved device directly, without registering it under CLUSTER."""

    @property
    def device_info(self):
        return None


class ProxmoxReplicationSummary(_ExistingDeviceReplicationSensor):
    """Inventory count or count of known job failures for the cluster."""

    def __init__(self, coordinator, entry_id, scope, kind, device, identity_context=None):
        super().__init__(coordinator, entry_id, "")
        self.device_entry = device
        self._identity_context = identity_context
        self._kind = kind
        self._inventory_seen = False
        self._attr_unique_id = (
            replication_summary_unique_id(identity_context.scope, kind)
            if identity_context and identity_context.use_scoped_identity
            else f"pve_cluster_{scope}_replication_{kind}"
        )
        self._attr_translation_key = f"replication_{kind}"
        if kind == "jobs":
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        data = self.coordinator.data or {}
        jobs = list(_jobs(data).values())
        self._inventory_seen |= bool(data.get("cluster_replication_ok"))
        if not jobs and not self._inventory_seen:
            return None
        if self._kind == "jobs":
            return len(data.get("cluster_replication", []))
        status, failed = _aggregate(jobs)
        return failed if failed else None if status == "unknown" else "ok"

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        jobs = [_job_details(job, data) for job in _jobs(data).values()]
        return {
            "jobs" if self._kind == "jobs" else "failed_jobs": [
                job for job in jobs
                if self._kind == "jobs" or _job_status(job) == "error"
            ],
            "inventory_fresh": bool(data.get("cluster_replication_ok")),
            "runtime_fresh": all(job.get("runtime_fresh", False) for job in jobs),
            "runtime_unknown_jobs": sum(_job_status(job) == "unknown" for job in jobs),
        }


class ProxmoxReplicationSensor(_ExistingDeviceReplicationSensor):
    """A measurement retaining its original job ID on the existing guest device."""

    def __init__(self, coordinator, entry_id, scope, job_id, kind, device, identity_context=None):
        super().__init__(coordinator, entry_id, "")
        self._job_id = job_id
        self._kind = kind
        self.device_entry = device
        self._identity_context = identity_context
        device_class = SENSOR_TYPES[kind]
        self._attr_unique_id = (
            replication_job_unique_id(identity_context.scope, job_id, kind)
            if identity_context and identity_context.use_scoped_identity
            else f"pve_cluster_{scope}_replication_{job_id}_{kind}"
        )
        self._attr_translation_key = f"replication_{kind}"
        self._attr_translation_placeholders = {"job_id": job_id}
        self._attr_device_class = device_class
        if kind == "duration":
            self._attr_native_unit_of_measurement = UnitOfTime.SECONDS
        if kind == "duration":
            self._attr_state_class = SensorStateClass.MEASUREMENT

    def _job(self):
        return _jobs(self.coordinator.data or {}).get(self._job_id, {})

    @property
    def native_value(self):
        job = self._job()
        if not job.get("runtime_present"):
            return None
        if self._kind in ("last_sync", "next_sync"):
            return _timestamp(job.get(self._kind))
        duration = _number(job.get("duration"))
        return duration if duration is not None and duration >= 0 else None

    @property
    def extra_state_attributes(self):
        return dict(self._job())


def _replication_guest_selections(hass, scope, cluster_entry=None, identity_context=None):
    """Resolve current PVE selections; None per type cannot authorize deletion."""
    unknown = {"qemu": None, "lxc": None}
    if identity_context and identity_context.use_scoped_identity:
        members = associated_pves_for_cluster(cluster_entry, hass.config_entries.async_entries(DOMAIN))
    else:
        members = []
        for candidate in hass.config_entries.async_entries(DOMAIN):
            if candidate.data.get(CONF_PLATFORM_TYPE) != "PVE":
                continue
            cluster = _entry_cluster_id(hass, candidate)
            if cluster is None:
                return unknown
            if cluster == scope:
                members.append(candidate)
    if not members:
        return unknown
    try:
        for member in members:
            for key in ("selected_vms", "selected_cts"):
                selection_guest_ids(get_entry_guest_selection(member, key))
        vms, cts = get_effective_guest_selections(hass, members[0], scope)
        result = {"qemu": selection_guest_ids(vms), "lxc": selection_guest_ids(cts)}
        # Missing configuration must remain 'all', including on sibling entries.
        for vmtype, key in (("qemu", "selected_vms"), ("lxc", "selected_cts")):
            if any(get_entry_guest_selection(member, key) is None for member in members):
                result[vmtype] = None
        return result
    except (ValueError, TypeError):
        return unknown


def setup_replication_sensors(hass, coordinator, entry, async_add_entities, identity_context=None):
    """Reconcile only this cluster's replication family, preserving measure IDs."""
    cluster_name = entry.data.get("cluster_name")
    if not isinstance(cluster_name, str) or not cluster_name.strip():
        return
    scope = (identity_context.scope if identity_context and identity_context.use_scoped_identity
             else cluster_name.lower())
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    prefix = f"pve_cluster_{scope}_replication_"
    old_pattern = re.compile(re.escape(prefix) + r"(\d+-\d+)_(status|last_sync|next_sync|duration)$")
    known_jobs = set()
    known_summaries = set()
    job_instances = {}
    deleting_jobs = {}
    measure_pattern = re.compile(
        re.escape(prefix) + r"([0-9]+-[0-9]+)_(last_sync|next_sync|duration)$"
    )

    async def delete_excluded_job(instances, rows):
        for instance in instances:
            if getattr(instance, "hass", None) is not None:
                await instance.async_remove(force_remove=True)
        for row in rows:
            current = registry.async_get(row.entity_id)
            if (current is not None and current.unique_id == row.unique_id
                    and current.config_entry_id == entry.entry_id):
                registry.async_remove(row.entity_id)
        cleanup_excluded_guest_devices(hass)

    duplicate_pattern = re.compile(
        r"proxmox_(?:vm|ct)_cluster_" + re.escape(scope) + r"_\d+_v1$"
    )

    def duplicate_devices():
        # Only the guest identifier accidentally registered under THIS CLUSTER
        # entry, never the real cluster identifier or a PVE-owned guest device.
        return [
            device for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
            if device.config_entry_id == entry.entry_id
            and not device.connections and len(device.identifiers) == 1
            and any(domain == DOMAIN and duplicate_pattern.fullmatch(identifier)
                    for domain, identifier in device.identifiers)
        ]

    def guest_device(kind, guest):
        # The main status entity is the authority for guest ownership, including
        # after migration. Neither source nor CLUSTER owns this device.
        status_uid = (
            f"pve_cluster_{scope}_proxmox_{kind}_{scope}_{guest}_status_v1"
        ).lower().replace(" ", "_")
        status_id = registry.async_get_entity_id("sensor", DOMAIN, status_uid)
        status = registry.async_get(status_id) if status_id else None
        if status is None or not status.config_entry_id:
            return None
        owner = hass.config_entries.async_get_entry(status.config_entry_id)
        if owner is None or owner.data.get(CONF_PLATFORM_TYPE) != "PVE":
            return None
        if (identity_context and identity_context.use_scoped_identity
                and owner not in associated_pves_for_cluster(entry, hass.config_entries.async_entries(DOMAIN))):
            return None
        identifier = (DOMAIN, f"proxmox_{kind}_cluster_{scope}_{guest}_v1")
        device = devices.async_get_device_by_identifier(
            identifier, config_entry_id=owner.entry_id
        )
        return device if device and device.id == status.device_id else None

    @callback
    def discover():
        data = coordinator.data or {}
        jobs = _jobs(data)
        selections = _replication_guest_selections(hass, scope, entry, identity_context)
        excluded_jobs = set()
        for job_id, job in jobs.items():
            selected = selections.get(_vmtype(job, data))
            guest = job.get("guest")
            if (guest is not None and str(guest).isdecimal()
                    and selected is not None and str(guest) not in selected):
                excluded_jobs.add(job_id)
        for job_id, task in list(deleting_jobs.items()):
            if task.done() and not task.cancelled() and task.exception() is None:
                del deleting_jobs[job_id]
        # Only fresh inventory authorizes the job-to-guest association for deletion.
        if data.get("cluster_replication_ok") is True:
            rows_by_job = {}
            for row in er.async_entries_for_config_entry(registry, entry.entry_id):
                if row.domain != "sensor" or row.platform != DOMAIN:
                    continue
                match = measure_pattern.fullmatch(row.unique_id or "")
                if match and match[1] in excluded_jobs:
                    rows_by_job.setdefault(match[1], []).append(row)
            for job_id in excluded_jobs:
                if job_id in deleting_jobs:
                    continue
                instances = job_instances.pop(job_id, [])
                rows = rows_by_job.get(job_id, [])
                known_jobs.discard(job_id)
                if instances or rows:
                    deleting_jobs[job_id] = hass.async_create_task(
                        delete_excluded_job(instances, rows)
                    )
        duplicates = duplicate_devices()
        duplicate_ids = {device.id for device in duplicates}
        cluster_device = devices.async_get_device_by_identifier(
            (DOMAIN, f"proxmox_cluster_{entry.entry_id}"),
            config_entry_id=entry.entry_id,
        )
        # Retire old status entities even if inventory is temporarily unavailable.
        # Confirmed inventory absence is required to remove old measurements.
        for entity in list(er.async_entries_for_config_entry(registry, entry.entry_id)):
            if entity.domain != "sensor" or entity.platform != DOMAIN:
                continue
            match = old_pattern.fullmatch(entity.unique_id or "")
            if not match:
                continue
            job_id, kind = match.groups()
            if kind in SENSOR_TYPES and job_id in deleting_jobs:
                continue
            if kind == "status" or (data.get("cluster_replication_ok") and job_id not in jobs):
                registry.async_remove(entity.entity_id)
                known_jobs.discard(job_id)
            elif entity.device_id:
                old_device = devices.async_get(entity.device_id)
                if old_device and (
                    old_device.id in duplicate_ids
                    or (cluster_device and old_device.id == cluster_device.id)
                ):
                    # Preserve the registry row/history while guest discovery is
                    # pending, but no longer display the old measure on CLUSTER.
                    registry.async_update_entity(entity.entity_id, device_id=None)

        entities = []
        if cluster_device is not None:
            for kind in ("jobs", "status"):
                uid = f"{prefix}{kind}"
                entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid)
                registered = registry.async_get(entity_id) if entity_id else None
                if registered and registered.config_entry_id != entry.entry_id:
                    continue
                if registered and registered.device_id != cluster_device.id:
                    registry.async_update_entity(entity_id, device_id=cluster_device.id)
                if kind not in known_summaries:
                    entities.append(ProxmoxReplicationSummary(
                        coordinator, entry.entry_id, scope, kind, cluster_device, identity_context
                    ))
                    known_summaries.add(kind)
        for job_id, job in jobs.items():
            if job_id in excluded_jobs or job_id in deleting_jobs:
                continue
            vmtype = _vmtype(job, data)
            guest = job.get("guest")
            if vmtype is None or guest is None:
                continue
            kind = "vm" if vmtype == "qemu" else "ct"
            device = guest_device(kind, guest)
            if device is None:
                # A guest may be registered after CLUSTER, or be unselected.
                continue
            for measure in SENSOR_TYPES:
                uid = f"{prefix}{job_id}_{measure}"
                entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid)
                registered = registry.async_get(entity_id) if entity_id else None
                if registered is not None and registered.config_entry_id != entry.entry_id:
                    continue
                if registered is not None and registered.device_id != device.id:
                    registry.async_update_entity(entity_id, device_id=device.id)
                if job_id not in known_jobs:
                    instance = ProxmoxReplicationSensor(
                        coordinator, entry.entry_id, scope, job_id, measure, device, identity_context
                    )
                    entities.append(instance)
                    job_instances.setdefault(job_id, []).append(instance)
            known_jobs.add(job_id)
        if entities:
            async_add_entities(entities)
        for device in duplicates:
            if er.async_entries_for_device(
                registry, device.id, include_disabled_entities=True
            ):
                continue
            if dr.async_entries_for_parent_device(devices, device.id):
                continue
            # Do not remove a device referenced as a parent by another family.
            if any(other.via_device_id == device.id
                   for other in devices.async_get_devices()):
                continue
            devices.async_remove_device(device.id)
        cleanup_excluded_guest_devices(hass)
        async_dispatcher_send(hass, _signal(scope))

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))
