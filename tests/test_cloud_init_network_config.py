"""Unit tests for :class:`tkc_lvlab.utils.cloud_init.NetworkConfig`
render output — both v1 (ENI) and v2 (netplan) templates.

The v2 template uses netplan's ``match.driver: virtio_net`` +
``set-name: {{ iface.name }}`` shape so the same manifest works on
hosts whose cloud images differ in default interface naming
(predictable ``enp1s0`` on Debian / Fedora vs. unpredictable
``eth0`` on AlmaLinux 10, whose cloud image disables predictable
naming via the kernel cmdline). Regression guards here prevent
either side of that decoupling from drifting silently.
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


def test_v2_static_ip_renders_match_by_driver_and_set_name() -> None:
    """A static-IP interface must carry a match-by-driver block and a
    set-name renaming the matched NIC to the manifest's iface.name.

    Without these the netplan config pins to a kernel device name that
    may not exist on the target distro — the root cause of the
    "eth0 vs enp1s0" deploy failures across the supported host matrix.
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
    assert eth0["set-name"] == "eth0"
    assert eth0["addresses"] == ["192.168.122.50/24"]
    assert eth0["dhcp4"] is False
    assert eth0["dhcp6"] is False
    assert eth0["routes"] == [{"to": "0.0.0.0/0", "via": "192.168.122.1"}]


def test_v2_dhcp_only_still_renders_match_and_set_name() -> None:
    """DHCP-only (no ip4) interfaces also need match+set-name —
    otherwise a manifest that omitted the IP for ergonomics would
    silently regress the cross-distro decoupling.
    """
    nc = NetworkConfig(
        network_version=NetworkVersion.V2,
        interfaces=[{"name": "eth0"}],
        nameservers=_empty_nameservers(),
    )
    parsed = yaml.safe_load(nc.render_config())

    eth0 = parsed["network"]["ethernets"]["eth0"]
    assert eth0["match"] == {"driver": "virtio_net"}
    assert eth0["set-name"] == "eth0"
    assert eth0["dhcp4"] is True
    assert eth0["dhcp6"] is True
    assert "addresses" not in eth0
    assert "routes" not in eth0


def test_v2_set_name_tracks_iface_name() -> None:
    """An operator who picks a non-default iface.name (e.g. ``mgmt0``)
    gets that exact name as the in-guest device. Backward-compatibility
    for legacy manifests that used ``name: enp1s0`` rides on this too —
    the NIC still ends up named enp1s0 in the guest.
    """
    nc = NetworkConfig(
        network_version=NetworkVersion.V2,
        interfaces=[{"name": "enp1s0", "ip4": "10.0.0.5/24", "ip4gw": "10.0.0.1"}],
        nameservers=_empty_nameservers(),
    )
    parsed = yaml.safe_load(nc.render_config())

    enp = parsed["network"]["ethernets"]["enp1s0"]
    assert enp["set-name"] == "enp1s0"
    assert enp["match"] == {"driver": "virtio_net"}


def test_v2_multiple_interfaces_each_get_match_and_set_name() -> None:
    """When the manifest declares multiple interfaces, every one needs
    its own match-by-driver + set-name. Netplan won't reliably rename
    >1 device per match group, but emitting the block for each is
    still correct — the multi-NIC limitation is documented in the
    template comment, not enforced by truncating the render.
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
        assert ethernets[name]["set-name"] == name


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
