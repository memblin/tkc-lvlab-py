"""Libvirt network introspection and static-IP validation.

Phase 6 step 2 of the lvscripts port — see ``docs-extra/lvscripts-survey.md`` §5
"PORT: network validation". Wraps ``virsh net-dumpxml`` to expose a typed
view of a libvirt network (forward mode, gateway, netmask, DHCP range) and
the two policy helpers that depend on it:

- :func:`validate_static_ip` — rejects an IP outside the subnet OR inside
    the network's DHCP range. Required before a one-off ``createvm`` can
    safely accept a ``--ip4`` flag.
- :func:`resolve_network_settings` — applies the NAT-vs-bridge forward-mode
    policy: NAT networks derive DNS and gateway from the network XML;
    bridge networks require the caller to supply explicit DNS and gateway
    values (matching lvscripts' refusal to guess).

Differences from the lvscripts source:

- ``get_network_info`` takes the libvirt URI as a parameter rather than
    hardcoding ``qemu:///system``, matching lvlab's URI flexibility.
- Subprocess calls go through :func:`tkc_lvlab.utils.virsh.run_virsh` so
    they inherit the ``LC_ALL=C``/``LANG=C`` locale lock, the
    :class:`VirshError` translation, and the timeout handling. The
    boundary translation in :func:`get_network_info` re-raises every
    libvirt-level failure as :class:`LibvirtNetworkError`.

Nothing here reads ``Lvlab.yml`` or imports a CLI module — the standalone
``createvm`` and the future manifest workflow can both depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import secrets
from typing import Any
import xml.etree.ElementTree as ET

from ..exceptions import LibvirtNetworkError, VirshError
from .virsh import run_virsh

# QEMU/KVM's registered OUI. libvirt assigns guest NICs MACs from this
# prefix by default, so generating our own from the same range keeps the
# address indistinguishable from an auto-assigned one.
_QEMU_OUI = "52:54:00"


def generate_mac() -> str:
    """Generate a random virtio NIC MAC address in QEMU's ``52:54:00`` OUI.

    lvlab pins a deterministic MAC per interface so the same address can be
    written into both the ``virt-install --network`` argument and the
    cloud-init ``network-config`` (``match: macaddress``). MAC matching is
    the only NIC selector cloud-init honours across *both* its netplan
    (Debian/Ubuntu) and NetworkManager (Fedora/RHEL) renderers — matching by
    driver works on netplan but is reduced to a literal ``interface-name`` on
    the NM renderer, which then binds only when the guest NIC happens to be
    named like the netplan label.

    Returns:
        A MAC address string such as ``"52:54:00:1a:2b:3c"`` (lowercase hex).
    """
    suffix = ":".join(f"{secrets.randbelow(256):02x}" for _ in range(3))
    return f"{_QEMU_OUI}:{suffix}"


# Valid values for ``interfaces.network_type`` (manifest) and
# ``--network-type`` (createvm). ``"network"`` is the default (managed
# libvirt network); ``"user"`` and ``"passt"`` are virt-install's
# user-mode options that don't require any libvirt network state —
# useful primarily on ``qemu:///session`` where rootless libvirt cannot
# trivially manage a NAT network. Static IPs are nonsensical under
# user-mode and are rejected at the manifest/CLI boundary.
NETWORK_TYPES: tuple[str, ...] = ("network", "user", "passt")
USER_MODE_NETWORK_TYPES: frozenset[str] = frozenset({"user", "passt"})


@dataclass(frozen=True)
class LibvirtNetworkInfo:
    """Network details resolved from ``virsh net-dumpxml``.

    A dual-stack network exposes both ``gateway_ip``/``netmask`` (IPv4)
    and ``gateway_ip6``/``prefix6`` (IPv6) — libvirt emits them as two
    separate ``<ip>`` elements within the same ``<network>``. The v4 side
    uses ``netmask`` and omits the ``family`` attribute; the v6 side uses
    ``family='ipv6'`` and ``prefix``. v4-only and v6-only networks leave
    the unused family's fields ``None``.

    Attributes:
        name: The libvirt network name (passed to ``net-dumpxml``).
        forward_mode: Lowercase forward mode reported by libvirt — typically
            ``"nat"`` or ``"bridge"``. ``"none"`` is used when the
            ``<forward>`` element is absent.
        gateway_ip: First IPv4 gateway address declared in the network XML,
            or ``None`` if absent.
        netmask: First IPv4 netmask declared, or ``None`` if absent.
        dhcp_start: First IP of the v4 DHCP range, or ``None`` when no DHCP
            range is configured (typical for bridge networks).
        dhcp_end: Last IP of the v4 DHCP range, or ``None``.
        gateway_ip6: First IPv6 gateway address declared, or ``None`` when
            the network is v4-only.
        prefix6: IPv6 prefix length (integer, e.g. ``64``) declared on the
            v6 ``<ip>`` element, or ``None`` when v6 is absent.
        dhcp6_start: First IP of the IPv6 DHCP range, or ``None``.
        dhcp6_end: Last IP of the IPv6 DHCP range, or ``None``.
    """

    name: str
    forward_mode: str
    gateway_ip: str | None
    netmask: str | None
    dhcp_start: str | None
    dhcp_end: str | None
    gateway_ip6: str | None = None
    prefix6: int | None = None
    dhcp6_start: str | None = None
    dhcp6_end: str | None = None

    @property
    def subnet(self) -> ipaddress.IPv4Network | None:
        """Return the IPv4 subnet inferred from gateway + netmask.

        Returns:
            The :class:`ipaddress.IPv4Network` covering the gateway address
            with ``strict=False`` (so the gateway itself doesn't have to be
            the network address), or ``None`` if either gateway or netmask
            is missing.
        """
        if self.gateway_ip is None or self.netmask is None:
            return None
        return ipaddress.IPv4Network(f"{self.gateway_ip}/{self.netmask}", strict=False)

    @property
    def subnet6(self) -> ipaddress.IPv6Network | None:
        """Return the IPv6 subnet inferred from ``gateway_ip6`` + ``prefix6``.

        Returns:
            The :class:`ipaddress.IPv6Network` covering the v6 gateway
            address with ``strict=False``, or ``None`` if either
            ``gateway_ip6`` or ``prefix6`` is missing.
        """
        if self.gateway_ip6 is None or self.prefix6 is None:
            return None
        return ipaddress.IPv6Network(f"{self.gateway_ip6}/{self.prefix6}", strict=False)


def get_network_info(uri: str, network_name: str) -> LibvirtNetworkInfo:
    """Resolve libvirt network metadata via ``virsh net-dumpxml``.

    Args:
        uri: libvirt connection URI (e.g. ``qemu:///system``,
            ``qemu:///session``). Passed through to
            :func:`tkc_lvlab.utils.virsh.run_virsh`.
        network_name: The libvirt network name (the value normally passed
            to ``virsh net-dumpxml <name>``).

    Returns:
        A populated :class:`LibvirtNetworkInfo`. Optional fields are
        ``None`` when the underlying XML did not declare them — callers
        must check before using them (typically via
        :meth:`LibvirtNetworkInfo.subnet`).

    Raises:
        LibvirtNetworkError: ``virsh net-dumpxml`` failed (network missing,
            URI unreachable, etc.) OR the output was not valid XML. The
            original :class:`VirshError` or :class:`xml.etree.ElementTree.ParseError`
            is chained via ``__cause__``.
    """
    try:
        result = run_virsh(uri, ["net-dumpxml", network_name])
    except VirshError as exc:
        raise LibvirtNetworkError(
            f"Unable to inspect libvirt network '{network_name}': "
            f"{exc.stderr or '<no stderr>'}"
        ) from exc

    try:
        root = ET.fromstring(result.stdout)
    except ET.ParseError as exc:
        raise LibvirtNetworkError(
            f"Invalid net-dumpxml response for network '{network_name}'."
        ) from exc

    forward_element = root.find("forward")
    forward_mode = (
        forward_element.attrib.get("mode", "none")
        if forward_element is not None
        else "none"
    )

    # libvirt represents IPv4 and IPv6 as separate ``<ip>`` siblings.
    # _split_ip_elements_by_family routes each one to the right slot so
    # dual-stack networks expose both pairs of fields.
    v4, v6 = _split_ip_elements_by_family(root, network_name)

    return LibvirtNetworkInfo(
        name=network_name,
        forward_mode=forward_mode,
        gateway_ip=v4.get("gateway_ip"),
        netmask=v4.get("netmask"),
        dhcp_start=v4.get("dhcp_start"),
        dhcp_end=v4.get("dhcp_end"),
        gateway_ip6=v6.get("gateway_ip6"),
        prefix6=v6.get("prefix6"),
        dhcp6_start=v6.get("dhcp6_start"),
        dhcp6_end=v6.get("dhcp6_end"),
    )


def _split_ip_elements_by_family(
    root: ET.Element, network_name: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Walk ``<ip>`` siblings and split their attributes by address family.

    v4 elements omit ``family`` or set it to ``ipv4``; v6 carries
    ``family='ipv6'``. Each family's fields come from the FIRST matching
    element (later duplicates are ignored — libvirt allows multiple
    same-family ``<ip>`` elements for secondary addresses but the lvlab
    callers only need one of each).

    Args:
        root: The ``<network>`` element from ``virsh net-dumpxml``.
        network_name: Used in the error message when an IPv6 ``prefix``
            attribute is non-integer.

    Returns:
        Tuple ``(v4_attrs, v6_attrs)`` where each dict carries the
        family-specific keys consumed by :class:`LibvirtNetworkInfo`.
        Missing fields are simply absent from the dict (the caller uses
        ``.get(...)`` and forwards ``None`` defaults).

    Raises:
        LibvirtNetworkError: An IPv6 ``<ip>`` element declared a
            ``prefix`` attribute that wasn't an integer.
    """
    v4: dict[str, Any] = {}
    v6: dict[str, Any] = {}
    for ip_element in root.findall("ip"):
        family = ip_element.attrib.get("family", "ipv4").lower()
        dhcp_range = ip_element.find("dhcp/range")
        range_start = dhcp_range.attrib.get("start") if dhcp_range is not None else None
        range_end = dhcp_range.attrib.get("end") if dhcp_range is not None else None
        if family == "ipv6":
            if v6:
                continue
            v6["gateway_ip6"] = ip_element.attrib.get("address")
            v6["prefix6"] = _parse_ipv6_prefix(
                ip_element.attrib.get("prefix"), network_name
            )
            v6["dhcp6_start"] = range_start
            v6["dhcp6_end"] = range_end
        else:
            if v4:
                continue
            v4["gateway_ip"] = ip_element.attrib.get("address")
            v4["netmask"] = ip_element.attrib.get("netmask")
            v4["dhcp_start"] = range_start
            v4["dhcp_end"] = range_end
    return v4, v6


def _parse_ipv6_prefix(prefix_attr: str | None, network_name: str) -> int | None:
    """Coerce a libvirt ``<ip prefix='N'>`` attribute to an integer.

    Raises:
        LibvirtNetworkError: ``prefix_attr`` is present but non-integer.
    """
    if prefix_attr is None:
        return None
    try:
        return int(prefix_attr)
    except ValueError as exc:
        raise LibvirtNetworkError(
            f"Network '{network_name}' has a non-integer IPv6 "
            f"prefix '{prefix_attr}' in net-dumpxml output."
        ) from exc


def validate_static_ip(vm_ip: str, network_info: LibvirtNetworkInfo) -> None:
    """Verify a candidate static IP fits within the network's subnet and avoids the DHCP pool.

    Dual-stack aware: ``vm_ip``'s address family (v4 vs v6) selects which
    subnet and DHCP range to check against. A v4 address validates against
    :attr:`LibvirtNetworkInfo.subnet` / ``dhcp_start``/``dhcp_end``; a v6
    address validates against :attr:`LibvirtNetworkInfo.subnet6` /
    ``dhcp6_start``/``dhcp6_end``. When the relevant family's subnet is
    unknown (e.g. validating a v6 address against a v4-only network), the
    checks are skipped — same opt-out as the v4-only path that predated
    dual-stack.

    Two checks (per family):

    1. The IP must be inside the family-appropriate subnet when subnet
        information is available. (When gateway or netmask/prefix is
        missing from the network XML the subnet is ``None`` and the
        check is skipped — the operator has implicitly opted out of
        subnet validation by configuring an incomplete network.)
    1. When the network has a DHCP range declared for that family, the
        IP must NOT fall inside ``[dhcp_start, dhcp_end]`` inclusive. A
        static IP inside the DHCP range would race the libvirt DHCP
        server on every boot.

    Args:
        vm_ip: The candidate IP, either as a bare address
            (``192.168.122.50`` or ``2001:db8::5``) or with a CIDR
            suffix (``192.168.122.50/24`` or ``2001:db8::5/64``). The
            CIDR is stripped before comparison.
        network_info: A populated :class:`LibvirtNetworkInfo`.

    Raises:
        ValueError: The IP is outside the inferred subnet OR inside the
            declared DHCP range. The message names the failing constraint
            so the operator can fix the input.
    """
    ip_value = ipaddress.ip_interface(vm_ip).ip

    if ip_value.version == 6:
        subnet = network_info.subnet6
        dhcp_start_str = network_info.dhcp6_start
        dhcp_end_str = network_info.dhcp6_end
    else:
        subnet = network_info.subnet
        dhcp_start_str = network_info.dhcp_start
        dhcp_end_str = network_info.dhcp_end

    if subnet is not None and ip_value not in subnet:
        raise ValueError(
            f"IP address '{ip_value}' is not in subnet '{subnet}' "
            f"for network '{network_info.name}'."
        )

    if dhcp_start_str and dhcp_end_str:
        dhcp_start = ipaddress.ip_address(dhcp_start_str)
        dhcp_end = ipaddress.ip_address(dhcp_end_str)
        if dhcp_start <= ip_value <= dhcp_end:
            raise ValueError(
                f"IP address '{ip_value}' falls within DHCP range "
                f"'{dhcp_start_str}-{dhcp_end_str}' for "
                f"network '{network_info.name}'. Choose a static IP outside "
                "that range."
            )


def resolve_network_settings(
    network_info: LibvirtNetworkInfo,
    *,
    default_dns: list[str] | None = None,
    default_gateway: str | None = None,
    default_search: list[str] | None = None,
) -> tuple[list[str], str, list[str]]:
    """Apply the forward-mode policy and return ``(dns_servers, gateway, search_domains)``.

    Policy:

    - **NAT networks** derive DNS and gateway from the network XML itself.
        The gateway IP serves as the DNS resolver (libvirt's dnsmasq lives
        there). ``default_dns`` and ``default_gateway`` are ignored for
        NAT — overriding them would defeat the point of NAT's
        self-contained networking.
    - **Bridge networks** require the caller to supply both ``default_dns``
        and ``default_gateway``. Bridges have no inherent DNS or gateway
        — the operator picks them. Calling this without them raises
        :class:`LibvirtNetworkError` rather than silently producing a
        VM with broken networking.
    - **Any other forward mode** (``"open"``, ``"route"``, ``"private"``,
        etc.) raises :class:`LibvirtNetworkError` — supporting them
        responsibly requires their own policy work.

    ``default_search`` is passed through to the caller verbatim; both NAT
    and bridge networks honor it.

    Args:
        network_info: A populated :class:`LibvirtNetworkInfo`.
        default_dns: DNS servers to use when ``network_info.forward_mode``
            is ``"bridge"``. Ignored for NAT.
        default_gateway: Gateway IP to use when ``network_info.forward_mode``
            is ``"bridge"``. Ignored for NAT.
        default_search: Search domains to include in the result. Defaults
            to an empty list when omitted.

    Returns:
        A 3-tuple ``(dns_servers, gateway, search_domains)`` ready to
        feed into cloud-init's ``network-config``.

    Raises:
        LibvirtNetworkError: A NAT network has no gateway in its XML, OR a
            bridge network was passed without both ``default_dns`` and
            ``default_gateway``, OR the forward mode is unsupported.
    """
    mode = network_info.forward_mode.lower()
    search_domains = list(default_search or [])

    if mode == "nat":
        if network_info.gateway_ip is None:
            raise LibvirtNetworkError(
                f"Network '{network_info.name}' is NAT but has no gateway "
                "address configured."
            )
        return [network_info.gateway_ip], network_info.gateway_ip, search_domains

    if mode == "bridge":
        if not default_dns or not default_gateway:
            raise LibvirtNetworkError(
                f"Network '{network_info.name}' is a bridge. Supply explicit "
                "default_dns and default_gateway, or use a NAT network."
            )
        return list(default_dns), default_gateway, search_domains

    raise LibvirtNetworkError(
        f"Network '{network_info.name}' forward mode "
        f"'{network_info.forward_mode}' is unsupported. Use a NAT network, "
        "or a bridge network with default_dns/default_gateway provided."
    )


def resolve_network_settings6(
    network_info: LibvirtNetworkInfo,
    *,
    default_dns6: list[str] | None = None,
    default_gateway6: str | None = None,
    default_search: list[str] | None = None,
) -> tuple[list[str], str, list[str]]:
    """IPv6 sibling of :func:`resolve_network_settings`.

    Same forward-mode policy as the v4 variant, but everything is keyed
    off the v6 fields (``gateway_ip6``, ``dhcp6_*``):

    - **NAT networks** derive DNS6 and gateway6 from the v6 ``<ip>``
        element of the network XML (libvirt's optional dnsmasq6 listens
        on the v6 gateway address when configured). ``default_dns6`` /
        ``default_gateway6`` are ignored.
    - **Bridge networks** require the caller to supply both
        ``default_dns6`` and ``default_gateway6``.
    - Any other forward mode raises :class:`LibvirtNetworkError`.

    Args:
        network_info: A populated :class:`LibvirtNetworkInfo`.
        default_dns6: IPv6 DNS servers for the bridge case. Ignored for NAT.
        default_gateway6: IPv6 gateway for the bridge case. Ignored for NAT.
        default_search: Search domains, passed through verbatim. Defaults
            to an empty list when omitted.

    Returns:
        ``(dns6_servers, gateway6, search_domains)``.

    Raises:
        LibvirtNetworkError: NAT network without a v6 gateway, bridge
            without both ``default_dns6`` and ``default_gateway6``, or
            an unsupported forward mode.
    """
    mode = network_info.forward_mode.lower()
    search_domains = list(default_search or [])

    if mode == "nat":
        if network_info.gateway_ip6 is None:
            raise LibvirtNetworkError(
                f"Network '{network_info.name}' is NAT but has no IPv6 gateway "
                "address configured."
            )
        return (
            [network_info.gateway_ip6],
            network_info.gateway_ip6,
            search_domains,
        )

    if mode == "bridge":
        if not default_dns6 or not default_gateway6:
            raise LibvirtNetworkError(
                f"Network '{network_info.name}' is a bridge. Supply explicit "
                "default_dns6 and default_gateway6, or use a NAT network."
            )
        return list(default_dns6), default_gateway6, search_domains

    raise LibvirtNetworkError(
        f"Network '{network_info.name}' forward mode "
        f"'{network_info.forward_mode}' is unsupported for IPv6. Use a NAT "
        "network, or a bridge network with default_dns6/default_gateway6 "
        "provided."
    )
