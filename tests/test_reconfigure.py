"""The PVE reconfigure step (cmcore fork, 2026-10-22): a node's proxy name changed
(proxmox.cmcore.net -> proxmox720.cmcore.net) and the entry must follow it without being
removed and added again, which would orphan every entity and its history."""

from unittest.mock import patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.proxmox_sensors.api import AuthenticationError, CannotConnect
from custom_components.proxmox_sensors.const import DOMAIN

OLD = "proxmox.cmcore.net:443"
NEW = "proxmox720.cmcore.net:443"
DATA = {
    "platform_type": "PVE",
    "host": OLD,
    "node": "dper720xd",
    "server_id": "dper720xd",
    "user": "hmasst@pve",
    "token_id": "hmasst",
    "token_secret": "not-a-real-secret",
    "verify_ssl": True,
    "auto_detect_node": True,
    "selected_vms": ["222", "338"],
    "selected_cts": ["1463"],
    "selected_storage": ["local"],
    "limited_permissions": False,
}


class FakeClient:
    """Stands in for ProxmoxClient; `answers` maps an API path to a reply or an exception."""

    made: list["FakeClient"] = []
    answers: dict = {}

    def __init__(self, host, user, password=None, token_id=None, token_secret=None,
                 server_type="PVE", port=None, verify_ssl=True):
        self.host = host
        self.token_id = token_id
        self.verify_ssl = verify_ssl
        self.calls: list[str] = []
        FakeClient.made.append(self)

    async def get(self, hass, path, raise_errors=False):
        self.calls.append(path)
        reply = FakeClient.answers.get(path)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def fake_client():
    FakeClient.made = []
    FakeClient.answers = {"nodes": [{"node": "dper720xd", "status": "online"}], "cluster/resources": []}
    with patch("custom_components.proxmox_sensors.config_flow.ProxmoxClient", FakeClient), \
            patch("custom_components.proxmox_sensors.async_setup_entry", return_value=True), \
            patch("custom_components.proxmox_sensors.async_unload_entry", return_value=True):
        yield FakeClient


def _entry(hass, data=None, unique_id=None, title="PVE: dper720xd"):
    entry = MockConfigEntry(domain=DOMAIN, data=dict(data or DATA), unique_id=unique_id, title=title, version=3)
    entry.add_to_hass(hass)
    return entry


async def test_the_form_offers_the_current_address(hass, fake_client):
    entry = _entry(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "reconfigure"
    field = next(k for k in result["data_schema"].schema if str(k) == "host")
    assert field.description == {"suggested_value": OLD}
    assert result["description_placeholders"] == {"node": "dper720xd"}
    assert fake_client.made == []                                   # nothing is called before a submit


async def test_only_the_host_changes_and_the_new_address_is_what_was_checked(hass, fake_client):
    entry = _entry(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": f"  {NEW} "})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert dict(entry.data) == {**DATA, "host": NEW}
    assert entry.unique_id is None and entry.title == "PVE: dper720xd"
    assert [c.host for c in fake_client.made] == [NEW]
    assert fake_client.made[0].verify_ssl is True and fake_client.made[0].token_id == "hmasst"
    assert fake_client.made[0].calls == ["nodes", "cluster/resources", "nodes"]


async def test_an_address_that_reaches_another_node_is_refused(hass, fake_client):
    entry = _entry(hass)
    fake_client.answers["nodes"] = [{"node": "dellprectower"}]
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "proxmoxdpt.cmcore.net:443"})
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": "node_not_found"}
    assert dict(entry.data) == DATA


@pytest.mark.parametrize(("raised", "error"), [
    (CannotConnect("PVE HTTP 502 while requesting nodes"), "cannot_connect"),
    (AuthenticationError("401"), "invalid_auth"),
    (RuntimeError("boom"), "unknown"),
])
async def test_a_failing_address_leaves_the_entry_as_it_was(hass, fake_client, raised, error):
    entry = _entry(hass)
    fake_client.answers["nodes"] = raised
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": NEW})
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": error}
    assert dict(entry.data) == DATA


async def test_no_visible_node_is_insufficient_permissions(hass, fake_client):
    entry = _entry(hass)
    fake_client.answers["nodes"] = []
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": NEW})
    assert result["errors"] == {"base": "insufficient_permissions"} and dict(entry.data) == DATA


@pytest.mark.parametrize("bad", ["https://proxmox720.cmcore.net", "proxmox720.cmcore.net/api2", "proxmox720.cmcore.net:99999",
                                 "proxmox720.cmcore.net:", "user@proxmox720.cmcore.net", "two words"])
async def test_a_malformed_address_is_refused_before_any_call(hass, fake_client, bad):
    entry = _entry(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": bad})
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"host": "invalid_host_port"}
    assert fake_client.made == [] and dict(entry.data) == DATA


async def test_a_host_based_unique_id_follows_the_address(hass, fake_client):
    entry = _entry(hass, unique_id=f"pve:{OLD}:dper720xd")
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": NEW})
    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == f"pve:{NEW}:dper720xd" and entry.data["host"] == NEW


async def test_an_address_another_entry_already_watches_aborts(hass, fake_client):
    _entry(hass, data={**DATA, "host": NEW}, title="PVE: dper720xd (2)")
    entry = _entry(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": NEW})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert dict(entry.data) == DATA and fake_client.made == []


async def test_a_pbs_entry_is_not_reconfigured_here(hass, fake_client):
    entry = _entry(hass, data={"platform_type": "PBS", "host": "pbs.cmcore.net:443", "node": "Proxmox",
                               "server_id": "pbs_1", "user": "hmasst@pbs", "token_id": "t", "token_secret": "s"},
                   title="PBS: Proxmox")
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_not_supported"


async def test_a_new_entry_still_starts_at_the_user_step(hass, fake_client):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": NEW, "platform_type": "PVE"})
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"host": "invalid_host"}   # a NEW entry keeps 5.2.0's rule
