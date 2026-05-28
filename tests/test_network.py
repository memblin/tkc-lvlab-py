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
    generate_mac,
    get_network_info,
    resolve_network_settings,
    validate_static_ip,
)
from tkc_lvlab.utils.virsh import VirshError

import re

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

# Dual-stack NAT network: v4 + v6 ``<ip>`` elements, each with its own
# ``<dhcp>`` range. libvirt uses ``family='ipv6'`` and ``prefix='N'``
# (no netmask attribute) for the v6 ``<ip>`` element; v4 omits the family
# attribute and uses ``netmask``. This is the realistic shape we expect
# from a libvirt 8.x+ network defined for dual-stack lab use (#137).
DUAL_STACK_NAT_XML = """\
<network>
  <name>default-dualstack</name>
  <forward mode='nat'>
    <nat>
      <port start='1024' end='65535'/>
    </nat>
  </forward>
  <bridge name='virbr1' stp='on' delay='0'/>
  <mac address='52:54:00:dd:ee:ff'/>
  <ip address='192.168.130.1' netmask='255.255.255.0'>
    <dhcp>
      <range start='192.168.130.2' end='192.168.130.254'/>
    </dhcp>
  </ip>
  <ip family='ipv6' address='2001:db8:130::1' prefix='64'>
    <dhcp>
      <range start='2001:db8:130::100' end='2001:db8:130::1ff'/>
    </dhcp>
  </ip>
</network>
"""

# v6-only network (NAT). Same shape but no v4 ``<ip>`` element. Used to
# confirm v4 fields stay ``None`` when only v6 is configured.
V6_ONLY_NAT_XML = """\
<network>
  <name>v6only</name>
  <forward mode='nat'/>
  <bridge name='virbr-v6'/>
  <ip family='ipv6' address='fd00:cafe::1' prefix='64'/>
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
# get_network_info — IPv6 parsing (#137 dual-stack)
# ---------------------------------------------------------------------------


def test_get_network_info_parses_dual_stack_nat() -> None:
    """A dual-stack NAT network parses BOTH v4 and v6 fields independently.

    libvirt emits two ``<ip>`` elements — one with no ``family`` (v4,
    ``netmask``) and one with ``family='ipv6'`` (``prefix``). Each may
    carry its own ``<dhcp>`` range. The parser must not let v6 overwrite
    v4 fields or vice versa.
    """
    with mock.patch.object(
        net_mod, "run_virsh", return_value=_fake_completed(DUAL_STACK_NAT_XML)
    ):
        info = get_network_info("qemu:///system", "default-dualstack")

    # v4 side unchanged.
    assert info.gateway_ip == "192.168.130.1"
    assert info.netmask == "255.255.255.0"
    assert info.dhcp_start == "192.168.130.2"
    assert info.dhcp_end == "192.168.130.254"
    # v6 side populated.
    assert info.gateway_ip6 == "2001:db8:130::1"
    assert info.prefix6 == 64
    assert info.dhcp6_start == "2001:db8:130::100"
    assert info.dhcp6_end == "2001:db8:130::1ff"


def test_get_network_info_parses_v6_only_network() -> None:
    """A v6-only network leaves v4 fields ``None`` but populates v6 fields.

    Even though lvlab's #137 first-cut scope is dual-stack only, the
    parser layer must not crash on v6-only networks (operators may use
    them with non-lvlab tooling on the same hypervisor).
    """
    with mock.patch.object(
        net_mod, "run_virsh", return_value=_fake_completed(V6_ONLY_NAT_XML)
    ):
        info = get_network_info("qemu:///system", "v6only")

    assert info.gateway_ip is None
    assert info.netmask is None
    assert info.gateway_ip6 == "fd00:cafe::1"
    assert info.prefix6 == 64
    assert info.dhcp6_start is None
    assert info.dhcp6_end is None


def test_get_network_info_v4_only_leaves_v6_fields_none() -> None:
    """A v4-only network (the existing default) parses with v6 fields ``None``.

    Regression guard for the dual-stack work: extending
    ``LibvirtNetworkInfo`` with v6 fields must not change behavior for
    callers that only see v4 networks today.
    """
    with mock.patch.object(
        net_mod, "run_virsh", return_value=_fake_completed(NAT_NETWORK_XML)
    ):
        info = get_network_info("qemu:///system", "default")

    assert info.gateway_ip == "192.168.122.1"  # v4 still parses
    assert info.gateway_ip6 is None
    assert info.prefix6 is None
    assert info.dhcp6_start is None
    assert info.dhcp6_end is None


# ---------------------------------------------------------------------------
# LibvirtNetworkInfo.subnet6
# ---------------------------------------------------------------------------


def test_subnet6_inferred_from_gateway_and_prefix() -> None:
    """subnet6 is an IPv6Network derived from gateway_ip6 + prefix6, strict=False."""
    info = LibvirtNetworkInfo(
        name="default-dualstack",
        forward_mode="nat",
        gateway_ip="192.168.130.1",
        netmask="255.255.255.0",
        dhcp_start=None,
        dhcp_end=None,
        gateway_ip6="2001:db8:130::1",
        prefix6=64,
        dhcp6_start=None,
        dhcp6_end=None,
    )
    assert info.subnet6 == ipaddress.IPv6Network("2001:db8:130::/64")


def test_subnet6_is_none_when_v6_gateway_missing() -> None:
    """Without a v6 gateway, subnet6 returns None (mirrors subnet's behavior)."""
    info = LibvirtNetworkInfo(
        name="v4only",
        forward_mode="nat",
        gateway_ip="192.168.122.1",
        netmask="255.255.255.0",
        dhcp_start=None,
        dhcp_end=None,
        gateway_ip6=None,
        prefix6=None,
        dhcp6_start=None,
        dhcp6_end=None,
    )
    assert info.subnet6 is None


def test_subnet6_is_none_when_prefix_missing() -> None:
    """A v6 gateway without a prefix can't infer a subnet — return None."""
    info = LibvirtNetworkInfo(
        name="weird",
        forward_mode="nat",
        gateway_ip=None,
        netmask=None,
        dhcp_start=None,
        dhcp_end=None,
        gateway_ip6="2001:db8::1",
        prefix6=None,
        dhcp6_start=None,
        dhcp6_end=None,
    )
    assert info.subnet6 is None


# ---------------------------------------------------------------------------
# validate_static_ip — IPv6 routing (#137)
# ---------------------------------------------------------------------------


def _dualstack_default() -> LibvirtNetworkInfo:
    """Build a NAT dual-stack info object reused across v6 validation tests."""
    return LibvirtNetworkInfo(
        name="default-dualstack",
        forward_mode="nat",
        gateway_ip="192.168.130.1",
        netmask="255.255.255.0",
        dhcp_start="192.168.130.2",
        dhcp_end="192.168.130.254",
        gateway_ip6="2001:db8:130::1",
        prefix6=64,
        dhcp6_start="2001:db8:130::100",
        dhcp6_end="2001:db8:130::1ff",
    )


def test_validate_static_ip_accepts_v6_in_subnet_outside_dhcp() -> None:
    """A v6 address inside the v6 subnet but outside the v6 DHCP range is accepted."""
    info = _dualstack_default()
    # ::1 is the gateway — outside DHCP range, inside the /64.
    validate_static_ip("2001:db8:130::1", info)  # Must not raise.
    # ::2 is below the dhcp6 range start (::100).
    validate_static_ip("2001:db8:130::2", info)  # Must not raise.


def test_validate_static_ip_rejects_v6_inside_dhcp6_range() -> None:
    """A v6 static IP inside the v6 DHCP range races libvirt's dnsmasq6 — rejected."""
    info = _dualstack_default()
    with pytest.raises(ValueError, match="DHCP range"):
        validate_static_ip("2001:db8:130::100", info)  # boundary
    with pytest.raises(ValueError, match="DHCP range"):
        validate_static_ip("2001:db8:130::150", info)  # mid
    with pytest.raises(ValueError, match="DHCP range"):
        validate_static_ip("2001:db8:130::1ff", info)  # boundary


def test_validate_static_ip_rejects_v6_outside_subnet() -> None:
    """A v6 address outside the network's /64 is rejected, ignoring v4 fields entirely."""
    info = _dualstack_default()
    with pytest.raises(ValueError, match="not in subnet"):
        validate_static_ip("2001:db8:999::5", info)


def test_validate_static_ip_v4_still_uses_v4_subnet_on_dual_stack() -> None:
    """A v4 address validated against a dual-stack network checks the v4 side only.

    Regression guard: the dual-stack extension must not let v6 fields
    interfere with v4 validation. A v4 address far from the v6 subnet
    must still validate against the v4 subnet.
    """
    info = _dualstack_default()
    validate_static_ip("192.168.130.1", info)  # v4 gateway, must not raise
    with pytest.raises(ValueError, match="DHCP range"):
        validate_static_ip("192.168.130.50", info)  # in v4 dhcp range


def test_validate_static_ip_v6_with_cidr_suffix() -> None:
    """A v6 address with a /prefix suffix still validates — strip the CIDR first."""
    info = _dualstack_default()
    validate_static_ip("2001:db8:130::1/64", info)  # Must not raise.


def test_validate_static_ip_v6_skips_checks_when_v6_subnet_unknown() -> None:
    """A v6 address validated against a v4-only network skips checks (no v6 subnet)."""
    info = _nat_default()  # v4-only fixture
    # Operator picked a v6 address; we have no v6 info — accept it.
    validate_static_ip("2001:db8::5", info)  # Must not raise.


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


# ---------------------------------------------------------------------------
# generate_mac
# ---------------------------------------------------------------------------


def test_generate_mac_uses_qemu_oui_and_valid_format() -> None:
    """The MAC sits in QEMU's 52:54:00 OUI and is well-formed lowercase hex.

    virt-install and netplan both accept a colon-separated 6-octet MAC;
    using QEMU's own OUI keeps lvlab-pinned MACs indistinguishable from
    libvirt's auto-assigned ones.
    """
    mac = generate_mac()
    assert mac.startswith("52:54:00:")
    assert re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac), mac


def test_generate_mac_varies_across_calls() -> None:
    """Successive MACs differ — a fixed MAC would collide when two VMs
    share a network. Not cryptographic; just guards against a constant."""
    macs = {generate_mac() for _ in range(20)}
    assert len(macs) > 1
