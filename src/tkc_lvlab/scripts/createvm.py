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
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

from .. import __version__
from ..config import parse_config
from ..utils.cloud_init import CloudInitIso, NetworkConfig
from ..utils.images import CloudImage
from ..utils.network import (
    LibvirtNetworkError,
    LibvirtNetworkInfo,
    get_network_info,
    resolve_network_settings,
    validate_static_ip,
)
from ..utils.osinfo import OsInfoLookupError, resolve_os_variant
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
from ..utils.standalone_cloud_init import OneoffCloudInit
from ..utils.subprocess_env import system_first_env
from ..utils.virsh import VirshError, run_virsh, vm_exists


# ---------------------------------------------------------------------------
# Defaults (mirror the lvscripts reference)
# ---------------------------------------------------------------------------

_SYSTEM_URI = "qemu:///system"
DEFAULT_NETWORK = "default"
DEFAULT_NETMASK = "24"
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


@dataclass(frozen=True)
class CatalogEntry:
    """A fully-resolved cloud image ready for ``createvm`` to deploy.

    Built by :func:`resolve_image_entry` from a merged catalog dict —
    either a :data:`BUILTIN_IMAGES` entry or an ``Lvlab.yml`` ``images:``
    entry. The ``os_variant`` and ``default_username`` fields are filled
    by derivation from the image key unless the source dict overrides
    them (see :func:`derive_os_variant` / :func:`derive_username`).

    Attributes:
        image_url: Direct URL to the qcow2 cloud image.
        checksum_url: URL to the checksum manifest, or ``None`` for an
            unverified custom image.
        checksum_type: Hash algorithm — ``sha256`` or ``sha512`` — or
            ``None`` when there's no checksum.
        checksum_url_gpg: Optional URL to a GPG keyring for verifying the
            checksum file. When ``None``, GPG verification is skipped.
        network_version: cloud-init network-config schema version,
            ``1`` (ENI-style) or ``2`` (netplan-style).
        os_variant: ``virt-install --os-variant`` argument. Passed
            through :func:`tkc_lvlab.utils.osinfo.resolve_os_variant`
            at deploy time for osinfo-db fuzzy fallback.
        default_username: First-boot account name cloud-init creates.
    """

    image_url: str
    checksum_url: str | None
    checksum_type: str | None
    checksum_url_gpg: str | None
    network_version: int
    os_variant: str
    default_username: str


# Built-in cloud images the standalone ``createvm`` script can resolve via
# ``VM_DISTRO`` out of the box. Each value uses the **same schema as an
# ``Lvlab.yml`` ``images:`` entry** so the built-in catalog and a cwd
# manifest merge through one code path (:func:`resolve_catalog`). The two
# createvm-only knobs — ``os_variant`` and ``username`` — are intentionally
# omitted: they're derived from the key (debian12 -> debian12 / debian) and
# only need to appear when the derivation is wrong.
#
# The ``refresh-cloud-images`` skill under ``.claude/skills/`` keeps these
# in sync with upstream and surfaces new-major / EOL drift.
BUILTIN_IMAGES: dict[str, dict[str, Any]] = {
    "debian12": {
        "image_url": (
            "https://cloud.debian.org/images/cloud/bookworm/20260518-2482/"
            "debian-12-generic-amd64-20260518-2482.qcow2"
        ),
        "checksum_url": (
            "https://cloud.debian.org/images/cloud/bookworm/20260518-2482/SHA512SUMS"
        ),
        "checksum_type": "sha512",
        "checksum_url_gpg": None,
        "network_version": 2,
    },
    "debian13": {
        "image_url": (
            "https://cloud.debian.org/images/cloud/trixie/latest/"
            "debian-13-generic-amd64.qcow2"
        ),
        "checksum_url": "https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS",
        "checksum_type": "sha512",
        "checksum_url_gpg": None,
        "network_version": 2,
    },
    "fedora44": {
        "image_url": (
            "https://download.fedoraproject.org/pub/fedora/linux/releases/44/"
            "Cloud/x86_64/images/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2"
        ),
        "checksum_url": (
            "https://download.fedoraproject.org/pub/fedora/linux/releases/44/"
            "Cloud/x86_64/images/Fedora-Cloud-44-1.7-x86_64-CHECKSUM"
        ),
        "checksum_type": "sha256",
        "checksum_url_gpg": "https://fedoraproject.org/fedora.gpg",
        "network_version": 2,
    },
}


# First-boot account names keyed by distro family. Anything not listed
# falls back to the family token itself (e.g. ``alpine318`` -> ``alpine``),
# and any entry can be overridden per image with a ``username:`` key.
_USERNAME_BY_FAMILY: dict[str, str] = {
    "debian": "debian",
    "ubuntu": "ubuntu",
    "fedora": "fedora",
    "almalinux": "almalinux",
    "rocky": "rocky",
    "centos": "cloud-user",
    "rhel": "cloud-user",
}


def _family_token(key: str) -> str:
    """Return the leading alphabetic family token of a catalog key.

    ``debian12`` -> ``debian``, ``debian12-salt`` -> ``debian``,
    ``fedora44`` -> ``fedora``. Falls back to the whole lower-cased key
    when it has no leading letters.

    Args:
        key: A catalog / ``VM_DISTRO`` key.

    Returns:
        The family token, lower-cased.
    """
    match = re.match(r"[a-z]+", key.lower())
    return match.group(0) if match else key.lower()


def derive_os_variant(key: str, explicit: str | None) -> str:
    """Resolve the ``--os-variant`` value for a catalog key.

    Args:
        key: The catalog / ``VM_DISTRO`` key (e.g. ``debian12-salt``).
        explicit: An ``os_variant`` provided by the catalog dict /
            manifest, or ``None`` to derive.

    Returns:
        ``explicit`` when set; otherwise the segment of ``key`` before the
        first ``-`` (``debian12-salt`` -> ``debian12``). The deploy path
        runs this through ``resolve_os_variant`` for osinfo-db fuzzy
        fallback, so an approximate value is fine.
    """
    return explicit if explicit else key.split("-")[0]


def derive_username(key: str, explicit: str | None) -> str:
    """Resolve the first-boot username for a catalog key.

    Args:
        key: The catalog / ``VM_DISTRO`` key.
        explicit: A ``username`` provided by the catalog dict / manifest,
            or ``None`` to derive.

    Returns:
        ``explicit`` when set; otherwise the family-conventional user from
        :data:`_USERNAME_BY_FAMILY`, falling back to the family token.
    """
    if explicit:
        return explicit
    family = _family_token(key)
    return _USERNAME_BY_FAMILY.get(family, family)


def resolve_catalog(
    manifest_images: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Merge the built-in catalog with a manifest's ``images:`` section.

    Args:
        manifest_images: The ``images:`` map from an ``Lvlab.yml``, or
            ``None`` when no manifest is present.

    Returns:
        A new dict of ``{name: image_config}``. Manifest entries override
        built-ins on a name collision; built-in names not redefined by the
        manifest remain resolvable.
    """
    catalog: dict[str, dict[str, Any]] = {
        name: dict(cfg) for name, cfg in BUILTIN_IMAGES.items()
    }
    if manifest_images:
        for name, cfg in manifest_images.items():
            catalog[name] = dict(cfg)
    return catalog


def resolve_image_entry(
    distro: str, catalog: dict[str, dict[str, Any]]
) -> CatalogEntry:
    """Resolve a ``VM_DISTRO`` value against a merged catalog.

    Matching is case-insensitive. ``os_variant`` and ``default_username``
    are taken from the source dict when present, else derived from the key.

    Args:
        distro: The user-supplied ``VM_DISTRO`` value.
        catalog: A merged catalog from :func:`resolve_catalog`.

    Returns:
        A fully-populated :class:`CatalogEntry`.

    Raises:
        ValueError: ``distro`` isn't in the catalog (message lists the
            available names).
    """
    index = {name.lower(): name for name in catalog}
    real_key = index.get(distro.lower())
    if real_key is None:
        available = ", ".join(sorted(catalog))
        raise ValueError(f"Unknown distro '{distro}'. Available: {available}")
    cfg = catalog[real_key]
    return CatalogEntry(
        image_url=cfg["image_url"],
        checksum_url=cfg.get("checksum_url"),
        checksum_type=cfg.get("checksum_type"),
        checksum_url_gpg=cfg.get("checksum_url_gpg"),
        network_version=cfg.get("network_version", 2),
        os_variant=derive_os_variant(real_key, cfg.get("os_variant")),
        default_username=derive_username(real_key, cfg.get("username")),
    )


def load_manifest_images(config_path: Path | None = None) -> dict[str, Any] | None:
    """Return the ``images:`` map from an ``Lvlab.yml``, if present.

    Args:
        config_path: An explicit manifest path (``--config``), or ``None``
            to look for ``Lvlab.yml`` in the current directory.

    Returns:
        The ``images:`` dict when a manifest is found and parses, or
        ``None`` when no manifest exists (cwd lookup only).

    Raises:
        ValueError: A manifest exists but couldn't be parsed, or an
            explicit ``--config`` path was given but not found.
    """
    fpath = str(config_path) if config_path is not None else None
    try:
        parsed = parse_config(fpath)
    except (KeyError, IndexError, TypeError, AttributeError, yaml.YAMLError) as exc:
        where = f"'{config_path}'" if config_path is not None else "Lvlab.yml"
        raise ValueError(f"Found {where} but couldn't parse it: {exc}") from exc
    if parsed is None:
        if config_path is not None:
            raise ValueError(f"Config file '{config_path}' does not exist.")
        return None
    _environment, images, _config_defaults, _machines = parsed
    return images


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


def parse_ip4_option(value: str, default_network: str) -> tuple[str, str]:
    """Split a ``--ip4`` argument into ``(network_name, raw_ip)``.

    Accepts either bare ``"IP"`` (uses ``default_network``) or
    ``"NETWORK,IP"``.

    Args:
        value: The raw value from ``--ip4``.
        default_network: Network to assume when ``value`` is bare.

    Returns:
        ``(network_name, raw_ip)``.

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
        return network, raw_ip
    return default_network, value.strip()


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
    typer.secho(message, fg=typer.colors.RED)
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
    if not cloud_image.exists_locally("image"):
        if not cloud_image.download_image():
            _fail(f"Failed to download cloud image from {cloud_image.image_url}")
    if cloud_image.checksum_url and not cloud_image.exists_locally("checksum"):
        cloud_image.download_checksum()
    if cloud_image.checksum_url_gpg and not cloud_image.exists_locally("checksum_gpg"):
        cloud_image.download_checksum_gpg()
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
    typer.secho("Initializing cloud images...", fg=typer.colors.GREEN)
    for name in sorted(catalog):
        entry = resolve_image_entry(name, catalog)
        cloud_image = _build_cloud_image(name, entry, _CLOUD_IMAGE_BASEDIR)
        _ensure_image_available(cloud_image)


# ---------------------------------------------------------------------------
# Context assembly (image + network + credentials)
# ---------------------------------------------------------------------------


@dataclass
class _CreateVmContext:
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
    authorized_keys: list[str] = field(default_factory=list)


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
    """Return the validated static CIDR for ``raw_ip``, or ``None`` for DHCP."""
    if raw_ip is None:
        return None
    vm_ip = ensure_cidr(raw_ip, netmask)
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
    vm_distro: str,
    ip4: str | None,
    network_name: str | None,
    netmask: str,
    memory: str,
    public_key: Path | None,
) -> _CreateVmContext:
    """Resolve image, network, addressing, and credentials.

    Every failure mode raises a typed exception the command body maps to a
    clean ``_fail``: :class:`DependencyError`, :class:`ValueError` (unknown
    distro / bad IP / bad memory), :class:`LibvirtNetworkError`,
    :class:`PasswordHashError`, :class:`PublicKeyError`.
    """
    check_createvm_tooling()
    entry = resolve_image_entry(vm_distro, catalog)

    resolved_network, raw_ip = _resolve_network_and_ip(
        ip4=ip4, network_name=network_name, default_network=DEFAULT_NETWORK
    )
    network_info = get_network_info(_SYSTEM_URI, resolved_network)
    dns_servers, gateway, search_domains = resolve_network_settings(network_info)
    vm_ip = _resolve_static_vm_ip(
        raw_ip=raw_ip, netmask=netmask, network_info=network_info
    )

    memory_mib = parse_memory_to_mib(memory)
    password_plain = generate_password_phrase()
    password_hash = hash_password_sha512(password_plain)
    authorized_keys = _resolve_authorized_keys(public_key)
    if not authorized_keys:
        raise PublicKeyError(
            "No SSH public keys discovered and none supplied via --public-key. "
            "Refusing to create a VM with no way to log in."
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
        authorized_keys=authorized_keys,
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
        username=ctx.entry.default_username,
        ssh_public_keys=ctx.authorized_keys,
        password_hash=ctx.password_hash,
    )

    iface: dict[str, Any] = {"name": "eth0"}
    nameservers: dict[str, Any] = {}
    if ctx.vm_ip is not None:
        iface["ip4"] = ctx.vm_ip
        iface["ip4gw"] = ctx.gateway
        nameservers = {"addresses": ctx.dns_servers, "search": ctx.search_domains}

    network_config = NetworkConfig(ctx.entry.network_version, [iface], nameservers)

    meta_data_path = vm_dir / "meta-data"
    user_data_path = vm_dir / "user-data"
    network_config_path = vm_dir / "network-config"
    cidata_path = vm_dir / "cidata.iso"

    network_config_path.write_text(network_config.render_config(), encoding="utf-8")
    user_data_path.write_text(oneoff.render_user_data(), encoding="utf-8")
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


def _virt_install_argv(
    *,
    vm_name: str,
    memory_mib: str,
    cpu: str,
    disk_path: Path,
    cidata_path: Path,
    os_variant: str,
    network_name: str,
) -> list[str]:
    """Build the ``virt-install`` argument vector (managed network, spice)."""
    try:
        resolved_variant, fallback_reason = resolve_os_variant(os_variant)
    except OsInfoLookupError as exc:
        typer.secho(
            f"warning: could not resolve --os-variant against osinfo-db ({exc}); "
            f"using requested {os_variant!r} as-is",
            fg=typer.colors.YELLOW,
        )
        resolved_variant = os_variant
    else:
        if fallback_reason:
            typer.secho(f"warning: {fallback_reason}", fg=typer.colors.YELLOW)

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
        f"network={network_name},model=virtio",
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
    typer.secho(f"Using image: {ctx.cloud_image.image_fpath}", fg=typer.colors.GREEN)
    typer.secho(f"Using os-variant: {ctx.entry.os_variant}", fg=typer.colors.GREEN)

    cidata_path = _render_cloud_init(vm_dir=vm_dir, vm_name=vm_name, ctx=ctx)
    disk_path = vm_dir / "disk0.qcow2"

    typer.secho("Copying base image...", fg=typer.colors.GREEN)
    shutil.copyfile(ctx.cloud_image.image_fpath, disk_path)

    typer.secho(f"Resizing disk to {disk_size}...", fg=typer.colors.GREEN)
    _run_cmd(["qemu-img", "resize", str(disk_path), disk_size])

    typer.secho("Starting install...", fg=typer.colors.GREEN)
    _run_cmd(
        _virt_install_argv(
            vm_name=vm_name,
            memory_mib=ctx.memory_mib,
            cpu=cpu,
            disk_path=disk_path,
            cidata_path=cidata_path,
            os_variant=ctx.entry.os_variant,
            network_name=ctx.network_name,
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
        typer.secho(
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


def _lookup_vm_mac(vm_name: str, network_name: str) -> str | None:
    """Return the VM MAC for the selected libvirt network, when available."""
    try:
        result = run_virsh(_SYSTEM_URI, ["domiflist", vm_name], check=False)
    except VirshError:
        return None
    if result.returncode != 0:
        return None

    target_network = network_name.lower()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        # domiflist row layout: Interface Type Source Model MAC
        source = fields[2].lower()
        mac = fields[4].lower()
        if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
            continue
        if source == target_network:
            return mac

    for line in result.stdout.splitlines():
        match = re.search(r"((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})", line)
        if match is not None:
            return match.group(1).lower()
    return None


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
    username = ctx.entry.default_username

    if manifest_path is not None:
        typer.secho(f"Using config: {manifest_path}", fg=typer.colors.BLUE)

    typer.secho("VM creation completed.", fg=typer.colors.GREEN)
    typer.echo()
    typer.secho(
        "One-time VM password (shown once and not retrievable later):",
        fg=typer.colors.YELLOW,
    )
    typer.secho(ctx.password_plain, fg=typer.colors.YELLOW)
    typer.echo()

    if ctx.vm_ip is not None:
        static_ip = ctx.vm_ip.split("/", maxsplit=1)[0]
        typer.secho("Example SSH command:", fg=typer.colors.BLUE)
        typer.secho(f"  $ ssh {username}@{static_ip}", fg=typer.colors.GREEN)
        typer.echo()
        return

    if ctx.forward_mode.lower() != "nat":
        typer.secho(
            f"Skipping libvirt DHCP lease wait for network '{ctx.network_name}' "
            f"(forward mode: {ctx.forward_mode}).",
            fg=typer.colors.BLUE,
        )
        typer.secho(
            "This network relies on external DHCP, so virsh lease queries may not "
            "show the VM address.",
            fg=typer.colors.YELLOW,
        )
        typer.secho(
            "Check your upstream DHCP service or DNS to find the assigned IP.",
            fg=typer.colors.YELLOW,
        )
        typer.echo()
        return

    vm_mac = _lookup_vm_mac(vm_name, ctx.network_name)
    lease_ip = _wait_for_dhcp_lease(
        vm_hostname,
        ctx.network_name,
        vm_mac=vm_mac,
        timeout_seconds=NAT_DHCP_LEASE_WAIT_SECONDS,
    )
    if lease_ip is not None:
        typer.secho(
            f"DHCP lease detected for {vm_hostname}: {lease_ip}", fg=typer.colors.GREEN
        )
        typer.echo()
        ssh_ip = lease_ip.split("/", maxsplit=1)[0]
        typer.secho("Example SSH command:", fg=typer.colors.BLUE)
        typer.secho(f"  $ ssh {username}@{ssh_ip}", fg=typer.colors.GREEN)
        typer.echo()
        return

    typer.secho(
        f"No DHCP lease was found for '{vm_hostname}' on network "
        f"'{ctx.network_name}' within {NAT_DHCP_LEASE_WAIT_SECONDS} seconds.",
        fg=typer.colors.YELLOW,
    )
    typer.secho(
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
        help="Static IPv4 address: IP or NETWORK,IP. Omit to use DHCP.",
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
    public_key: Path | None = typer.Option(
        None,
        "--public-key",
        help="Path to an additional SSH public key to append.",
    ),
    init_cloud_images: bool = typer.Option(
        False,
        "--init-cloud-images",
        help="Download any missing cloud images before VM creation.",
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
    has_vm_args = vm_name is not None or vm_distro is not None
    has_all_vm_args = vm_name is not None and vm_distro is not None

    if has_vm_args and not has_all_vm_args:
        _fail("VM_NAME and VM_DISTRO must be provided together.")
    if not init_cloud_images and not has_all_vm_args:
        _fail("Missing required arguments: VM_NAME and VM_DISTRO.")

    try:
        catalog = resolve_catalog(load_manifest_images(config_path))
    except ValueError as exc:
        _fail(str(exc))

    if init_cloud_images:
        _initialize_cloud_images(catalog)
        if not has_all_vm_args:
            typer.secho("Cloud images initialized.", fg=typer.colors.GREEN)
            return

    # vm_name / vm_distro guaranteed by the validation above.
    assert vm_name is not None and vm_distro is not None

    typer.echo()
    _ensure_storage_root_writable(storage_root)

    try:
        ctx = _build_createvm_context(
            catalog=catalog,
            vm_distro=vm_distro,
            ip4=ip4,
            network_name=network_name,
            netmask=netmask,
            memory=memory,
            public_key=public_key,
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
