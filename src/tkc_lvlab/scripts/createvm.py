"""Standalone ``createvm`` console script — one-off VM creation.

A faithful port of the ``lvscripts-py`` reference ``createvm`` command: the
UI, colored output, positional ``VM_NAME`` / ``VM_DISTRO`` arguments, the
``--ip4`` / ``--netmask`` static-addressing flow, ``--init-cloud-images``,
``--config``, ``--version``, the DHCP-lease wait, and the completion
report all mirror the reference. Two things are intentionally adapted for
lvlab:

- **Image storage** — cloud images cache under
    ``/var/lib/libvirt/images/lvlab/cloud-images`` (shared with ``lvlab up``)
    and per-VM state lands under
    ``/var/lib/libvirt/images/lvlab/oneoff/<vm_name>/``.
- **Config source** — ``VM_DISTRO`` resolves against the built-in
    :data:`BUILTIN_IMAGES` catalog merged with the ``images:`` section of an
    ``Lvlab.yml`` in the current directory (or ``--config``). Manifest
    entries win on a name collision; ``os_variant`` and the first-boot
    username are derived from the image key and overridable per image.

The libvirt domain is the **raw** ``VM_NAME`` you pass. The script targets
``qemu:///system`` (rootless ``qemu:///session`` + user-mode networking are
a tracked follow-up, not part of this script today). cloud-init ISOs are
built in-process with :mod:`pycdlib`; the per-VM qcow2 is a standalone copy
(``cp`` + ``qemu-img resize``).

``run = app`` is kept as a backwards-compat alias for the console-script
entry point (``[project.scripts] createvm = "tkc_lvlab.scripts.createvm:run"``)
and for test imports.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

from .. import __version__
from ..config import HostConfig, NetworkDefaults, load_host_config
from ..exceptions import CloudInitError, ImageError
from ..utils.catalog import (
    BUILTIN_IMAGES,
    ImageEntry as CatalogEntry,
    build_image_entry,
    resolve_catalog,
    resolve_image_entry,
)
from ..utils.cloud_init import CloudInitIso, NetworkConfig
from ..utils.images import CloudImage
from ..utils.network import (
    LibvirtNetworkError,
    LibvirtNetworkInfo,
    generate_mac,
    get_network_info,
    resolve_network_settings,
    resolve_network_settings6,
    validate_static_ip,
)
from ..utils.osinfo import OsInfoLookupError, resolve_os_variant
from ..utils.output import (
    render_one_time_password,
    render_ssh_hint,
    secho,
    set_no_color,
)
from ..utils.passwords import (
    PasswordHashError,
    generate_password_phrase,
    hash_password_sha512,
)
from ..utils.requirements import DependencyError, check_createvm_tooling
from ..utils.ssh_keys import (
    PublicKeyError,
    dedupe_public_keys,
    discover_default_public_keys,
    load_public_key,
)
from ..utils.standalone_cloud_init import (
    OneoffCloudInit,
    render_user_data_override,
    user_data_supplies_keys,
)
from ..utils.subprocess_env import system_first_env
from ..utils.virsh import VirshError, run_virsh, vm_exists

# ---------------------------------------------------------------------------
# Defaults (mirror the lvscripts reference)
# ---------------------------------------------------------------------------

_SYSTEM_URI = "qemu:///system"
DEFAULT_NETWORK = "default"
DEFAULT_NETMASK = "24"
# Default IPv6 prefix length appended to a bare ``--ip6`` address that
# lacks a ``/CIDR`` suffix. /64 is the universal lab-network default —
# the same convention libvirt's dual-stack examples use (#137).
DEFAULT_PREFIX6 = "64"
# Values for ``--ip4`` and ``--ip6`` that mean "no static IP — use DHCP",
# equivalent to omitting the flag. None of these are valid IP addresses,
# so there's no ambiguity with a real address (issue #105).
_DHCP_SENTINELS = frozenset({"dhcp", "default", "auto"})
DEFAULT_DISK_SIZE = "35G"
DEFAULT_CPU = "2"
DEFAULT_MEMORY = "2048"
NAT_DHCP_LEASE_WAIT_SECONDS = 20

# Shared with ``lvlab up`` — CloudImage appends ``/cloud-images``.
_CLOUD_IMAGE_BASEDIR = Path("/var/lib/libvirt/images/lvlab")
# Per-VM state, namespaced beside the shared cache. Distinct from
# ``lvlab up``'s ``lvlab/<env>/<vm>/`` layout so one-off VMs never collide
# with manifest VM disks.
_ONEOFF_STORAGE_ROOT = Path("/var/lib/libvirt/images/lvlab/oneoff")


# ---------------------------------------------------------------------------
# Built-in cloud-image catalog
# ---------------------------------------------------------------------------


# ``BUILTIN_IMAGES`` and the catalog resolvers (``resolve_catalog`` /
# ``resolve_image_entry``) now live in ``utils/catalog.py`` so ``lvlab``
# (``cli.py``) can share them without importing from ``scripts/``. They are
# re-exported above for backwards-compatible imports
# (``from tkc_lvlab.scripts.createvm import BUILTIN_IMAGES``).


# Config resolution (images + per-network defaults) is shared with ``lvlab``
# via ``config.load_host_config``: it layers ``/etc/Lvlab.yml`` (host-wide) <
# ``~/.Lvlab.yml`` (per-user) < ``./Lvlab.yml`` (CWD) < an explicit ``--config``
# path and returns a :class:`tkc_lvlab.config.HostConfig`. ``createvm`` reads
# its merged ``images`` (-> :func:`resolve_catalog`), ``networks`` per-bridge
# defaults, and ``default_network`` (#138).


# ---------------------------------------------------------------------------
# Storage path conventions
# ---------------------------------------------------------------------------


def storage_dir_for(vm_name: str, root: Path = _ONEOFF_STORAGE_ROOT) -> Path:
    """Return the per-VM storage directory under the one-off root.

    Args:
        vm_name: The user-supplied VM name.
        root: Override the storage root (test seam — production callers
            should use the default).

    Returns:
        ``<root>/<vm_name>``.
    """
    return root / vm_name


# ---------------------------------------------------------------------------
# Argument parsers / value normalizers
# ---------------------------------------------------------------------------


def parse_ip4_option(value: str, default_network: str) -> tuple[str, str | None]:
    """Split a ``--ip4`` argument into ``(network_name, raw_ip_or_None)``.

    Accepts a bare ``"IP"`` (uses ``default_network``), a ``"NETWORK,IP"``
    pair, a bare ``"NETWORK"`` name (→ that network with DHCP, e.g.
    ``--ip4 vlan10``; issue #136), or a DHCP sentinel (``dhcp`` / ``default``
    / ``auto``, case-insensitive) in the IP slot of either form. A sentinel
    resolves the raw IP to ``None`` — i.e. DHCP, the same as omitting
    ``--ip4`` — so ``--ip4 default`` launches a DHCP VM instead of being
    mangled into the invalid address ``default/<netmask>`` (issue #105). A
    bare token is treated as a network name (DHCP) unless it is IP-ish
    (digits/dots, optional ``/CIDR``), which keeps the #105 clean error for a
    numeric typo.

    Args:
        value: The raw value from ``--ip4``.
        default_network: Network to assume when ``value`` is bare.

    Returns:
        ``(network_name, raw_ip)`` where ``raw_ip`` is ``None`` for DHCP.

    Raises:
        ValueError: ``value`` has a comma but either side is empty.
    """
    if "," in value:
        network, _, raw_ip = value.partition(",")
        network = network.strip()
        raw_ip = raw_ip.strip()
        if not network or not raw_ip:
            raise ValueError(
                f"Invalid --ip4 value '{value}'. Expected IP or NETWORK,IP."
            )
        if raw_ip.lower() in _DHCP_SENTINELS:
            return network, None
        return network, raw_ip
    raw_ip = value.strip()
    if raw_ip.lower() in _DHCP_SENTINELS:
        return default_network, None
    # A bare token that isn't IP-ish (only digits/dots, optional /CIDR) is a
    # network name → use that network with DHCP (e.g. ``--ip4 vlan10``). IP-ish
    # tokens stay on the static path so a numeric typo still gets the clean
    # "not a valid IPv4 address" error rather than a confusing network lookup
    # (issue #105 / #136).
    if not re.fullmatch(r"[\d.]+(/\d+)?", raw_ip):
        return raw_ip, None
    return default_network, raw_ip


def parse_ip6_option(value: str, default_network: str) -> tuple[str, str | None]:
    """IPv6 sibling of :func:`parse_ip4_option`.

    Mirrors every shape ``parse_ip4_option`` accepts, just with an IPv6
    IP-ish heuristic (hex digits + colons + optional ``/CIDR``):

    - ``ADDR`` (e.g. ``2001:db8::5``) → static v6 on ``default_network``.
    - ``NETWORK,ADDR`` → static v6 on a named network.
    - ``dhcp`` / ``default`` / ``auto`` (case-insensitive) → DHCPv6/SLAAC
        on ``default_network``, same as omitting ``--ip6``.
    - Bare network name (e.g. ``--ip6 vlan10``) → DHCPv6/SLAAC on that
        network.

    Args:
        value: The raw value from ``--ip6``.
        default_network: Network to assume when ``value`` is bare.

    Returns:
        ``(network_name, raw_ip)`` where ``raw_ip`` is ``None`` for the
        DHCPv6/SLAAC paths.

    Raises:
        ValueError: ``value`` has a comma but either side is empty.
    """
    if "," in value:
        network, _, raw_ip = value.partition(",")
        network = network.strip()
        raw_ip = raw_ip.strip()
        if not network or not raw_ip:
            raise ValueError(
                f"Invalid --ip6 value '{value}'. Expected ADDR or NETWORK,ADDR."
            )
        if raw_ip.lower() in _DHCP_SENTINELS:
            return network, None
        return network, raw_ip
    raw_ip = value.strip()
    if raw_ip.lower() in _DHCP_SENTINELS:
        return default_network, None
    # A bare token that isn't v6-ish (only hex digits / colons, optional
    # ``/CIDR``) is treated as a network name → SLAAC/DHCPv6 on it. The
    # IP-ish gate parallels the v4 path: a bare token that looks like an
    # address stays on the static path so a malformed v6 typo still hits
    # the clean ``not a valid IPv6 address`` error rather than a
    # confusing network lookup.
    if not re.fullmatch(r"[\da-fA-F:]+(/\d+)?", raw_ip):
        return raw_ip, None
    return default_network, raw_ip


def ensure_cidr(ip: str, netmask: str) -> str:
    """Ensure the supplied IP address carries a CIDR suffix.

    Args:
        ip: An IPv4 address, optionally already in CIDR form.
        netmask: CIDR prefix length to append when ``ip`` lacks one.

    Returns:
        ``ip`` unchanged when it already has a ``/``; otherwise
        ``f"{ip}/{netmask}"``.
    """
    if "/" in ip:
        return ip
    return f"{ip}/{netmask}"


def parse_memory_to_mib(value: str) -> str:
    """Convert a memory value with an optional unit suffix to MiB.

    Args:
        value: A memory size such as ``2048``, ``2G``, or ``512M``.

    Returns:
        The size in MiB as a decimal string (virt-install's ``--memory``
        unit).

    Raises:
        ValueError: ``value`` is not a number with an optional
            ``k/m/g/t/p`` suffix.
    """
    match = re.fullmatch(r"(\d+)([kKmMgGtTpP]?)", value.strip())
    if not match:
        raise ValueError(f"Invalid memory value '{value}'.")

    amount = int(match.group(1))
    unit = match.group(2).lower()
    if not unit or unit == "m":
        return str(amount)

    multipliers = {"k": 1 / 1024, "g": 1024, "t": 1024**2, "p": 1024**3}
    return str(int(amount * multipliers[unit]))


# ---------------------------------------------------------------------------
# CLI app + small helpers
# ---------------------------------------------------------------------------


app = typer.Typer(help="Create a libvirt VM using a cloud image and cloud-init.")


def _fail(message: str) -> None:
    """Print ``message`` in red and exit nonzero.

    Mirrors the reference: this raises :class:`typer.Exit`, so callers
    write ``_fail(...)`` as a statement (any following ``return`` is
    defensive and unreachable).

    Raises:
        typer.Exit: Always, with code 1.
    """
    secho(message, fg=typer.colors.RED)
    raise typer.Exit(code=1)


def _version_callback(value: bool) -> None:
    """Print the installed package version and exit when ``--version`` is set."""
    if value:
        typer.echo(f"createvm {__version__}")
        raise typer.Exit()


def _ensure_storage_root_writable(storage_root: Path) -> None:
    """Verify ``createvm`` can create the per-VM storage directory.

    Walks up to the nearest existing ancestor of ``storage_root`` and
    checks it's writable, failing fast with actionable guidance before any
    image download.

    Args:
        storage_root: The per-VM storage root.

    Raises:
        typer.Exit: The nearest existing ancestor denies write. The message
            points at ``libvirt`` group membership and ``root:libvirt 0771``
            permissions on ``/var/lib/libvirt/images``.
    """
    probe = storage_root
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    if not os.access(probe, os.W_OK):
        _fail(
            f"Cannot write under {storage_root} (nearest existing ancestor "
            f"{probe} is not writable). Ensure your user is in the 'libvirt' "
            f"group and that /var/lib/libvirt/images is root:libvirt mode 0771 "
            f"so group members can create sub-directories — e.g. "
            f"`sudo usermod -aG libvirt $USER` (then re-login) and "
            f"`sudo chmod 0771 /var/lib/libvirt/images`."
        )


def _build_cloud_image(name: str, entry: CatalogEntry, image_dir: Path) -> CloudImage:
    """Construct a :class:`CloudImage` from a catalog entry.

    Args:
        name: The catalog key (becomes ``CloudImage.name``).
        entry: The catalog entry providing URLs and metadata.
        image_dir: Root directory under which the cloud image lives.
            ``CloudImage`` appends ``/cloud-images/`` itself.

    Returns:
        A :class:`CloudImage` reading from ``image_dir/cloud-images/<filename>``.
    """
    config = {
        "image_url": entry.image_url,
        "checksum_url": entry.checksum_url,
        "checksum_type": entry.checksum_type,
        "checksum_url_gpg": entry.checksum_url_gpg,
        "network_version": entry.network_version,
        # Pass the already-resolved values so CloudImage's own catalog
        # resolution lands on the same os_variant/username as ``entry``.
        "os_variant": entry.os_variant,
        "username": entry.default_username,
    }
    config_defaults = {"cloud_image_basedir": str(image_dir)}
    return CloudImage(
        name=name, config=config, environment={}, config_defaults=config_defaults
    )


def _ensure_image_available(cloud_image: CloudImage) -> None:
    """Download and verify the cloud image if not already present locally.

    Args:
        cloud_image: A populated :class:`CloudImage` instance.

    Raises:
        typer.Exit: Download or verification failed.
    """
    try:
        if not cloud_image.exists_locally("image"):
            if not cloud_image.download_image():
                _fail(f"Failed to download cloud image from {cloud_image.image_url}")
        if cloud_image.checksum_url and not cloud_image.exists_locally("checksum"):
            cloud_image.download_checksum()
        if cloud_image.checksum_url_gpg and not cloud_image.exists_locally(
            "checksum_gpg"
        ):
            cloud_image.download_checksum_gpg()
    except ImageError as exc:
        # Clean boundary (issue #98): a transport/HTTP download failure (e.g.
        # the gzip-served Fedora GPG key returning 416) surfaces the
        # ImageError's actionable message + manual-placement workaround
        # instead of a raw requests traceback.
        _fail(str(exc))
    if cloud_image.checksum_url_gpg:
        cloud_image.gpg_verify_checksum_file()
    if cloud_image.checksum_url and not cloud_image.checksum_verify_image():
        _fail(f"Cloud image {cloud_image.image_fpath} failed checksum verification.")


def _initialize_cloud_images(catalog: dict[str, dict[str, Any]]) -> None:
    """Download every catalog image that isn't already cached.

    Args:
        catalog: A merged catalog from :func:`resolve_catalog`.

    Raises:
        typer.Exit: Any image fails to download or verify.
    """
    secho("Initializing cloud images...", fg=typer.colors.GREEN)
    for name in sorted(catalog):
        entry = resolve_image_entry(name, catalog)
        cloud_image = _build_cloud_image(name, entry, _CLOUD_IMAGE_BASEDIR)
        _ensure_image_available(cloud_image)


# ---------------------------------------------------------------------------
# Context assembly (image + network + credentials)
# ---------------------------------------------------------------------------


@dataclass
class _CreateVmContext:  # pylint: disable=too-many-instance-attributes
    """Everything resolved before any VM state is written to disk."""

    cloud_image: CloudImage
    entry: CatalogEntry
    network_name: str
    forward_mode: str
    dns_servers: list[str]
    gateway: str | None
    search_domains: list[str]
    vm_ip: str | None
    memory_mib: str
    password_plain: str
    password_hash: str
    # Resolved first-boot account: an explicit per-image ``username:`` wins,
    # else a host-config ``default_vm_username``, else the key-derived family
    # name (#138). Carried here so the cloud-init render + the SSH hint agree.
    username: str = ""
    # Deterministic NIC MAC pinned up front so the same address feeds both
    # ``virt-install --network ...,mac=`` and the cloud-init network-config's
    # ``match: macaddress`` (the only renderer-agnostic NIC selector).
    mac: str = ""
    authorized_keys: list[str] = field(default_factory=list)
    # Host-wide first-boot commands from the layered config (#138).
    runcmd: list[str] = field(default_factory=list)
    # Fully-rendered ``user-data`` from a layered ``user_data:`` override
    # (#138 §4). When set, it is written verbatim and the structured one-off
    # template is skipped. ``None`` means "use the structured template".
    user_data_override: str | None = None
    # IPv6 dual-stack (#137). All v6 fields are ``None`` / empty list when
    # the run is v4-only. ``vm_ip6`` is the resolved CIDR static address;
    # ``gateway6`` and ``dns_servers6`` come from the network's v6 settings
    # (NAT self-derives, bridge requires explicit ``--gateway6``/``--dns6``).
    vm_ip6: str | None = None
    gateway6: str | None = None
    dns_servers6: list[str] = field(default_factory=list)


def _resolve_network_and_ip(
    *, ip4: str | None, network_name: str | None, default_network: str
) -> tuple[str, str | None]:
    """Resolve the libvirt network name and any raw static IP.

    Returns:
        ``(network_name, raw_ip_or_None)``.
    """
    if ip4 is not None:
        return parse_ip4_option(ip4, network_name or default_network)
    return network_name or default_network, None


def _resolve_static_vm_ip(
    *, raw_ip: str | None, netmask: str, network_info: LibvirtNetworkInfo
) -> str | None:
    """Return the validated static CIDR for ``raw_ip``, or ``None`` for DHCP.

    A ``raw_ip`` of ``None`` means DHCP (the caller already mapped the
    DHCP sentinels and an omitted ``--ip4`` to ``None``). A non-``None``
    value that isn't a parseable IPv4 address raises a clean, actionable
    ``ValueError`` — *not* the stdlib ``ipaddress`` message that echoes
    the netmask-mangled ``<value>/<netmask>`` form the user never typed
    (issue #105). The subnet / DHCP-range checks in
    :func:`validate_static_ip` keep their own specific messages.
    """
    if raw_ip is None:
        return None
    vm_ip = ensure_cidr(raw_ip, netmask)
    try:
        ipaddress.ip_interface(vm_ip)
    except ValueError as exc:
        raise ValueError(
            f"--ip4 value {raw_ip!r} is not a valid IPv4 address. Pass an "
            "address like 192.168.122.50 (or NETWORK,192.168.122.50), or use "
            "--ip4 dhcp (or omit --ip4) for DHCP."
        ) from exc
    validate_static_ip(vm_ip, network_info)
    return vm_ip


def _resolve_v6_settings(
    *,
    ip6: str | None,
    network_name: str | None,
    config_default_network: str | None,
    resolved_network: str,
    network_info: LibvirtNetworkInfo,
    prefix6: str,
    default_dns6: list[str] | None,
    default_gateway6: str | None,
    default_search: list[str] | None,
) -> tuple[str | None, str | None, list[str]]:
    """Resolve the v6 leg of a (potentially dual-stack) createvm run.

    The v6 path is independent of v4 — it may target a different libvirt
    network — but the common case is a dual-stack manifest that shares
    one network for both families, in which case the v4 ``network_info``
    is reused without re-probing libvirt.

    Args:
        ip6: Raw ``--ip6`` flag value, or ``None`` when the run is v4-only.
        network_name: Explicit ``--network`` value, used when ``--ip6``
            doesn't include its own ``NETWORK,`` prefix.
        config_default_network: Layered-config default network name.
        resolved_network: The network already resolved by the v4 path —
            lets us reuse ``network_info`` when v6 lands on the same one.
        network_info: The v4 path's :class:`LibvirtNetworkInfo`. Reused
            verbatim when ``ip6`` targets the same network.
        prefix6: Default IPv6 prefix length appended to a bare static v6.
        default_dns6: ``--dns6`` value, parsed to a list (or ``None``).
        default_gateway6: ``--gateway6`` value (or ``None``).
        default_search: Reused for the v6 search-domain pass through.

    Returns:
        ``(vm_ip6, gateway6, dns_servers6)``. A SLAAC/DHCPv6 result
        comes back as ``(None, None, [])`` so the network-config render
        leaves the v6 stanza alone and dhcp6 stays enabled.

    Raises:
        ValueError: Static ``--ip6`` on a bridge network without both
            ``--gateway6`` and ``--dns6``, OR an unparseable static
            address (chained from :class:`ValueError` raised by
            :func:`_resolve_static_vm_ip6`).
        LibvirtNetworkError: ``resolve_network_settings6`` policy
            rejection (e.g. NAT network with no v6 gateway in its XML).
    """
    if ip6 is None:
        return None, None, []
    resolved_network6, raw_ip6 = parse_ip6_option(
        ip6, network_name or config_default_network or DEFAULT_NETWORK
    )
    network_info6 = (
        network_info
        if resolved_network6 == resolved_network
        else get_network_info(_SYSTEM_URI, resolved_network6)
    )
    # ``networks:`` entries are v4-only today; v6 defaults come from
    # ``--gateway6``/``--dns6``. Per-network v6 defaults under
    # ``networks[<name>].gateway6``/``.dns6`` are a tracked enhancement,
    # not in this first cut.
    if (
        raw_ip6 is not None
        and network_info6.forward_mode.lower() == "bridge"
        and not (default_gateway6 and default_dns6)
    ):
        raise ValueError(
            f"Network '{resolved_network6}' is a bridge; a static --ip6 "
            "needs --gateway6 <addr> and --dns6 <addr[,addr]> (no "
            "'networks:' entry covers v6 yet). Or use "
            f"'--ip6 {resolved_network6}' for SLAAC/DHCPv6 on it."
        )
    dns_servers6, gateway6, _ = resolve_network_settings6(
        network_info6,
        default_dns6=default_dns6,
        default_gateway6=default_gateway6,
        default_search=default_search,
    )
    vm_ip6 = _resolve_static_vm_ip6(
        raw_ip=raw_ip6, prefix6=prefix6, network_info=network_info6
    )
    # An ``--ip6`` that resolved to SLAAC/DHCPv6 (no static address)
    # should NOT inject v6 gateway/DNS into the static render path —
    # cloud-init's network-config leaves dhcp6 enabled and the
    # router-advertised values take effect at runtime.
    if vm_ip6 is None:
        return None, None, []
    return vm_ip6, gateway6, list(dns_servers6)


def _resolve_static_vm_ip6(
    *, raw_ip: str | None, prefix6: str, network_info: LibvirtNetworkInfo
) -> str | None:
    """IPv6 sibling of :func:`_resolve_static_vm_ip`.

    A ``raw_ip`` of ``None`` means SLAAC/DHCPv6. A non-``None`` value
    gets a ``/prefix6`` appended when it lacks one and is then validated
    by :func:`validate_static_ip` (dual-stack-aware: routes to v6 subnet
    + v6 DHCP range based on the address family).

    Args:
        raw_ip: Bare IPv6 address (with or without ``/CIDR``), or
            ``None`` for SLAAC/DHCPv6.
        prefix6: Default prefix length string (e.g. ``"64"``).
        network_info: A populated :class:`LibvirtNetworkInfo`.

    Returns:
        The CIDR string ready for the cloud-init render, or ``None`` for
        the SLAAC/DHCPv6 path.

    Raises:
        ValueError: ``raw_ip`` is not a parseable IPv6 address, or it
            collides with the network's static-IP policy.
    """
    if raw_ip is None:
        return None
    vm_ip = raw_ip if "/" in raw_ip else f"{raw_ip}/{prefix6}"
    try:
        ipaddress.IPv6Interface(vm_ip)
    except ValueError as exc:
        raise ValueError(
            f"--ip6 value {raw_ip!r} is not a valid IPv6 address. Pass an "
            "address like 2001:db8::5 (or NETWORK,2001:db8::5), or use "
            "--ip6 dhcp (or omit --ip6) for SLAAC/DHCPv6."
        ) from exc
    validate_static_ip(vm_ip, network_info)
    return vm_ip


def _resolve_authorized_keys(public_key: Path | None) -> list[str]:
    """Discover default SSH keys, append ``--public-key``, and dedupe.

    Raises:
        PublicKeyError: ``--public-key`` was provided and failed validation.
    """
    keys = discover_default_public_keys()
    if public_key is not None:
        keys.append(load_public_key(public_key.expanduser()))
    return dedupe_public_keys(keys)


def _build_createvm_context(
    *,
    catalog: dict[str, dict[str, Any]],
    vm_name: str,
    vm_distro: str,
    ip4: str | None,
    network_name: str | None,
    netmask: str,
    memory: str,
    public_key: Path | None,
    default_dns: list[str] | None = None,
    default_gateway: str | None = None,
    default_search: list[str] | None = None,
    networks: dict[str, NetworkDefaults] | None = None,
    config_default_network: str | None = None,
    default_vm_username: str | None = None,
    runcmd: list[str] | None = None,
    user_data: dict[str, Any] | None = None,
    ip6: str | None = None,
    prefix6: str = DEFAULT_PREFIX6,
    default_dns6: list[str] | None = None,
    default_gateway6: str | None = None,
) -> _CreateVmContext:
    """Resolve image, network, addressing, and credentials.

    ``default_dns`` / ``default_gateway`` / ``default_search`` come from the
    ``--dns`` / ``--gateway`` / ``--search-domain`` flags and are forwarded to
    :func:`resolve_network_settings`: required for a static address on a bridge
    network (a bridge has no libvirt-managed DNS/gateway), ignored for NAT.

    ``networks`` and ``config_default_network`` come from the layered host
    config (#138). They set the **lower-precedence** fallbacks that a flag
    overrides:

    - **Network name:** ``--ip4 NETWORK,IP`` -> ``--network`` ->
        ``config_default_network`` -> the built-in ``"default"``.
    - **gateway / dns / search:** the matching flag -> the resolved network's
        ``networks[<name>]`` entry -> :func:`resolve_network_settings` (NAT
        self-derivation, else the bridge "needs gateway+dns" error). So a
        configured bridge needs no flags; an unconfigured one still errors.

    ``default_vm_username`` (also from the layered config) sets the first-boot
    account when the image entry doesn't pin one: an explicit per-image
    ``username:`` wins, then ``default_vm_username``, then the key-derived
    family name (#138). ``runcmd`` is the layered config's host-wide
    first-boot command list, rendered into the guest's cloud-init user-data.

    ``user_data`` is a layered-config ``user-data`` override (#138 §4). When
    present it is rendered here (placeholders filled, discovered keys appended,
    ``runcmd`` prepended) into :attr:`_CreateVmContext.user_data_override`,
    replacing the structured one-off template; failing fast (before any state
    is written) on a bad placeholder or shape. An override that hard-codes an
    ``ssh_authorized_keys`` entry also satisfies the "no way to log in" guard,
    so a host with no discoverable ``~/.ssh`` key need not pass ``--public-key``.

    Every failure mode raises a typed exception the command body maps to a
    clean ``_fail``: :class:`DependencyError`, :class:`ValueError` (unknown
    distro / bad IP / bad memory / bridge static IP missing --gateway/--dns),
    :class:`LibvirtNetworkError`, :class:`PasswordHashError`,
    :class:`PublicKeyError`.
    """
    check_createvm_tooling()
    entry = resolve_image_entry(vm_distro, catalog)

    # First-boot account: an explicit per-image ``username:`` is a deliberate
    # pin and wins; otherwise a host-config ``default_vm_username`` overrides
    # the key-derived family guess (#138).
    username = (
        entry.default_username
        if entry.username_explicit
        else (default_vm_username or entry.default_username)
    )

    resolved_network, raw_ip = _resolve_network_and_ip(
        ip4=ip4,
        network_name=network_name,
        default_network=config_default_network or DEFAULT_NETWORK,
    )

    # Fold the resolved network's host-config defaults under the CLI flags:
    # an explicit flag wins, otherwise the networks[<name>] entry supplies the
    # value (and a missing entry leaves it None for resolve_network_settings).
    net_defaults = (networks or {}).get(resolved_network)
    eff_gateway = (
        default_gateway
        if default_gateway is not None
        else (net_defaults.gateway if net_defaults else None)
    )
    eff_dns = (
        default_dns
        if default_dns is not None
        else (net_defaults.dns if net_defaults else None)
    )
    eff_search = (
        default_search
        if default_search is not None
        else (net_defaults.search if net_defaults else None)
    )

    network_info = get_network_info(_SYSTEM_URI, resolved_network)
    # A static address on a bridge needs DNS + gateway from a flag or a
    # networks[<name>] entry; fail with the flag names before the generic
    # resolve_network_settings error (which is phrased for default_dns/gateway).
    if (
        raw_ip is not None
        and network_info.forward_mode.lower() == "bridge"
        and not (eff_gateway and eff_dns)
    ):
        raise ValueError(
            f"Network '{resolved_network}' is a bridge; a static --ip4 needs "
            "--gateway <ip> and --dns <ip[,ip]> (or a 'networks:' entry for it "
            f"in Lvlab.yml). Or use '--ip4 {resolved_network}' for DHCP on it, "
            "or a NAT network."
        )
    dns_servers, gateway, search_domains = resolve_network_settings(
        network_info,
        default_dns=eff_dns,
        default_gateway=eff_gateway,
        default_search=eff_search,
    )
    vm_ip = _resolve_static_vm_ip(
        raw_ip=raw_ip, netmask=netmask, network_info=network_info
    )

    vm_ip6, gateway6, dns_servers6 = _resolve_v6_settings(
        ip6=ip6,
        network_name=network_name,
        config_default_network=config_default_network,
        resolved_network=resolved_network,
        network_info=network_info,
        prefix6=prefix6,
        default_dns6=default_dns6,
        default_gateway6=default_gateway6,
        default_search=default_search,
    )

    memory_mib = parse_memory_to_mib(memory)
    password_plain = generate_password_phrase()
    password_hash = hash_password_sha512(password_plain)
    authorized_keys = _resolve_authorized_keys(public_key)
    if not authorized_keys and not (
        user_data is not None and user_data_supplies_keys(user_data)
    ):
        raise PublicKeyError(
            "No SSH public keys discovered and none supplied via --public-key "
            "(and no 'user_data' override hard-codes one). Refusing to create "
            "a VM with no way to log in."
        )

    # A layered ``user_data:`` override owns the whole ``user-data`` document.
    # Render it now (fail fast, before any disk/image state) with the structured
    # path's resolved values exposed as placeholders; the host-wide top-level
    # ``runcmd`` is prepended ahead of the override's own ``runcmd``.
    user_data_override: str | None = None
    if user_data is not None:
        user_data_override = render_user_data_override(
            user_data,
            context={
                "vm_name": vm_name,
                "vm_hostname": vm_name.split(".")[0],
                "default_vm_username": username,
                "password_hash": password_hash,
            },
            authorized_keys=authorized_keys,
            runcmd_prefix=runcmd or [],
        )

    cloud_image = _build_cloud_image(vm_distro, entry, _CLOUD_IMAGE_BASEDIR)

    return _CreateVmContext(
        cloud_image=cloud_image,
        entry=entry,
        network_name=resolved_network,
        forward_mode=network_info.forward_mode,
        dns_servers=dns_servers,
        gateway=gateway,
        search_domains=search_domains,
        vm_ip=vm_ip,
        memory_mib=memory_mib,
        password_plain=password_plain,
        password_hash=password_hash,
        username=username,
        mac=generate_mac(),
        authorized_keys=authorized_keys,
        runcmd=list(runcmd or []),
        user_data_override=user_data_override,
        vm_ip6=vm_ip6,
        gateway6=gateway6,
        dns_servers6=dns_servers6,
    )


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


def _check_vm_preconditions(vm_name: str, vm_dir: Path) -> None:
    """Guard against clobbering existing state before provisioning.

    Raises:
        typer.Exit: The per-VM directory already exists, or a libvirt
            domain with this name is already defined.
    """
    if vm_dir.exists():
        _fail(f"VM directory '{vm_dir}' already exists. Cannot create VM.")
    if vm_exists(_SYSTEM_URI, vm_name):
        _fail(f"VM '{vm_name}' already exists in libvirt. Cannot create VM.")


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


def _render_cloud_init(*, vm_dir: Path, vm_name: str, ctx: _CreateVmContext) -> Path:
    """Render meta-data / user-data / network-config and pack the cidata ISO.

    Args:
        vm_dir: The per-VM directory (already created).
        vm_name: The raw VM name (also the libvirt domain name).
        ctx: The resolved create context.

    Returns:
        Path to the freshly-written ``cidata.iso``.

    Raises:
        OSError: ``CloudInitIso.write`` reported failure.
    """
    oneoff = OneoffCloudInit(
        libvirt_vm_name=vm_name,
        hostname=vm_name.split(".")[0],
        fqdn=vm_name,
        username=ctx.username,
        ssh_public_keys=ctx.authorized_keys,
        password_hash=ctx.password_hash,
        runcmd=ctx.runcmd,
    )

    iface: dict[str, Any] = {"name": "eth0", "macaddress": ctx.mac}
    nameservers: dict[str, Any] = {}
    if ctx.vm_ip is not None:
        iface["ip4"] = ctx.vm_ip
        iface["ip4gw"] = ctx.gateway
        nameservers = {"addresses": ctx.dns_servers, "search": ctx.search_domains}
    # IPv6 dual-stack (#137): a separate static v6 also feeds the same
    # interface dict. netplan accepts a mixed-family ``addresses`` list,
    # so v6 DNS appends onto the v4 DNS list.
    if ctx.vm_ip6 is not None:
        iface["ip6"] = ctx.vm_ip6
        iface["ip6gw"] = ctx.gateway6
        existing_addresses = list(nameservers.get("addresses", []))
        nameservers["addresses"] = existing_addresses + list(ctx.dns_servers6)
        nameservers.setdefault("search", ctx.search_domains)

    network_config = NetworkConfig(ctx.entry.network_version, [iface], nameservers)

    meta_data_path = vm_dir / "meta-data"
    user_data_path = vm_dir / "user-data"
    network_config_path = vm_dir / "network-config"
    cidata_path = vm_dir / "cidata.iso"

    # A layered ``user_data:`` override (already rendered in the context) owns
    # the user-data document; otherwise the structured one-off template renders it.
    user_data = (
        ctx.user_data_override
        if ctx.user_data_override is not None
        else oneoff.render_user_data()
    )

    network_config_path.write_text(network_config.render_config(), encoding="utf-8")
    user_data_path.write_text(user_data, encoding="utf-8")
    meta_data_path.write_text(oneoff.render_meta_data(), encoding="utf-8")

    iso = CloudInitIso(
        meta_data_fpath=str(meta_data_path),
        user_data_fpath=str(user_data_path),
        network_config_fpath=str(network_config_path),
        iso_fpath=str(cidata_path),
    )
    if not iso.write():
        raise OSError("Failed to build cidata.iso.")
    return cidata_path


def _run_cmd(argv: list[str]) -> None:
    """Run a provisioning subprocess with system-first PATH, raising on failure.

    The environment is set via :func:`system_first_env` so that any binary
    using a ``#!/usr/bin/env python3`` shebang (e.g. ``virt-install`` on
    Debian 13) resolves the interpreter to the host's system Python.

    Raises:
        subprocess.CalledProcessError: The command exited nonzero.
        OSError: The binary was not found.
    """
    subprocess.run(
        argv, check=True, capture_output=True, text=True, env=system_first_env()
    )


# Binary (1024-based) unit multipliers, matching qemu-img's size conventions
# (``qemu-img resize`` treats ``K``/``M``/``G``/``T`` as powers of 1024).
_SIZE_UNIT_MULTIPLIERS: dict[str, int] = {
    "k": 1024,
    "m": 1024**2,
    "g": 1024**3,
    "t": 1024**4,
}


def parse_disk_size_to_bytes(value: str) -> int:
    """Convert a qemu-img-style disk size string to a byte count.

    Units are binary (1024-based), matching ``qemu-img resize`` — ``1G`` is
    ``1024**3`` bytes, not ``10**9``. A bare number with no suffix is treated
    as a byte count.

    Args:
        value: A disk size such as ``35G``, ``512M``, or ``10737418240``.

    Returns:
        The size in bytes.

    Raises:
        ValueError: ``value`` is not a non-negative integer with an optional
            ``k/m/g/t`` suffix.
    """
    match = re.fullmatch(r"(\d+)([kKmMgGtT]?)", value.strip())
    if not match:
        raise ValueError(f"Invalid disk size '{value}'.")

    amount = int(match.group(1))
    unit = match.group(2).lower()
    if not unit:
        return amount
    return amount * _SIZE_UNIT_MULTIPLIERS[unit]


def _image_virtual_size_bytes(image_fpath: str) -> int:
    """Return the virtual size (in bytes) of a qcow2/raw image via qemu-img.

    Args:
        image_fpath: Path to the base cloud image.

    Returns:
        The image's ``virtual-size`` in bytes.

    Raises:
        subprocess.CalledProcessError: ``qemu-img info`` exited nonzero.
        OSError: The ``qemu-img`` binary was not found.
        ValueError: The JSON output lacked an integer ``virtual-size``.
    """
    proc = subprocess.run(
        ["qemu-img", "info", "--output=json", image_fpath],
        check=True,
        capture_output=True,
        text=True,
        env=system_first_env(),
    )
    info = json.loads(proc.stdout)
    virtual_size = info.get("virtual-size")
    if not isinstance(virtual_size, int):
        raise ValueError(
            f"qemu-img info gave no integer virtual-size for '{image_fpath}'."
        )
    return virtual_size


def _human_size(num_bytes: int) -> str:
    """Format a byte count as a compact binary-unit string (for messages only).

    Args:
        num_bytes: A byte count.

    Returns:
        A short label such as ``10G`` or ``512M``; falls back to a raw byte
        count for sizes that don't divide cleanly into a single unit.
    """
    for unit, multiplier in (
        ("T", 1024**4),
        ("G", 1024**3),
        ("M", 1024**2),
        ("K", 1024),
    ):
        if num_bytes >= multiplier and num_bytes % multiplier == 0:
            return f"{num_bytes // multiplier}{unit}"
    return f"{num_bytes}B"


def _virt_install_argv(
    *,
    vm_name: str,
    memory_mib: str,
    cpu: str,
    disk_path: Path,
    cidata_path: Path,
    os_variant: str,
    network_name: str,
    mac: str,
) -> list[str]:
    """Build the ``virt-install`` argument vector (managed network, spice).

    ``mac`` is pinned on the ``--network`` arg so it matches the
    ``match: macaddress`` selector rendered into the guest's cloud-init
    network-config; see :func:`tkc_lvlab.utils.network.generate_mac`.
    """
    try:
        resolved_variant, fallback_reason = resolve_os_variant(os_variant)
    except OsInfoLookupError as exc:
        secho(
            f"warning: could not resolve --os-variant against osinfo-db ({exc}); "
            f"using requested {os_variant!r} as-is",
            fg=typer.colors.YELLOW,
        )
        resolved_variant = os_variant
    else:
        if fallback_reason:
            secho(f"warning: {fallback_reason}", fg=typer.colors.YELLOW)

    return [
        "virt-install",
        f"--connect={_SYSTEM_URI}",
        f"--name={vm_name}",
        f"--memory={memory_mib}",
        f"--vcpus={cpu}",
        "--import",
        f"--disk=path={disk_path}",
        f"--disk={cidata_path},device=cdrom",
        f"--os-variant={resolved_variant}",
        "--network",
        f"network={network_name},model=virtio,mac={mac}",
        "--graphics",
        "spice,listen=127.0.0.1",
        "--noautoconsole",
    ]


def _provision_vm(
    *, vm_dir: Path, vm_name: str, ctx: _CreateVmContext, disk_size: str, cpu: str
) -> None:
    """Render cloud-init, copy + resize the disk, and run ``virt-install``.

    ``vm_dir`` must already exist. Raises on the first failure so the
    command body can wipe the partial directory.

    Raises:
        subprocess.CalledProcessError: ``cp`` / ``qemu-img`` / ``virt-install``
            exited nonzero.
        OSError: Disk copy or ISO build failed.
    """
    secho(f"Using image: {ctx.cloud_image.image_fpath}", fg=typer.colors.GREEN)
    secho(f"Using os-variant: {ctx.entry.os_variant}", fg=typer.colors.GREEN)

    cidata_path = _render_cloud_init(vm_dir=vm_dir, vm_name=vm_name, ctx=ctx)
    disk_path = vm_dir / "disk0.qcow2"

    secho("Copying base image...", fg=typer.colors.GREEN)
    shutil.copyfile(ctx.cloud_image.image_fpath, disk_path)

    # Deliberate divergence from lvscripts-py's unconditional `qemu-img resize`
    # (ref #88): qemu-img cannot shrink a qcow2 (`resize` to a smaller size
    # fails), so a `--disk-size` at or below the base image's virtual size
    # would crash the whole provision. Skip the resize and keep the base size
    # instead, warning loudly so the requested-vs-actual mismatch is visible.
    base_virtual_size = _image_virtual_size_bytes(ctx.cloud_image.image_fpath)
    requested_size = parse_disk_size_to_bytes(disk_size)
    if requested_size <= base_virtual_size:
        secho(
            f"Requested --disk-size {disk_size} is <= base image virtual size "
            f"{_human_size(base_virtual_size)}; skipping resize, keeping "
            f"{_human_size(base_virtual_size)}.",
            fg=typer.colors.YELLOW,
        )
    else:
        secho(f"Resizing disk to {disk_size}...", fg=typer.colors.GREEN)
        _run_cmd(["qemu-img", "resize", str(disk_path), disk_size])

    secho("Starting install...", fg=typer.colors.GREEN)
    _run_cmd(
        _virt_install_argv(
            vm_name=vm_name,
            memory_mib=ctx.memory_mib,
            cpu=cpu,
            disk_path=disk_path,
            cidata_path=cidata_path,
            os_variant=ctx.entry.os_variant,
            network_name=ctx.network_name,
            mac=ctx.mac,
        )
    )


def _command_error_details(exc: subprocess.CalledProcessError | OSError) -> str:
    """Extract a stderr/stdout tail from a failed provisioning command."""
    if not isinstance(exc, subprocess.CalledProcessError):
        return ""
    stderr = (exc.stderr or "").strip()
    stdout = (exc.stdout or "").strip()
    if stderr:
        return f"\n{stderr}"
    if stdout:
        return f"\n{stdout}"
    return ""


def _cleanup_failed_vm_dir(vm_dir: Path) -> None:
    """Best-effort wipe of a partially-provisioned VM directory."""
    if not vm_dir.exists():
        return
    try:
        shutil.rmtree(vm_dir)
    except OSError as exc:
        secho(
            f"VM provisioning failed and cleanup of '{vm_dir}' also failed: {exc}",
            fg=typer.colors.RED,
        )


# ---------------------------------------------------------------------------
# Completion report + DHCP lease discovery (ported from the reference)
# ---------------------------------------------------------------------------


_DHCP_LEASE_PATTERN = re.compile(
    r"^\s*\S+\s+\S+\s+"
    r"(?P<mac>(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})\s+"
    r"ipv4\s+(?P<ip>\d+\.\d+\.\d+\.\d+/\d+)\s+(?P<hostname>\S+)"
)


def _iter_parsed_leases(leases_output: str):
    """Yield ``(mac_lower, ip_cidr, hostname)`` for each parseable IPv4 lease."""
    for line in leases_output.splitlines():
        if "ipv4" not in line:
            continue
        match = _DHCP_LEASE_PATTERN.search(line)
        if match is None:
            continue
        yield match.group("mac").lower(), match.group("ip"), match.group("hostname")


def _hostname_forms(value: str) -> tuple[str, str]:
    normalized = value.strip().strip(".").lower()
    short = normalized.split(".")[0] if normalized else ""
    return normalized, short


def _hostname_exact_match(expected: str, observed: str) -> bool:
    expected_full, expected_short = _hostname_forms(expected)
    observed_full, observed_short = _hostname_forms(observed)
    return observed_full in (expected_full, expected_short) or observed_short in (
        expected_full,
        expected_short,
    )


def _hostname_matches(expected: str, observed: str) -> bool:
    expected_full, expected_short = _hostname_forms(expected)
    observed_full, observed_short = _hostname_forms(observed)
    if observed_full in (expected_full, expected_short):
        return True
    if observed_short in (expected_full, expected_short):
        return True
    return observed_short.startswith(expected_short) or expected_short.startswith(
        observed_short
    )


def _match_lease_by_mac(leases, vm_mac: str) -> str | None:
    target = vm_mac.lower()
    for lease_mac, lease_ip, _ in leases:
        if lease_mac == target:
            return lease_ip
    return None


def _match_lease_by_hostname(leases, vm_hostname: str) -> str | None:
    exact_match: str | None = None
    fuzzy_match: str | None = None
    for _, lease_ip, lease_hostname in leases:
        if lease_hostname == "-":
            continue
        if _hostname_exact_match(vm_hostname, lease_hostname):
            exact_match = lease_ip
        elif fuzzy_match is None and _hostname_matches(vm_hostname, lease_hostname):
            fuzzy_match = lease_ip
    return exact_match if exact_match is not None else fuzzy_match


def _extract_lease_ip(
    leases_output: str, vm_hostname: str, vm_mac: str | None
) -> str | None:
    """Extract the first IPv4 CIDR lease by MAC, else by fuzzy hostname."""
    if vm_mac is not None:
        return _match_lease_by_mac(_iter_parsed_leases(leases_output), vm_mac)
    return _match_lease_by_hostname(_iter_parsed_leases(leases_output), vm_hostname)


def _wait_for_dhcp_lease(
    vm_hostname: str, network_name: str, *, vm_mac: str | None, timeout_seconds: int
) -> str | None:
    """Poll ``virsh net-dhcp-leases`` and return the VM IPv4 CIDR when it appears."""
    with Progress(
        TextColumn("Waiting for DHCP lease"),
        BarColumn(),
        TextColumn("{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
        transient=True,
    ) as progress:
        task_id = progress.add_task("dhcp-wait", total=timeout_seconds)
        for _ in range(timeout_seconds):
            try:
                result = run_virsh(
                    _SYSTEM_URI, ["net-dhcp-leases", network_name], check=False
                )
            except VirshError:
                result = None
            if result is not None and result.returncode == 0:
                lease_ip = _extract_lease_ip(result.stdout, vm_hostname, vm_mac)
                if lease_ip is not None:
                    progress.update(task_id, completed=timeout_seconds)
                    return lease_ip
            time.sleep(1)
            progress.advance(task_id)
    return None


def _print_completion_details(
    *, ctx: _CreateVmContext, vm_name: str, manifest_path: Path | None
) -> None:
    """Print the password, config note, and an SSH hint (lease-aware for NAT)."""
    vm_hostname = vm_name.split(".")[0]
    username = ctx.username

    if manifest_path is not None:
        secho(f"Using config: {manifest_path}", fg=typer.colors.BLUE)

    secho("VM creation completed.", fg=typer.colors.GREEN)
    typer.echo()
    # Shared one-time-password + SSH-hint output (issue #106): lvlab up uses
    # the same helpers so both read consistently.
    render_one_time_password(ctx.password_plain)

    if ctx.vm_ip is not None:
        static_ip = ctx.vm_ip.split("/", maxsplit=1)[0]
        render_ssh_hint(username, static_ip)
        return

    if ctx.forward_mode.lower() != "nat":
        secho(
            f"Skipping libvirt DHCP lease wait for network '{ctx.network_name}' "
            f"(forward mode: {ctx.forward_mode}).",
            fg=typer.colors.BLUE,
        )
        secho(
            "This network relies on external DHCP, so virsh lease queries may not "
            "show the VM address.",
            fg=typer.colors.YELLOW,
        )
        secho(
            "Check your upstream DHCP service or DNS to find the assigned IP.",
            fg=typer.colors.YELLOW,
        )
        typer.echo()
        return

    # The MAC is pinned by createvm (ctx.mac) and passed verbatim to
    # virt-install, so it's authoritative — no need to read it back from
    # domiflist.
    lease_ip = _wait_for_dhcp_lease(
        vm_hostname,
        ctx.network_name,
        vm_mac=ctx.mac,
        timeout_seconds=NAT_DHCP_LEASE_WAIT_SECONDS,
    )
    if lease_ip is not None:
        secho(
            f"DHCP lease detected for {vm_hostname}: {lease_ip}", fg=typer.colors.GREEN
        )
        typer.echo()
        render_ssh_hint(username, lease_ip.split("/", maxsplit=1)[0])
        return

    secho(
        f"No DHCP lease was found for '{vm_hostname}' on network "
        f"'{ctx.network_name}' within {NAT_DHCP_LEASE_WAIT_SECONDS} seconds.",
        fg=typer.colors.YELLOW,
    )
    secho(
        f"You can check manually with: sudo virsh net-dhcp-leases {ctx.network_name}",
        fg=typer.colors.YELLOW,
    )
    typer.echo()


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def _manifest_path_used(config_path: Path | None) -> Path | None:
    """Return the manifest path that resolved the catalog, for the report."""
    if config_path is not None:
        return config_path
    cwd_manifest = Path("Lvlab.yml")
    return cwd_manifest if cwd_manifest.is_file() else None


@app.command()
def createvm(  # pylint: disable=too-many-arguments,too-many-locals
    vm_name: str | None = typer.Argument(None, help="FQDN for the VM."),
    vm_distro: str | None = typer.Argument(None, help="Configured distro key."),
    ip4: str | None = typer.Option(
        None,
        "--ip4",
        help=(
            "Static IPv4 address: IP or NETWORK,IP. Use 'dhcp' (or 'default'/"
            "'auto'), or omit the flag, for DHCP."
        ),
    ),
    netmask: str = typer.Option(
        DEFAULT_NETMASK, help="CIDR netmask to append if the IP lacks one."
    ),
    disk_size: str = typer.Option(DEFAULT_DISK_SIZE, help="Disk size for the VM."),
    cpu: str = typer.Option(DEFAULT_CPU, help="Number of vCPUs."),
    memory: str = typer.Option(DEFAULT_MEMORY, help="Memory size for the VM."),
    network_name: str | None = typer.Option(
        None,
        "--network",
        help="Libvirt network name (defaults to 'default').",
    ),
    gateway: str | None = typer.Option(
        None,
        "--gateway",
        help=(
            "Gateway IP for a static --ip4 on a bridge network. Required with "
            "--dns for a bridge; ignored for NAT (self-derived)."
        ),
    ),
    dns: str | None = typer.Option(
        None,
        "--dns",
        help=(
            "Comma-separated DNS server(s) for a static --ip4 on a bridge "
            "network. Required with --gateway for a bridge; ignored for NAT."
        ),
    ),
    search_domain: str | None = typer.Option(
        None,
        "--search-domain",
        help="Comma-separated DNS search domain(s) (honored on NAT and bridge).",
    ),
    ip6: str | None = typer.Option(
        None,
        "--ip6",
        help=(
            "Static IPv6 address (dual-stack with --ip4): ADDR or NETWORK,ADDR. "
            "Use 'dhcp' (or 'default'/'auto'), or omit the flag, for "
            "SLAAC/DHCPv6."
        ),
    ),
    gateway6: str | None = typer.Option(
        None,
        "--gateway6",
        help=(
            "IPv6 gateway for a static --ip6 on a bridge network. Required with "
            "--dns6 for a bridge; ignored for NAT (self-derived)."
        ),
    ),
    dns6: str | None = typer.Option(
        None,
        "--dns6",
        help=(
            "Comma-separated IPv6 DNS server(s) for a static --ip6 on a bridge "
            "network. Required with --gateway6 for a bridge; ignored for NAT."
        ),
    ),
    public_key: Path | None = typer.Option(
        None,
        "--public-key",
        help="Path to an additional SSH public key to append.",
    ),
    init_cloud_images: bool = typer.Option(
        False,
        "--init-cloud-images",
        help=(
            "Download any missing cloud images before VM creation. "
            "DEPRECATED: prefer 'lvlab init' (the single image-init path)."
        ),
    ),
    config_path: Path | None = typer.Option(
        None,
        "--config",
        help="Path to a specific Lvlab.yml manifest (.yaml or .yml).",
    ),
    storage_root: Path = typer.Option(
        _ONEOFF_STORAGE_ROOT,
        "--storage-root",
        hidden=True,
        file_okay=False,
        help="Override the per-VM storage root (test seam).",
    ),
    no_color: bool = typer.Option(
        False,
        "--no-color",
        help="Disable colored output (also honors the NO_COLOR env var).",
    ),
    version: bool = typer.Option(  # pylint: disable=unused-argument
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the installed tkc-lvlab package version and exit.",
    ),
) -> None:
    """Create a libvirt VM using configured cloud images and cloud-init."""
    if no_color:
        set_no_color(True)
    has_vm_args = vm_name is not None or vm_distro is not None
    has_all_vm_args = vm_name is not None and vm_distro is not None

    if has_vm_args and not has_all_vm_args:
        _fail("VM_NAME and VM_DISTRO must be provided together.")
    if not init_cloud_images and not has_all_vm_args:
        _fail("Missing required arguments: VM_NAME and VM_DISTRO.")

    try:
        host_config: HostConfig = load_host_config(config_path)
        catalog = resolve_catalog(host_config.images or None)
    except ValueError as exc:
        _fail(str(exc))

    if init_cloud_images:
        # Deprecated in favour of `lvlab init`, which is now the single
        # image-init path and also initializes the built-in defaults when no
        # Lvlab.yml is present (issue #97). Kept working for compatibility.
        secho(
            "Note: 'createvm --init-cloud-images' is deprecated; use 'lvlab init' "
            "instead (it initializes the built-in defaults with no Lvlab.yml). "
            "This flag still works for now.",
            fg=typer.colors.YELLOW,
        )
        _initialize_cloud_images(catalog)
        if not has_all_vm_args:
            secho("Cloud images initialized.", fg=typer.colors.GREEN)
            return

    # vm_name / vm_distro guaranteed by the validation above.
    assert vm_name is not None and vm_distro is not None

    typer.echo()
    _ensure_storage_root_writable(storage_root)

    dns_servers = [s.strip() for s in dns.split(",") if s.strip()] if dns else None
    dns_servers6 = [s.strip() for s in dns6.split(",") if s.strip()] if dns6 else None
    search_domains = (
        [s.strip() for s in search_domain.split(",") if s.strip()]
        if search_domain
        else None
    )

    try:
        ctx = _build_createvm_context(
            catalog=catalog,
            vm_name=vm_name,
            vm_distro=vm_distro,
            ip4=ip4,
            network_name=network_name,
            netmask=netmask,
            memory=memory,
            public_key=public_key,
            default_dns=dns_servers,
            default_gateway=gateway,
            default_search=search_domains,
            networks=host_config.networks,
            config_default_network=host_config.default_network,
            default_vm_username=host_config.default_vm_username,
            runcmd=host_config.runcmd,
            user_data=host_config.user_data,
            ip6=ip6,
            default_dns6=dns_servers6,
            default_gateway6=gateway6,
        )
    except (
        LibvirtNetworkError,
        PasswordHashError,
        PublicKeyError,
        ValueError,
        DependencyError,
    ) as exc:
        _fail(str(exc))

    vm_dir = storage_dir_for(vm_name, root=storage_root)
    _check_vm_preconditions(vm_name, vm_dir)
    _ensure_image_available(ctx.cloud_image)

    try:
        vm_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        _fail(f"VM directory '{vm_dir}' already exists. Cannot create VM.")

    try:
        _provision_vm(
            vm_dir=vm_dir, vm_name=vm_name, ctx=ctx, disk_size=disk_size, cpu=cpu
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        _cleanup_failed_vm_dir(vm_dir)
        _fail(
            f"VM creation failed and cleanup completed: {exc}{_command_error_details(exc)}"
        )

    _print_completion_details(
        ctx=ctx, vm_name=vm_name, manifest_path=_manifest_path_used(config_path)
    )


# Backwards-compat alias for the entry point and external imports.
run = app


if __name__ == "__main__":  # pragma: no cover
    app()
