"""Storage sensors for Proxmox storage pools."""

import logging

from homeassistant.helpers import device_registry as dr

from .base import ProxmoxBaseSensor
from ..const import DOMAIN
from ..logic.pve_local_identity import (
    coordinator_pve_local_identity_context,
    node_device_identifier,
    storage_device_identifier,
)

_LOGGER = logging.getLogger(__name__)


class ProxmoxStorageSensor(ProxmoxBaseSensor):
    """Main storage usage sensor."""

    def __init__(self, coordinator, storage_name, st, node=None):
        uid = f"proxmox_storage_{node}_{storage_name}_percent_v1"
        super().__init__(coordinator, storage_name, None, "%", uid, node)

        self._storage_name = storage_name
        self._attr_translation_key = "storage_usage"
        self._attr_state_class = "measurement"

        stype = (st.get("type") or "").lower()

        if stype == "pbs":
            self._attr_icon = "mdi:database-sync"
        elif stype == "nfs":
            self._attr_icon = "mdi:nas"
        elif stype in ["lvm", "lvmthin"]:
            self._attr_icon = "mdi:harddisk"
        elif stype == "dir":
            self._attr_icon = "mdi:folder"
        elif stype == "zfspool":
            self._attr_icon = "mdi:database"
        elif stype in ["ceph", "rbd"]:
            self._attr_icon = "mdi:database-outline"
        else:
            self._attr_icon = "mdi:database"

    @property
    def device_info(self):
        node_id = (self._node or "node").lower()
        context = coordinator_pve_local_identity_context(self.coordinator)
        info = {
            "identifiers": {
                (DOMAIN, storage_device_identifier(
                    context, node_id, self._storage_name
                ))
            },
            "name": f"5. Storage: {self._storage_name}",
            "manufacturer": "Proxmox",
            "model": "Storage Resource",
        }

        try:
            info["via_device_id"] = dr.async_get_device_id_by_identifier(
                self.coordinator.hass,
                (DOMAIN, node_device_identifier(context, node_id)),
                config_entry_id=self.coordinator.config_entry.entry_id,
            )
        except ValueError:
            _LOGGER.debug(
                "Parent node device %s not found in config entry %s; omitting via_device_id",
                node_id,
                self.coordinator.config_entry.entry_id,
            )
        return info

    def _get_value(self):
        storage_data = self.coordinator.data.get("storage", {}).get(
            self._storage_name, {}
        )

        used = storage_data.get("used") or 0
        total = storage_data.get("total") or 0

        if total == 0:
            return 0

        return round((used / total) * 100, 2)


class ProxmoxStorageAttributeSensor(ProxmoxBaseSensor):
    """Additional sensors for storage attributes."""

    def __init__(self, coordinator, storage_name, st, label, key, node=None):
        uid = f"proxmox_storage_{node}_{storage_name}_{key}_v1"
        unit = "GB" if key in ("used", "avail", "total") else None

        super().__init__(coordinator, storage_name, None, unit, uid, node)
        if key == "used":
            self._attr_translation_key = "storage_used"
        elif key == "avail":
            self._attr_translation_key = "storage_free"
        elif key == "total":
            self._attr_translation_key = "storage_total"
        elif key == "type":
            self._attr_translation_key = "storage_type"

        self._storage_name = storage_name
        self._key = key

        stype = (st.get("type") or "").lower()

        if key == "type":

            if stype == "pbs":
                self._attr_icon = "mdi:database-sync"
            elif stype == "nfs":
                self._attr_icon = "mdi:nas"
            elif stype in ["lvm", "lvmthin"]:
                self._attr_icon = "mdi:harddisk"
            elif stype == "dir":
                self._attr_icon = "mdi:folder"
            elif stype == "zfspool":
                self._attr_icon = "mdi:database"
            elif stype in ["ceph", "rbd"]:
                self._attr_icon = "mdi:database-outline"
            else:
                self._attr_icon = "mdi:database"

        else:
            icon_map = {
                "used": "mdi:database-arrow-up",
                "avail": "mdi:database-arrow-down",
                "total": "mdi:database",
                "path": "mdi:folder-network",
            }

            self._attr_icon = icon_map.get(key, "mdi:information-outline")

        if key in ("used", "avail", "total"):
            self._attr_device_class = "data_size"
            self._attr_state_class = "measurement"

    @property
    def device_info(self):
        node_id = (self._node or "node").lower()
        context = coordinator_pve_local_identity_context(self.coordinator)
        return {
            "identifiers": {
                (DOMAIN, storage_device_identifier(
                    context, node_id, self._storage_name
                ))
            },
            "name": f"5. Storage: {self._storage_name}",
        }

    def _get_value(self):
        storage_data = self.coordinator.data.get("storage", {}).get(
            self._storage_name, {}
        )

        value = storage_data.get(self._key)

        if value is None or value == "":
            return "Unknown" if not self.unit_of_measurement else 0

        if self._key in ("used", "avail", "total"):
            try:
                return round(float(value) / (1024**3), 2)
            except (ValueError, TypeError):
                return 0

        return str(value).capitalize() if self._key == "type" else value
