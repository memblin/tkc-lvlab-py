"""Unit tests for :class:`tkc_lvlab.utils.cloud_init.NetworkConfig`
render output — both v1 (ENI) and v2 (netplan) templates.

The v2 template selects each NIC by ``match.driver: virtio_net`` and
configures it under whatever name the distro assigns (predictable
``enp1s0`` on Debian / Fedora vs. ``eth0`` on AlmaLinux 10, whose cloud
image disables predictable naming via the kernel cmdline). It
deliberately does NOT ``set-name``/rename: netplan renaming leaves the
NIC unconfigured under systemd-networkd (Debian/Ubuntu) and the guest
never gets a DHCP lease. Regression guards here keep the rename from
creeping back in and keep the match block from drifting.
"""

from __future__ import annotations

import yaml

from tkc_lvlab.utils.cloud_init import NetworkConfig, NetworkVersion


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty_nameservers() -> dict:
    """Manifest shape for "no DNS overrides — leave DHCP defaults alone"."""
    return {}


# ---------------------------------------------------------------------------
# v2 (netplan) — match-by-driver + set-name
# ---------------------------------------------------------------------------


def test_v2_static_ip_renders_match_by_driver_without_rename() -> None:
    """A static-IP interface carries a match-by-driver block and NO
    set-name.

    Renaming via netplan breaks interface bring-up under
    systemd-networkd (Debian/Ubuntu), so we match the virtio NIC and
    configure it under whatever name the distro assigned rather than
    pinning to a renamed device.
    """
    nc = NetworkConfig(
        network_version=NetworkVersion.V2,
        interfaces=[
            {"name": "eth0", "ip4": "192.168.122.50/24", "ip4gw": "192.168.122.1"}
        ],
        nameservers=_empty_nameservers(),
    )
    parsed = yaml.safe_load(nc.render_config())

    eth0 = parsed["network"]["ethernets"]["eth0"]
    assert eth0["match"] == {"driver": "virtio_net"}
    assert "set-name" not in eth0
    assert eth0["addresses"] == ["192.168.122.50/24"]
    assert eth0["dhcp4"] is False
    assert eth0["dhcp6"] is False
    assert eth0["routes"] == [{"to": "0.0.0.0/0", "via": "192.168.122.1"}]


def test_v2_dhcp_only_renders_match_without_rename() -> None:
    """DHCP-only (no ip4) interfaces also match-by-driver with no
    set-name — a manifest that omits the IP must not regress into a
    rename that breaks DHCP on Debian/Ubuntu.
    """
    nc = NetworkConfig(
        network_version=NetworkVersion.V2,
        interfaces=[{"name": "eth0"}],
        nameservers=_empty_nameservers(),
    )
    parsed = yaml.safe_load(nc.render_config())

    eth0 = parsed["network"]["ethernets"]["eth0"]
    assert eth0["match"] == {"driver": "virtio_net"}
    assert "set-name" not in eth0
    assert eth0["dhcp4"] is True
    assert eth0["dhcp6"] is True
    assert "addresses" not in eth0
    assert "routes" not in eth0


def test_v2_ethernet_key_is_iface_name_label_without_rename() -> None:
    """``iface.name`` becomes the netplan ethernet *key* (a label) but is
    NOT applied as a set-name rename.

    The in-guest device keeps its distro-assigned name; the key only
    identifies the stanza. Using a label that is clearly not a kernel
    device name (``mgmt0``) makes the distinction explicit.
    """
    nc = NetworkConfig(
        network_version=NetworkVersion.V2,
        interfaces=[{"name": "mgmt0", "ip4": "10.0.0.5/24", "ip4gw": "10.0.0.1"}],
        nameservers=_empty_nameservers(),
    )
    parsed = yaml.safe_load(nc.render_config())

    ethernets = parsed["network"]["ethernets"]
    assert set(ethernets.keys()) == {"mgmt0"}
    assert ethernets["mgmt0"]["match"] == {"driver": "virtio_net"}
    assert "set-name" not in ethernets["mgmt0"]


def test_v2_multiple_interfaces_each_get_match_without_rename() -> None:
    """Each declared interface gets its own match-by-driver block and no
    set-name. The multi-NIC limitation (driver match can't disambiguate
    >1 virtio NIC) is documented in the template comment; the render
    still emits one stanza per interface rather than truncating.
    """
    nc = NetworkConfig(
        network_version=NetworkVersion.V2,
        interfaces=[
            {"name": "eth0", "ip4": "192.168.122.10/24", "ip4gw": "192.168.122.1"},
            {"name": "eth1"},
        ],
        nameservers=_empty_nameservers(),
    )
    parsed = yaml.safe_load(nc.render_config())

    ethernets = parsed["network"]["ethernets"]
    assert set(ethernets.keys()) == {"eth0", "eth1"}
    for name in ("eth0", "eth1"):
        assert ethernets[name]["match"] == {"driver": "virtio_net"}
        assert "set-name" not in ethernets[name]


def test_v2_nameservers_render_under_interface_block() -> None:
    """Manifest-supplied DNS still lands under the ethernet block, not
    at the top level — verifies the new match/set-name additions didn't
    break the existing nameserver injection path.
    """
    nc = NetworkConfig(
        network_version=NetworkVersion.V2,
        interfaces=[
            {"name": "eth0", "ip4": "192.168.122.7/24", "ip4gw": "192.168.122.1"}
        ],
        nameservers={"search": ["lab.local"], "addresses": ["192.168.122.1"]},
    )
    parsed = yaml.safe_load(nc.render_config())

    eth0 = parsed["network"]["ethernets"]["eth0"]
    assert eth0["nameservers"]["search"] == ["lab.local"]
    assert eth0["nameservers"]["addresses"] == ["192.168.122.1"]


# ---------------------------------------------------------------------------
# v1 (ENI) — unchanged regression guard
# ---------------------------------------------------------------------------


def test_v1_static_ip_does_not_use_netplan_match_keywords() -> None:
    """The v1 template is intentionally untouched by the v2 refactor —
    ENI has no clean match-by-driver equivalent, and no supported
    distro in the matrix needs v1. Guard against accidentally
    copying the v2 changes into v1.j2 in a future edit.
    """
    nc = NetworkConfig(
        network_version=NetworkVersion.V1,
        interfaces=[
            {"name": "eth0", "ip4": "192.168.122.50/24", "ip4gw": "192.168.122.1"}
        ],
        nameservers=_empty_nameservers(),
    )
    rendered = nc.render_config()

    assert "set-name" not in rendered
    assert "virtio_net" not in rendered
    # v1 still uses the ENI physical/static shape.
    assert "type: physical" in rendered
    assert "type: static" in rendered
