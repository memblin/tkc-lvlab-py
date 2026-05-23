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
import xml.etree.ElementTree as ET

from .virsh import VirshError, run_virsh


# Valid values for ``interfaces.network_type`` (manifest) and
# ``--network-type`` (createvm). ``"network"`` is the default (managed
# libvirt network); ``"user"`` and ``"passt"`` are virt-install's
# user-mode options that don't require any libvirt network state —
# useful primarily on ``qemu:///session`` where rootless libvirt cannot
# trivially manage a NAT network. Static IPs are nonsensical under
# user-mode and are rejected at the manifest/CLI boundary.
NETWORK_TYPES: tuple[str, ...] = ("network", "user", "passt")
USER_MODE_NETWORK_TYPES: frozenset[str] = frozenset({"user", "passt"})


class LibvirtNetworkError(RuntimeError):
    """Raised when libvirt network information cannot be resolved or validated.

    Wraps two error surfaces:

    - **Discovery failure** — ``virsh net-dumpxml`` failed, the output was
        not parseable XML, or the named network does not exist.
    - **Policy failure** — a bridge network was used without explicit
        gateway/DNS, or the forward mode is one we don't support.

    Both are operator-actionable errors; the message names the specific
    failure.
    """


@dataclass(frozen=True)
class LibvirtNetworkInfo:
    """Network details resolved from ``virsh net-dumpxml``.

    Attributes:
        name: The libvirt network name (passed to ``net-dumpxml``).
        forward_mode: Lowercase forward mode reported by libvirt — typically
            ``"nat"`` or ``"bridge"``. ``"none"`` is used when the
            ``<forward>`` element is absent.
        gateway_ip: First IPv4 gateway address declared in the network XML,
            or ``None`` if absent.
        netmask: First IPv4 netmask declared, or ``None`` if absent.
        dhcp_start: First IP of the DHCP range, or ``None`` when no DHCP
            range is configured (typical for bridge networks).
        dhcp_end: Last IP of the DHCP range, or ``None``.
    """

    name: str
    forward_mode: str
    gateway_ip: str | None
    netmask: str | None
    dhcp_start: str | None
    dhcp_end: str | None

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

    ip_element = root.find("ip")
    gateway_ip = ip_element.attrib.get("address") if ip_element is not None else None
    netmask = ip_element.attrib.get("netmask") if ip_element is not None else None

    dhcp_range = ip_element.find("dhcp/range") if ip_element is not None else None
    dhcp_start = dhcp_range.attrib.get("start") if dhcp_range is not None else None
    dhcp_end = dhcp_range.attrib.get("end") if dhcp_range is not None else None

    return LibvirtNetworkInfo(
        name=network_name,
        forward_mode=forward_mode,
        gateway_ip=gateway_ip,
        netmask=netmask,
        dhcp_start=dhcp_start,
        dhcp_end=dhcp_end,
    )


def validate_static_ip(vm_ip: str, network_info: LibvirtNetworkInfo) -> None:
    """Verify a candidate static IP fits within the network's subnet and avoids the DHCP pool.

    Two checks:

    1. The IP must be inside :attr:`LibvirtNetworkInfo.subnet` when subnet
        information is available. (When gateway or netmask is missing from
        the network XML the subnet is ``None`` and the check is skipped —
        the operator has implicitly opted out of subnet validation by
        configuring an incomplete network.)
    1. When the network has a DHCP range declared, the IP must NOT fall
        inside ``[dhcp_start, dhcp_end]`` inclusive. A static IP inside
        the DHCP range would race the libvirt DHCP server on every
        boot.

    Args:
        vm_ip: The candidate IP, either as a bare address (``192.168.122.50``)
            or with a CIDR suffix (``192.168.122.50/24``). The CIDR is
            stripped before comparison.
        network_info: A populated :class:`LibvirtNetworkInfo`.

    Raises:
        ValueError: The IP is outside the inferred subnet OR inside the
            declared DHCP range. The message names the failing constraint
            so the operator can fix the input.
    """
    ip_value = ipaddress.ip_interface(vm_ip).ip

    if network_info.subnet is not None and ip_value not in network_info.subnet:
        raise ValueError(
            f"IP address '{ip_value}' is not in subnet '{network_info.subnet}' "
            f"for network '{network_info.name}'."
        )

    if network_info.dhcp_start and network_info.dhcp_end:
        dhcp_start = ipaddress.ip_address(network_info.dhcp_start)
        dhcp_end = ipaddress.ip_address(network_info.dhcp_end)
        if dhcp_start <= ip_value <= dhcp_end:
            raise ValueError(
                f"IP address '{ip_value}' falls within DHCP range "
                f"'{network_info.dhcp_start}-{network_info.dhcp_end}' for "
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
