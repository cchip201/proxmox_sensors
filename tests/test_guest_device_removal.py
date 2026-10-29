"""Removing an empty guest device from a PVE entry (cmcore fork, 2026-10-28).

The 720's entry kept 17 empty guest devices ("4. VM: hmasst-(462)", "3. CT: pulse-(1785)")
of guests that moved to the other node and are no longer selected. The 720 keeps a
stopped replica of each with the same VMID on purpose, so the old rule (refused while the
VMID is on the node) could never let them go. An unselected guest's device may now be
removed; a selected guest, or one whose selection is missing or unreadable, keeps the old
rule, and every other check still holds.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.proxmox_sensors import async_remove_config_entry_device
from custom_components.proxmox_sensors.const import DOMAIN
from custom_components.proxmox_sensors.logic.guest_selection import guest_unselected
from custom_components.proxmox_sensors.logic.pve_local_identity import PveLocalIdentityContext

NODE = "dper720xd"
VM_462 = f"proxmox_vm_cluster_{NODE}_462_v1"
CT_1785 = f"proxmox_ct_cluster_{NODE}_1785_v1"
REPLICAS = [                                    # what /cluster/resources answers on the 720
    {"type": "node", "node": NODE, "status": "online"},
    {"type": "qemu", "vmid": 462, "node": NODE, "name": "hmasst", "status": "stopped"},
    {"type": "lxc", "vmid": 1785, "node": NODE, "name": "pulse", "status": "stopped"},
    {"type": "storage", "storage": "local", "node": NODE},
]
DATA = {                                        # the selection the config flow saved when the entry was made
    "platform_type": "PVE",
    "host": "proxmox720.cmcore.net:443",
    "node": NODE,
    "server_id": NODE,
    "user": "hmasst@pve",
    "token_id": "hmasst",
    "token_secret": "not-a-real-secret",
    "selected_vms": ["222", "462"],
    "selected_cts": ["1463", "1785"],
    "selected_storage": ["local"],
}
MOVED = {"selected_vms": ["222"], "selected_cts": ["1463"]}   # the options form after the guests moved


@pytest.fixture(autouse=True)
def live_platforms():
    """One loaded platform of the integration; its one entity is the node's, not the guest's."""
    node_sensor = SimpleNamespace(device_entry=None, registry_entry=None,
                                  device_info={"identifiers": {(DOMAIN, f"proxmox_node_{NODE}")}})
    with patch("custom_components.proxmox_sensors.logic.pve_devices.async_get_platforms",
               return_value=[SimpleNamespace(entities={"sensor.dper720xd_status": node_sensor})]):
        yield


def _guest_device(hass, options, *, data=None, identifier=VM_462, built=None, runtime=None, cluster=None):
    entry = MockConfigEntry(domain=DOMAIN, data=dict(data or DATA), options=options,
                            title=f"PVE: {NODE}", version=3)
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, identifier)}, name=identifier,
    )
    coordinator = SimpleNamespace(
        last_update_success=True,
        pve_local_identity_context=PveLocalIdentityContext(None, None),
        data={"cluster_resources_ok": True, "cluster_resources": list(REPLICAS),
              "vms": dict(built or {}), "cts": {}, **(cluster or {})},
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"coordinator": coordinator, "services_ready": True,
                                                        **(runtime or {})}
    return entry, device


async def _removable(hass, entry, device):
    return await async_remove_config_entry_device(hass, entry, dr.async_get(hass).async_get(device.id))


# ---- the pure rule ---------------------------------------------------------------------------------

def test_a_guest_missing_from_a_readable_selection_is_unselected():
    for selection in (["222", "338"], [222, 338], ("222",), {"222"}, ["dellprectower:222"], []):
        assert guest_unselected(selection, "462"), selection
    assert guest_unselected(["222"], 462)


def test_a_guest_in_the_selection_is_selected_whatever_the_id_type():
    for selection in (["462"], [462], ("222", 462), {"462"}, ["dper720xd:462"], ["DPER720XD:462"], ["0462"]):
        assert not guest_unselected(selection, "462"), selection


def test_a_missing_or_unreadable_selection_is_never_unselected():
    for selection in (None, "222", "222,338", 222, {"222": True}, ["vm-222"], ["222", None], [True], [2.5], [""]):
        assert not guest_unselected(selection, "462"), selection
    assert not guest_unselected(["222"], None)


# ---- through async_remove_config_entry_device ------------------------------------------------------

@pytest.mark.parametrize("selected_vms", [["222"], [222], [], ["dellprectower:222"]])
async def test_an_unselected_guest_whose_vmid_is_still_on_the_node_can_be_removed(hass, selected_vms):
    entry, device = _guest_device(hass, {**MOVED, "selected_vms": selected_vms})
    assert entry.data["selected_vms"] == ["222", "462"]        # the options form's save wins over data
    assert await _removable(hass, entry, device)


@pytest.mark.parametrize("selected_vms", [["222", "462"], [462], ["dper720xd:462"]])
async def test_a_selected_guest_whose_vmid_is_on_the_node_is_refused(hass, selected_vms):
    entry, device = _guest_device(hass, {**MOVED, "selected_vms": selected_vms})
    assert not await _removable(hass, entry, device)


@pytest.mark.parametrize("disabled_by", [None, er.RegistryEntryDisabler.USER])
async def test_an_unselected_guests_device_that_still_has_an_entity_is_refused(hass, disabled_by):
    entry, device = _guest_device(hass, MOVED)
    er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"pve_cluster_{NODE}_proxmox_vm_{NODE}_462_status_v1",
        config_entry=entry, device_id=device.id, disabled_by=disabled_by,
    )
    assert not await _removable(hass, entry, device)


@pytest.mark.parametrize(("options", "data"), [
    ({}, {k: v for k, v in DATA.items() if k != "selected_vms"}),   # missing everywhere
    ({**MOVED, "selected_vms": None}, None),                         # the legacy "every guest" marker
    ({**MOVED, "selected_vms": "222"}, None),                        # not a list
    ({**MOVED, "selected_vms": ["vm-222"]}, None),                   # an id that is not a VMID
])
async def test_a_missing_or_unreadable_selection_keeps_the_vmid_rule(hass, options, data):
    entry, device = _guest_device(hass, options, data=data)
    assert not await _removable(hass, entry, device)
    hass.data[DOMAIN][entry.entry_id]["coordinator"].data["cluster_resources"] = REPLICAS[:1]
    assert await _removable(hass, entry, device)               # the old rule: gone from the node = removable


async def test_an_unselected_guest_this_entry_still_builds_is_refused(hass):
    built = {f"{NODE}:462": {"vmid": 462, "node": NODE, "status": "stopped"}}   # e.g. a sibling's selection
    entry, device = _guest_device(hass, MOVED, built=built)
    assert not await _removable(hass, entry, device)


@pytest.mark.parametrize(("selected_cts", "removable"), [(["1463"], True), ([], True),
                                                         (["1785"], False), ([1785], False)])
async def test_a_container_follows_the_same_rule(hass, selected_cts, removable):
    entry, device = _guest_device(hass, {**MOVED, "selected_cts": selected_cts}, identifier=CT_1785)
    assert await _removable(hass, entry, device) is removable


@pytest.mark.parametrize(("runtime", "cluster", "healthy"), [
    ({"services_ready": False}, None, False),
    ({"unloading": True}, None, False),
    (None, {"cluster_resources_ok": False}, False),
    (None, {"cluster_resources": None}, False),
    (None, {"vms": None}, False),
    (None, {"cluster_resources": [*REPLICAS, "not-a-resource"]}, False),
    (None, None, True),
])
async def test_an_unselected_guest_still_needs_a_healthy_coordinator(hass, runtime, cluster, healthy):
    entry, device = _guest_device(hass, MOVED, runtime=runtime, cluster=cluster)
    assert await _removable(hass, entry, device) is healthy
    hass.data[DOMAIN][entry.entry_id]["coordinator"].last_update_success = False
    assert not await _removable(hass, entry, device)


@pytest.mark.parametrize("identifier", [f"proxmox_storage_{NODE}_local", f"proxmox_node_{NODE}"])
async def test_a_device_that_is_not_an_unselected_guest_of_ours_is_refused(hass, identifier):
    entry, device = _guest_device(hass, MOVED, identifier=identifier)
    assert not await _removable(hass, entry, device)
