"""Unit tests for :mod:`tkc_lvlab.utils.network`.

These tests use realistic ``virsh net-dumpxml`` XML fixtures so the
parser is exercised against the actual shape libvirt emits, not against
synthetic data. The :func:`get_network_info` call goes through
:func:`tkc_lvlab.utils.virsh.run_virsh`, which is patched at the
``tkc_lvlab.utils.network`` import boundary — no real ``virsh``
subprocess fires here.

Locked-in contracts:

- A NAT network with a DHCP range parses every field correctly,
    including the ``forward_mode``, gateway, netmask, and DHCP range.
- A bridge network parses with ``forward_mode == "bridge"`` and the
    DHCP range stays ``None`` (bridge networks typically have no DHCP).
- :attr:`LibvirtNetworkInfo.subnet` is ``None`` when gateway/netmask
    are absent.
- :func:`validate_static_ip` rejects out-of-subnet IPs AND in-DHCP-range
    IPs (both boundary cases tested).
- :func:`resolve_network_settings` applies the NAT-vs-bridge policy:
    NAT derives DNS/gateway from the network XML; bridge requires the
    caller to supply explicit defaults.
- Unsupported forward modes raise :class:`LibvirtNetworkError`.
- A :class:`VirshError` from ``run_virsh`` translates to
    :class:`LibvirtNetworkError`; a malformed XML response translates
    too.
"""

from __future__ import annotations

import ipaddress
import subprocess
from unittest import mock

import pytest

from tkc_lvlab.utils import network as net_mod
from tkc_lvlab.utils.network import (
    LibvirtNetworkError,
    LibvirtNetworkInfo,
    get_network_info,
    resolve_network_settings,
    validate_static_ip,
)
from tkc_lvlab.utils.virsh import VirshError


# Realistic virsh net-dumpxml output for libvirt's stock "default" NAT
# network — gateway 192.168.122.1, /24, dnsmasq DHCP range .2-.254.
NAT_NETWORK_XML = """\
<network>
  <name>default</name>
  <uuid>aabbccdd-1234-5678-9abc-def012345678</uuid>
  <forward mode='nat'>
    <nat>
      <port start='1024' end='65535'/>
    </nat>
  </forward>
  <bridge name='virbr0' stp='on' delay='0'/>
  <mac address='52:54:00:aa:bb:cc'/>
  <ip address='192.168.122.1' netmask='255.255.255.0'>
    <dhcp>
      <range start='192.168.122.2' end='192.168.122.254'/>
    </dhcp>
  </ip>
</network>
"""

# Bridge network with no DHCP (typical for a host-bridged setup).
BRIDGE_NETWORK_XML = """\
<network>
  <name>vlan10</name>
  <forward mode='bridge'/>
  <bridge name='br10'/>
</network>
"""

# Open-mode network — neither NAT nor bridge; should be rejected by the
# forward-mode policy helper.
OPEN_NETWORK_XML = """\
<network>
  <name>open-net</name>
  <forward mode='open'/>
  <bridge name='virbr-open'/>
  <ip address='10.1.0.1' netmask='255.255.0.0'/>
</network>
"""


def _fake_completed(stdout: str) -> subprocess.CompletedProcess[str]:
    """Return a CompletedProcess shaped like run_virsh's success return."""
    return subprocess.CompletedProcess(
        args=["virsh"], returncode=0, stdout=stdout, stderr=""
    )


# ---------------------------------------------------------------------------
# get_network_info — XML parsing
# ---------------------------------------------------------------------------


def test_get_network_info_parses_nat_network() -> None:
    """A NAT network's gateway, netmask, and DHCP range all parse out."""
    with mock.patch.object(
        net_mod, "run_virsh", return_value=_fake_completed(NAT_NETWORK_XML)
    ) as run_mock:
        info = get_network_info("qemu:///system", "default")

    # run_virsh is invoked with the URI and the net-dumpxml args.
    run_mock.assert_called_once_with("qemu:///system", ["net-dumpxml", "default"])

    assert info.name == "default"
    assert info.forward_mode == "nat"
    assert info.gateway_ip == "192.168.122.1"
    assert info.netmask == "255.255.255.0"
    assert info.dhcp_start == "192.168.122.2"
    assert info.dhcp_end == "192.168.122.254"


def test_get_network_info_parses_bridge_network_without_dhcp() -> None:
    """A bridge network has forward_mode='bridge' and dhcp fields stay None."""
    with mock.patch.object(
        net_mod, "run_virsh", return_value=_fake_completed(BRIDGE_NETWORK_XML)
    ):
        info = get_network_info("qemu:///system", "vlan10")

    assert info.name == "vlan10"
    assert info.forward_mode == "bridge"
    assert info.gateway_ip is None
    assert info.netmask is None
    assert info.dhcp_start is None
    assert info.dhcp_end is None


def test_get_network_info_missing_forward_element_yields_none_mode() -> None:
    """An XML with no <forward> element reports forward_mode='none' (per the dataclass docs)."""
    xml_no_forward = "<network><name>isolated</name></network>"
    with mock.patch.object(
        net_mod, "run_virsh", return_value=_fake_completed(xml_no_forward)
    ):
        info = get_network_info("qemu:///system", "isolated")

    assert info.forward_mode == "none"


def test_get_network_info_virsh_failure_translates_to_libvirt_network_error() -> None:
    """A VirshError from run_virsh becomes LibvirtNetworkError, preserving the stderr.

    The boundary translation is what callers depend on — they catch
    LibvirtNetworkError, not VirshError. A regression here would leak
    subprocess errors into command-level code.
    """
    err = VirshError(
        1,
        "error: Network not found: no network with matching name 'ghost'",
        ["net-dumpxml"],
    )
    with mock.patch.object(net_mod, "run_virsh", side_effect=err):
        with pytest.raises(LibvirtNetworkError, match="Network not found"):
            get_network_info("qemu:///system", "ghost")


def test_get_network_info_malformed_xml_translates_to_libvirt_network_error() -> None:
    """If virsh exits 0 but emits garbage, the XML parse error surfaces as our domain error."""
    with mock.patch.object(
        net_mod, "run_virsh", return_value=_fake_completed("not xml at all")
    ):
        with pytest.raises(LibvirtNetworkError, match="Invalid net-dumpxml response"):
            get_network_info("qemu:///system", "default")


# ---------------------------------------------------------------------------
# LibvirtNetworkInfo.subnet
# ---------------------------------------------------------------------------


def test_subnet_inferred_from_gateway_and_netmask() -> None:
    """subnet is a /24 IPv4Network derived from gateway+netmask, strict=False."""
    info = LibvirtNetworkInfo(
        name="default",
        forward_mode="nat",
        gateway_ip="192.168.122.1",
        netmask="255.255.255.0",
        dhcp_start=None,
        dhcp_end=None,
    )
    assert info.subnet == ipaddress.IPv4Network("192.168.122.0/24")


def test_subnet_is_none_when_gateway_missing() -> None:
    """Without a gateway, subnet can't be inferred — returns None, not an exception."""
    info = LibvirtNetworkInfo(
        name="bridge",
        forward_mode="bridge",
        gateway_ip=None,
        netmask=None,
        dhcp_start=None,
        dhcp_end=None,
    )
    assert info.subnet is None


def test_subnet_is_none_when_netmask_missing() -> None:
    """Gateway alone isn't enough — netmask is also required."""
    info = LibvirtNetworkInfo(
        name="weird",
        forward_mode="nat",
        gateway_ip="10.0.0.1",
        netmask=None,
        dhcp_start=None,
        dhcp_end=None,
    )
    assert info.subnet is None


# ---------------------------------------------------------------------------
# validate_static_ip — subnet + DHCP range checks
# ---------------------------------------------------------------------------


def _nat_default() -> LibvirtNetworkInfo:
    """Build a NAT-default-shaped info object reused across boundary tests."""
    return LibvirtNetworkInfo(
        name="default",
        forward_mode="nat",
        gateway_ip="192.168.122.1",
        netmask="255.255.255.0",
        dhcp_start="192.168.122.2",
        dhcp_end="192.168.122.254",
    )


def test_validate_static_ip_accepts_in_subnet_outside_dhcp() -> None:
    """An IP inside the subnet but outside DHCP range is the valid case.

    The libvirt default network's DHCP runs .2-.254 so anything outside
    that is fine. Picking the gateway address itself is a weird but
    technically-valid choice (operator's problem if it conflicts) —
    confirm we don't reject it.
    """
    info = _nat_default()
    # .1 is the gateway — outside the DHCP range, inside the subnet.
    validate_static_ip("192.168.122.1", info)  # Must not raise.
    # .255 is the broadcast — but inside the /24, so it passes the subnet check.
    # (We don't reject broadcast/network — operator can shoot self in foot.)


def test_validate_static_ip_rejects_inside_dhcp_range_inclusive() -> None:
    """Static IP inside the DHCP range is rejected — the libvirt DHCP server would race."""
    info = _nat_default()

    # Lower boundary of DHCP range.
    with pytest.raises(ValueError, match="falls within DHCP range"):
        validate_static_ip("192.168.122.2", info)

    # Mid-range.
    with pytest.raises(ValueError, match="falls within DHCP range"):
        validate_static_ip("192.168.122.100", info)

    # Upper boundary.
    with pytest.raises(ValueError, match="falls within DHCP range"):
        validate_static_ip("192.168.122.254", info)


def test_validate_static_ip_rejects_outside_subnet() -> None:
    """An IP outside the network's subnet is rejected, regardless of DHCP."""
    info = _nat_default()
    with pytest.raises(ValueError, match="is not in subnet"):
        validate_static_ip("10.0.0.5", info)


def test_validate_static_ip_skips_subnet_check_when_subnet_unknown() -> None:
    """If gateway/netmask are missing, subnet is None and we don't reject anything.

    This is the bridge-network case — no subnet inferred, no DHCP range
    declared, so validate_static_ip is effectively a no-op. The operator
    is on the hook for IP correctness elsewhere.
    """
    info = LibvirtNetworkInfo(
        name="vlan10",
        forward_mode="bridge",
        gateway_ip=None,
        netmask=None,
        dhcp_start=None,
        dhcp_end=None,
    )
    # Must not raise — any IP is "valid" from this function's POV.
    validate_static_ip("100.64.10.50", info)


def test_validate_static_ip_accepts_cidr_suffix() -> None:
    """A 192.168.122.50/24 input still validates — the CIDR is stripped first."""
    info = _nat_default()
    # The gateway IP doesn't conflict, doesn't fall in DHCP range — should pass even with CIDR.
    validate_static_ip("192.168.122.1/24", info)  # Must not raise.


# ---------------------------------------------------------------------------
# resolve_network_settings — forward-mode policy
# ---------------------------------------------------------------------------


def test_resolve_nat_derives_dns_and_gateway_from_xml() -> None:
    """NAT: DNS = [gateway_ip], gateway = gateway_ip. Defaults are ignored.

    Why ignored: libvirt's NAT uses dnsmasq on the gateway IP; supplying
    different DNS/gateway from outside would point VMs at unreachable
    servers (the libvirt-internal subnet isn't routable from anywhere
    else).
    """
    info = _nat_default()
    dns, gateway, search = resolve_network_settings(
        info,
        default_dns=["8.8.8.8"],  # explicitly ignored for NAT
        default_gateway="1.1.1.1",  # explicitly ignored for NAT
        default_search=["example.invalid"],
    )
    assert dns == ["192.168.122.1"]
    assert gateway == "192.168.122.1"
    assert search == ["example.invalid"]


def test_resolve_nat_raises_when_gateway_missing() -> None:
    """A NAT network with no gateway in the XML is broken; surface that loudly."""
    info = LibvirtNetworkInfo(
        name="broken-nat",
        forward_mode="nat",
        gateway_ip=None,
        netmask="255.255.255.0",
        dhcp_start=None,
        dhcp_end=None,
    )
    with pytest.raises(LibvirtNetworkError, match="no gateway address"):
        resolve_network_settings(info)


def test_resolve_bridge_requires_explicit_dns_and_gateway() -> None:
    """Bridge networks cannot guess at DNS/gateway — caller must supply both."""
    info = LibvirtNetworkInfo(
        name="vlan10",
        forward_mode="bridge",
        gateway_ip=None,
        netmask=None,
        dhcp_start=None,
        dhcp_end=None,
    )

    with pytest.raises(LibvirtNetworkError, match="bridge"):
        resolve_network_settings(info)  # neither default supplied

    with pytest.raises(LibvirtNetworkError, match="bridge"):
        resolve_network_settings(info, default_dns=["1.1.1.1"])  # gateway missing

    with pytest.raises(LibvirtNetworkError, match="bridge"):
        resolve_network_settings(info, default_gateway="100.64.10.1")  # dns missing


def test_resolve_bridge_with_explicit_defaults_returns_them() -> None:
    """When both defaults are present, bridge networks accept them."""
    info = LibvirtNetworkInfo(
        name="vlan10",
        forward_mode="bridge",
        gateway_ip=None,
        netmask=None,
        dhcp_start=None,
        dhcp_end=None,
    )
    dns, gateway, search = resolve_network_settings(
        info,
        default_dns=["100.64.10.10", "100.64.10.11"],
        default_gateway="100.64.10.1",
        default_search=["tkclabs.io"],
    )
    assert dns == ["100.64.10.10", "100.64.10.11"]
    assert gateway == "100.64.10.1"
    assert search == ["tkclabs.io"]


def test_resolve_rejects_unsupported_forward_mode() -> None:
    """Any forward mode that isn't NAT or bridge is explicitly unsupported.

    Open / route / private modes need their own policy work — silently
    treating them like NAT or bridge would produce broken networking
    that's hard to diagnose.
    """
    info = LibvirtNetworkInfo(
        name="open-net",
        forward_mode="open",
        gateway_ip="10.1.0.1",
        netmask="255.255.0.0",
        dhcp_start=None,
        dhcp_end=None,
    )
    with pytest.raises(LibvirtNetworkError, match="forward mode"):
        resolve_network_settings(info)


def test_resolve_search_domains_default_to_empty_list() -> None:
    """When default_search is omitted, the returned search list is empty (not None)."""
    info = _nat_default()
    _, _, search = resolve_network_settings(info)
    assert search == []
