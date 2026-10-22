"""The pure rules behind the PVE reconfigure step (cmcore fork, 2026-10-22)."""

from custom_components.proxmox_sensors.logic.host_change import node_is_visible, split_host_port


def test_a_proxy_name_with_its_port_splits():
    assert split_host_port("proxmox720.cmcore.net:443") == ("proxmox720.cmcore.net", 443)
    assert split_host_port("  10.0.10.20:8006 ") == ("10.0.10.20", 8006)


def test_a_value_without_a_port_comes_back_whole():
    assert split_host_port("proxmox720.cmcore.net") == ("proxmox720.cmcore.net", None)
    assert split_host_port("[fd00::1]") == ("[fd00::1]", None)
    assert split_host_port("fd00::1") == ("fd00::1", None)


def test_a_port_that_is_not_a_plain_number_in_range_is_refused():
    for bad in ("", "   ", None, "name:", ":443", "name:0", "name:65536", "name:443x", "name:0443", "name:-1"):
        assert split_host_port(bad) is None, bad


def test_the_node_must_be_among_the_answered_nodes():
    nodes = [{"node": "dper720xd", "status": "online"}, {"node": "Other"}]
    assert node_is_visible(nodes, "dper720xd")
    assert node_is_visible(nodes, "other")
    assert not node_is_visible(nodes, "dellprectower")
    assert not node_is_visible(nodes, "")
    assert not node_is_visible(None, "dper720xd")
    assert not node_is_visible(["dper720xd"], "dper720xd")
