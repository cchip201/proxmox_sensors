"""Changing the address of an EXISTING PVE entry (cmcore fork, 2026-10-22).

Every PVE entry made before the 5.2.0 host validation keeps its node behind a reverse
proxy as ``name:port`` (``proxmox.cmcore.net:443``), and both clients honor that port:
proxmoxer splits it off the host, and the fork's PBS patch does the same. When the proxy
name changes, the entry needs the new name without being removed and added again, which
would orphan every entity and its history. These rules are pure, so they are unit-tested
without Home Assistant.
"""

from __future__ import annotations


def split_host_port(value) -> tuple[str, int | None] | None:
    """Split ``name:443`` into ``("name", 443)``.

    A value without a port comes back as ``(value, None)``; so does a bracketed or bare
    IPv6 address, which the caller's own host validation then judges. ``None`` means the
    value is empty or carries a port that is not a plain number from 1 to 65535.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("[") or text.count(":") != 1:
        return text, None
    host, port_text = text.split(":", 1)
    if not host or not port_text.isdigit() or port_text != str(int(port_text)):
        return None
    port = int(port_text)
    if not 1 <= port <= 65535:
        return None
    return host, port


def node_is_visible(nodes, node) -> bool:
    """True when ``node`` is among the nodes a PVE ``nodes`` call answered with.

    The new address must lead to the SAME node the entry watches: a name that answers
    with valid credentials but reaches another node would move the entry's entities onto
    the wrong machine.
    """
    want = str(node or "").strip().lower()
    if not want or not isinstance(nodes, list):
        return False
    return any(
        isinstance(item, dict) and str(item.get("node") or "").strip().lower() == want
        for item in nodes
    )
