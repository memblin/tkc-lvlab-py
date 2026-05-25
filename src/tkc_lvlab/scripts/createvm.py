"""Standalone ``createvm`` console script — one-off VM creation.

The orchestrator that turns a single command-line into a fully-deployed
libvirt VM. The script:

- names the libvirt domain with the **raw** ``vm_name`` you pass (no
    prefix), matching the ``lvscripts-py`` reference. ``destroyvm`` undoes
    it by the same raw name;
- resolves ``--distro`` against a merged image catalog: the built-in
    :data:`BUILTIN_IMAGES` plus the ``images:`` section of an ``Lvlab.yml``
    in the current directory, if one is present. Manifest entries win on a
    name collision; built-in names still resolve when the manifest doesn't
    redefine them;
- caches cloud images under ``/var/lib/libvirt/images/lvlab/cloud-images``
    — the same cache ``lvlab up`` uses, so an image downloaded by either
    path is reused by the other;
- writes per-VM state under ``/var/lib/libvirt/images/lvlab/oneoff/<vm_name>/``;
- defaults to ``qemu:///system`` (most lab setups), with a ``--uri`` flag
    to override;
- produces a standalone qcow2 by default (``--copy``: cp + resize, no
    cloud-images dependency — the right default for throwaway one-off
    VMs); ``--no-copy`` opts into the qemu-img backing-file strategy
    (storage-efficient but tied to the cached image). ``lvlab up`` keeps
    the backing-file strategy.

``--distro`` matches case-insensitively. The script keeps a minimal
``import click`` for ``click.Choice`` on ``--network-type`` and
``click.BadParameter`` in ``--ip4`` parsing. ``run = app`` is kept as a
backwards-compat alias for the entry point and tests.

Wired up as a console script via
``[project.scripts] createvm = "tkc_lvlab.scripts.createvm:run"``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import click
import typer
import yaml

from ..config import parse_config
from ..utils.cloud_init import CloudInitIso, NetworkConfig
from ..utils.images import CloudImage
from ..utils.network import (
    NETWORK_TYPES,
    USER_MODE_NETWORK_TYPES,
    LibvirtNetworkError,
    get_network_info,
    resolve_network_settings,
    validate_static_ip,
)
from ..utils.passwords import (
    PasswordHashError,
    generate_password_phrase,
    hash_password_sha512,
)
from ..utils.osinfo import OsInfoLookupError, resolve_os_variant
from ..utils.requirements import DependencyError, check_createvm_tooling
from ..utils.subprocess_env import system_first_env
from ..utils.ssh_keys import (
    PublicKeyError,
    dedupe_public_keys,
    discover_default_public_keys,
    load_public_key,
)
from ..utils.standalone_cloud_init import OneoffCloudInit


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
        checksum_url_gpg: Optional URL to a GPG keyring for verifying
            the checksum file. When ``None``, GPG verification is
            skipped (still strongly recommended where the upstream
            provides one).
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


# Built-in cloud images the standalone ``createvm`` script can resolve
# via ``--distro`` out of the box. Each value uses the **same schema as an
# ``Lvlab.yml`` ``images:`` entry** so the built-in catalog and a cwd
# manifest merge through one code path (:func:`resolve_catalog`). The two
# createvm-only knobs — ``os_variant`` and ``username`` — are intentionally
# omitted here: they're derived from the key (debian12 -> debian12 /
# debian) and only need to appear in a dict when the derivation is wrong.
#
# Entries point at specific pinned point releases; when a distro release
# goes EOL its URL eventually 404s (the checksum-verification path catches
# that). The ``refresh-cloud-images`` skill under ``.claude/skills/`` keeps
# these in sync with upstream and surfaces new-major / EOL drift.
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


# First-boot account names keyed by distro family. Cloud images ship a
# distro-conventional default user; this map covers the families lvlab
# targets. Anything not listed falls back to the family token itself
# (e.g. ``alpine318`` -> ``alpine``), and any entry can be overridden per
# image with a ``username:`` key in the catalog dict / manifest.
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
        key: A catalog / ``--distro`` key.

    Returns:
        The family token, lower-cased.
    """
    match = re.match(r"[a-z]+", key.lower())
    return match.group(0) if match else key.lower()


def derive_os_variant(key: str, explicit: str | None) -> str:
    """Resolve the ``--os-variant`` value for a catalog key.

    Args:
        key: The catalog / ``--distro`` key (e.g. ``debian12-salt``).
        explicit: An ``os_variant`` provided by the catalog dict /
            manifest, or ``None`` to derive.

    Returns:
        ``explicit`` when set; otherwise the segment of ``key`` before
        the first ``-`` (``debian12-salt`` -> ``debian12``). The deploy
        path runs this through ``resolve_os_variant`` for osinfo-db
        fuzzy fallback, so an approximate value is fine.
    """
    return explicit if explicit else key.split("-")[0]


def derive_username(key: str, explicit: str | None) -> str:
    """Resolve the first-boot username for a catalog key.

    Args:
        key: The catalog / ``--distro`` key.
        explicit: A ``username`` provided by the catalog dict /
            manifest, or ``None`` to derive.

    Returns:
        ``explicit`` when set; otherwise the family-conventional user
        from :data:`_USERNAME_BY_FAMILY`, falling back to the family
        token itself.
    """
    if explicit:
        return explicit
    family = _family_token(key)
    return _USERNAME_BY_FAMILY.get(family, family)


def resolve_catalog(
    manifest_images: dict[str, Any] | None
) -> dict[str, dict[str, Any]]:
    """Merge the built-in catalog with a manifest's ``images:`` section.

    Args:
        manifest_images: The ``images:`` map from a cwd ``Lvlab.yml``,
            or ``None`` when no manifest is present.

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
    """Resolve a ``--distro`` value against a merged catalog.

    Matching is case-insensitive. ``os_variant`` and ``default_username``
    are taken from the source dict when present, else derived from the
    key.

    Args:
        distro: The user-supplied ``--distro`` value.
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


def load_manifest_images() -> dict[str, Any] | None:
    """Return the ``images:`` map from a cwd ``Lvlab.yml``, if present.

    Returns:
        The ``images:`` dict when an ``Lvlab.yml`` is found and parses,
        or ``None`` when no manifest exists in the current directory.

    Raises:
        ValueError: An ``Lvlab.yml`` exists but couldn't be parsed.
    """
    try:
        parsed = parse_config()
    except (KeyError, IndexError, TypeError, AttributeError, yaml.YAMLError) as exc:
        raise ValueError(f"Found Lvlab.yml but couldn't parse it: {exc}") from exc
    if parsed is None:
        return None
    _environment, images, _config_defaults, _machines = parsed
    return images


# ---------------------------------------------------------------------------
# Storage path conventions
# ---------------------------------------------------------------------------


# Per-VM state (disk, cidata.iso, cloud-init files) lands under this root,
# namespaced beside the shared cloud-image cache at
# ``/var/lib/libvirt/images/lvlab/cloud-images``. It is deliberately
# distinct from ``lvlab up``'s ``lvlab/<env>/<vm>/`` layout, so one-off VMs
# never collide with manifest VM disks. The libvirt domain itself is the
# raw ``vm_name`` you pass — ``destroyvm`` undoes it by that same name.
_ONEOFF_STORAGE_ROOT = Path("/var/lib/libvirt/images/lvlab/oneoff")


def storage_dir_for(vm_name: str, root: Path = _ONEOFF_STORAGE_ROOT) -> Path:
    """Return the per-VM storage directory under the one-off root.

    Args:
        vm_name: The user-supplied short name.
        root: Override the storage root (test seam — production
            callers should use the default).

    Returns:
        ``<root>/<vm_name>``.
    """
    return root / vm_name


# ---------------------------------------------------------------------------
# Argument parsers (shared with future ``destroyvm``)
# ---------------------------------------------------------------------------


def parse_ip4_option(value: str, default_network: str) -> tuple[str, str]:
    """Split a ``--ip4`` argument into ``(network_name, ip)``.

    Accepts either bare ``"IP"`` (uses ``default_network``) or
    ``"NETWORK,IP"``.

    Args:
        value: The raw value from ``--ip4``.
        default_network: Network to assume when ``value`` is bare.

    Returns:
        ``(network_name, raw_ip)``.

    Raises:
        click.BadParameter: ``value`` has a comma but either side is
            empty.
    """
    if "," in value:
        network, _, raw_ip = value.partition(",")
        network = network.strip()
        raw_ip = raw_ip.strip()
        if not network or not raw_ip:
            raise click.BadParameter(
                f"Invalid --ip4 value '{value}'. Expected IP or NETWORK,IP."
            )
        return network, raw_ip
    return default_network, value.strip()


def _collect_ssh_keys(public_key_path: Path | None) -> list[str]:
    """Discover default SSH keys and (optionally) append ``--public-key``.

    Args:
        public_key_path: Optional path to a key file the user provided
            via ``--public-key``. Validated and appended after discovery
            output.

    Returns:
        Deduplicated, order-preserving list of validated key strings.

    Raises:
        PublicKeyError: ``--public-key`` was provided and failed
            validation.
    """
    keys = list(discover_default_public_keys())
    if public_key_path is not None:
        keys.append(load_public_key(public_key_path))
    return dedupe_public_keys(keys)


# ---------------------------------------------------------------------------
# Disk creation
# ---------------------------------------------------------------------------


def _create_disk(
    *,
    image_path: Path,
    disk_path: Path,
    size: str,
    copy_strategy: bool,
) -> None:
    """Create the per-VM qcow2 disk.

    ``copy_strategy=True`` (the ``createvm`` default): ``cp`` the cloud
    image then ``qemu-img resize`` it to ``<size>``. Standalone qcow2,
    no cloud-images dependency, but takes the full image size.

    ``copy_strategy=False``: ``qemu-img create -F qcow2 -b <image_path>
    -f qcow2 <disk_path> <size>`` — backing-file mode, storage-efficient
    but ties the VM to the cloud image's lifetime.

    Args:
        image_path: Verified cloud image on disk.
        disk_path: Output qcow2 file.
        size: qemu-img size string (e.g. ``20G``).
        copy_strategy: ``True`` to use ``cp + resize``; ``False`` for
            backing file.

    Raises:
        typer.Exit: ``qemu-img`` (or ``cp``) exited non-zero.
    """
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    if copy_strategy:
        try:
            shutil.copyfile(image_path, disk_path)
        except OSError as exc:
            raise _fail(f"Failed to copy cloud image to {disk_path}: {exc}") from exc
        _run_subprocess(["qemu-img", "resize", str(disk_path), size])
        return

    # Backing-file mode.
    _run_subprocess(
        [
            "qemu-img",
            "create",
            "-F",
            "qcow2",
            "-b",
            str(image_path),
            "-f",
            "qcow2",
            str(disk_path),
            size,
        ]
    )


def _run_subprocess(argv: list[str]) -> None:
    """Run a subprocess with check=True, translating errors via :func:`_fail`.

    The environment is set via :func:`system_first_env` so that any
    binary using a ``#!/usr/bin/env python3`` shebang (e.g.
    ``virt-install`` on Debian 13) resolves the interpreter to the
    host's system Python instead of the venv-shadowed one. Without
    this override, virt-install fails to import ``gi`` from the
    system ``python3-gi`` package because the venv interpreter
    doesn't see system site-packages.

    Args:
        argv: Command line, first element is the binary name.

    Raises:
        typer.Exit: Non-zero exit code on subprocess failure; the
            stderr text is folded into the error message printed
            before exit so the operator sees the real failure.
    """
    try:
        subprocess.run(
            argv, check=True, capture_output=True, text=True, env=system_first_env()
        )
    except FileNotFoundError as exc:
        raise _fail(f"{argv[0]} not found in PATH.") from exc
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or exc.stdout or "unknown error").strip()
        raise _fail(f"{argv[0]} {' '.join(argv[1:])} failed: {msg}") from exc


# ---------------------------------------------------------------------------
# virt-install
# ---------------------------------------------------------------------------


def _virt_install(
    *,
    uri: str,
    domain_name: str,
    memory_mib: int,
    cpus: int,
    disk_path: Path,
    cidata_path: Path,
    os_variant: str,
    network_name: str,
    network_type: str,
) -> None:
    """Build and run the ``virt-install`` command for a one-off VM.

    Args:
        uri: libvirt URI to deploy against.
        domain_name: The raw ``vm_name`` (no prefix).
        memory_mib: Guest memory in MiB.
        cpus: vCPU count.
        disk_path: Primary qcow2 disk.
        cidata_path: cloud-init seed ISO (attached as cdrom).
        os_variant: ``virt-install --os-variant`` value.
        network_name: libvirt network name (only used when
            ``network_type == "network"``).
        network_type: ``"network"`` (default — managed libvirt network),
            ``"user"`` (SLIRP), or ``"passt"``. The user-mode forms are
            primarily for ``qemu:///session`` and ignore
            ``network_name``.

    Raises:
        typer.Exit: ``virt-install`` exited non-zero.
    """
    if network_type == "user":
        network_arg = "user,model=virtio"
    elif network_type == "passt":
        network_arg = "passt,model=virtio"
    else:
        network_arg = f"network={network_name},model=virtio"

    try:
        resolved_variant, fallback_reason = resolve_os_variant(os_variant)
    except OsInfoLookupError as exc:
        typer.echo(
            f"warning: could not resolve --os-variant against osinfo-db ({exc}); "
            f"using requested {os_variant!r} as-is",
            err=True,
        )
        resolved_variant = os_variant
    else:
        if fallback_reason:
            typer.echo(f"warning: {fallback_reason}", err=True)

    argv = [
        "virt-install",
        f"--connect={uri}",
        f"--name={domain_name}",
        f"--memory={memory_mib}",
        f"--vcpus={cpus}",
        "--import",
        "--disk",
        f"path={disk_path}",
        "--disk",
        f"path={cidata_path},device=cdrom",
        f"--os-variant={resolved_variant}",
        "--network",
        network_arg,
        "--graphics",
        "vnc,listen=0.0.0.0",
        "--noautoconsole",
    ]
    _run_subprocess(argv)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


_DEFAULT_URI = "qemu:///system"
_DEFAULT_NETWORK = "default"
_DEFAULT_NETWORK_TYPE = "network"
_DEFAULT_MEMORY_MIB = 2048
_DEFAULT_CPUS = 2
_DEFAULT_DISK_SIZE = "20G"


app = typer.Typer(
    help="Create a one-off libvirt VM from a built-in cloud image.",
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _fail(message: str) -> typer.Exit:
    """Print ``Error: <message>`` to stderr and return an ``Exit(1)``.

    Returning (rather than raising) lets callers preserve the original
    exception's cause via ``raise _fail(msg) from exc``.
    """
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(code=1)


def _ensure_storage_root_writable(storage_root: Path) -> None:
    """Verify ``createvm`` can create the per-VM storage directory.

    Walks up to the nearest existing ancestor of ``storage_root`` and
    checks it's writable. This fails fast with actionable guidance
    before any image download, rather than deep inside ``mkdir``.

    Args:
        storage_root: The per-VM storage root (``--storage-root``).

    Raises:
        typer.Exit: The nearest existing ancestor denies write. The
            message points at the expected ``libvirt`` group membership
            and ``root:libvirt 0771`` permissions on
            ``/var/lib/libvirt/images``.
    """
    probe = storage_root
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    if not os.access(probe, os.W_OK):
        raise _fail(
            f"Cannot write under {storage_root} (nearest existing ancestor "
            f"{probe} is not writable). Ensure your user is in the 'libvirt' "
            f"group and that /var/lib/libvirt/images is root:libvirt mode 0771 "
            f"so group members can create sub-directories — e.g. "
            f"`sudo usermod -aG libvirt $USER` (then re-login) and "
            f"`sudo chmod 0771 /var/lib/libvirt/images`."
        )


def _createvm_resolve_network(
    network: str, network_type: str, ip4: str | None, uri: str
) -> tuple[str, str]:
    """Validate the network selection plus optional static ``--ip4``.

    User-mode networking (SLIRP / passt) bypasses libvirt's managed
    networks and ignores static IPs — so ``--ip4`` is rejected upfront
    with a clear message before any state is created. The managed
    "network" type runs the full introspection: ``--ip4`` is parsed,
    the libvirt network is looked up, the IP is validated against the
    network's subnet + DHCP range, and the policy resolver runs to
    surface bridge-without-defaults errors before write.

    Returns:
        Tuple of (network_name, network_type_normalized). The
        network_name is what virt-install will see; the normalized
        type is the lowercased ``--network-type``.

    Raises:
        typer.Exit: With code 1 on any user-input or libvirt-network
            validation failure.
    """
    network_type_normalized = network_type.lower()
    if network_type_normalized in USER_MODE_NETWORK_TYPES:
        if ip4 is not None:
            raise _fail(
                f"--ip4 is not supported with --network-type {network_type_normalized}. "
                f"User-mode networking (SLIRP/passt) does not honour static IPs."
            )
        # network_name is unused at virt-install time for user-mode but the
        # caller threads it through anyway; keep the value the operator passed.
        return network, network_type_normalized

    if ip4 is not None:
        network_name, raw_ip = parse_ip4_option(ip4, network)
    else:
        network_name, raw_ip = network, None
    try:
        net_info = get_network_info(uri, network_name)
        if raw_ip is not None:
            validate_static_ip(raw_ip, net_info)
        # Resolve policy for completeness — surfaces bridge-without-defaults
        # errors before any state is written. Returns are unused in this
        # iteration; cloud-init network-config rendering uses them in a
        # follow-up commit.
        resolve_network_settings(net_info)
    except (LibvirtNetworkError, ValueError) as exc:
        raise _fail(str(exc)) from exc
    return network_name, network_type_normalized


def _createvm_render_cloud_init(
    vm_dir: Path,
    vm_name: str,
    domain_name: str,
    entry: CatalogEntry,
    ssh_keys: list[str],
    password_hash: str,
) -> Path:
    """Render meta-data / user-data / network-config and pack the cidata ISO.

    Writes the three cloud-init source files into ``vm_dir`` and packs
    them with ``pycdlib`` via :class:`CloudInitIso`. The network-config
    uses a default-shaped ``eth0`` interface so cloud-init brings the
    NIC up via DHCP on NAT networks; static-IP rendering is a follow-up
    once the Step-3 validation feeds into ``NetworkConfig``.

    Returns:
        The path to the freshly-written ``cidata.iso``.

    Raises:
        typer.Exit: With code 1 if ``CloudInitIso.write`` reports failure.
    """
    oneoff = OneoffCloudInit(
        libvirt_vm_name=domain_name,
        hostname=vm_name.split(".")[0],
        fqdn=vm_name,
        username=entry.default_username,
        ssh_public_keys=ssh_keys,
        password_hash=password_hash,
    )

    network_config_path = vm_dir / "network-config"
    user_data_path = vm_dir / "user-data"
    meta_data_path = vm_dir / "meta-data"
    cidata_path = vm_dir / "cidata.iso"

    iface = {"name": "eth0"}
    network_config = NetworkConfig(entry.network_version, [iface], {})
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
        raise _fail("Failed to build cidata.iso.")
    return cidata_path


@app.command()
def createvm(  # pylint: disable=too-many-arguments,too-many-locals
    vm_name: str = typer.Argument(..., help="Short name for the one-off VM."),
    distro: str = typer.Option(
        ...,
        "--distro",
        help=(
            "Cloud image to use (case-insensitive). Resolves against the "
            "built-in catalog merged with the 'images:' section of an "
            "Lvlab.yml in the current directory, if present."
        ),
    ),
    memory: int = typer.Option(_DEFAULT_MEMORY_MIB, "--memory", show_default=True),
    cpu: int = typer.Option(_DEFAULT_CPUS, "--cpu", show_default=True),
    disk_size: str = typer.Option(_DEFAULT_DISK_SIZE, "--disk-size", show_default=True),
    network: str = typer.Option(
        _DEFAULT_NETWORK,
        "--network",
        show_default=True,
        help="libvirt network name. NAT default uses 'default'; bridges need explicit name.",
    ),
    network_type: str = typer.Option(
        _DEFAULT_NETWORK_TYPE,
        "--network-type",
        click_type=click.Choice(NETWORK_TYPES, case_sensitive=False),
        show_default=True,
        help=(
            "Network attachment mode. 'network' (default) uses a managed "
            "libvirt network named via --network. 'user' / 'passt' use "
            "virt-install's user-mode forms — required for qemu:///session "
            "where rootless libvirt cannot manage a NAT network. --ip4 is "
            "rejected with user-mode."
        ),
    ),
    ip4: str = typer.Option(
        None,
        "--ip4",
        help=(
            "Static IPv4. Accepts 'IP' (uses --network) or 'NETWORK,IP'. "
            "Validated against the network's subnet AND DHCP range."
        ),
    ),
    public_key: Path = typer.Option(
        None,
        "--public-key",
        exists=True,
        dir_okay=False,
        help="Path to an additional SSH public key (appended after discovered defaults).",
    ),
    copy_strategy: bool = typer.Option(
        True,
        "--copy/--no-copy",
        help=(
            "Disk strategy. Default --copy: a standalone qcow2 (cp + qemu-img "
            "resize) independent of the cloud-images cache, so you can wipe and "
            "re-init that cache without breaking this VM. --no-copy uses "
            "qemu-img create -b backing-file (storage-efficient, but ties the "
            "VM to the cached image)."
        ),
    ),
    uri: str = typer.Option(
        _DEFAULT_URI, "--uri", show_default=True, help="libvirt connection URI."
    ),
    storage_root: Path = typer.Option(
        _ONEOFF_STORAGE_ROOT,
        "--storage-root",
        show_default=True,
        file_okay=False,
        help="Override the per-VM storage root (test seam).",
    ),
) -> None:
    """Create a one-off libvirt VM named ``VM_NAME`` from a cloud image.

    The libvirt domain is the raw ``VM_NAME`` you pass. ``--distro``
    resolves against the built-in catalog merged with the ``images:``
    section of an ``Lvlab.yml`` in the current directory (manifest entries
    win on a name collision). Storage lands under ``<storage-root>/<VM_NAME>/``
    (default ``/var/lib/libvirt/images/lvlab/oneoff/``).
    """
    domain_name = vm_name
    vm_dir = storage_dir_for(vm_name, root=storage_root)

    typer.echo(f"Creating VM '{domain_name}' under {vm_dir}", err=True)

    # Step 1: dependency precheck + storage writability.
    try:
        check_createvm_tooling()
    except DependencyError as exc:
        raise _fail(str(exc)) from exc
    _ensure_storage_root_writable(storage_root)

    # Step 2: resolve image against built-ins merged with a cwd Lvlab.yml.
    # Use lvlab's CloudImage for download + verify, sharing the lvlab cache.
    try:
        catalog = resolve_catalog(load_manifest_images())
        entry = resolve_image_entry(distro, catalog)
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    image_dir = Path("/var/lib/libvirt/images/lvlab")
    cloud_image = _build_cloud_image(distro, entry, image_dir)
    _ensure_image_available(cloud_image)

    # Step 3: network + IP validation.
    network_name, network_type_normalized = _createvm_resolve_network(
        network, network_type, ip4, uri
    )

    # Step 4: SSH keys.
    try:
        ssh_keys = _collect_ssh_keys(public_key)
    except PublicKeyError as exc:
        raise _fail(str(exc)) from exc
    if not ssh_keys:
        raise _fail(
            "No SSH public keys discovered and none supplied via --public-key. "
            "Refusing to create a VM with no way to log in."
        )

    # Step 5: password + hash.
    password = generate_password_phrase()
    try:
        password_hash_value = hash_password_sha512(password)
    except PasswordHashError as exc:
        raise _fail(str(exc)) from exc

    # Step 6: cloud-init render + ISO build.
    try:
        vm_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise _fail(
            f"Storage dir {vm_dir} already exists. Pick a different VM name or "
            "delete the directory first."
        ) from exc

    cidata_path = _createvm_render_cloud_init(
        vm_dir=vm_dir,
        vm_name=vm_name,
        domain_name=domain_name,
        entry=entry,
        ssh_keys=ssh_keys,
        password_hash=password_hash_value,
    )

    # Step 7: create the qcow2 disk.
    disk_path = vm_dir / "disk0.qcow2"
    _create_disk(
        image_path=Path(cloud_image.image_fpath),
        disk_path=disk_path,
        size=disk_size,
        copy_strategy=copy_strategy,
    )

    # Step 8: virt-install.
    try:
        _virt_install(
            uri=uri,
            domain_name=domain_name,
            memory_mib=memory,
            cpus=cpu,
            disk_path=disk_path,
            cidata_path=cidata_path,
            os_variant=entry.os_variant,
            network_name=network_name,
            network_type=network_type_normalized,
        )
    except typer.Exit:
        # Cleanup-on-failure: wipe the partial VM directory before
        # re-raising so the operator can retry without colliding.
        shutil.rmtree(vm_dir, ignore_errors=True)
        raise

    typer.echo(
        f"\nVM '{domain_name}' created. First-boot console password: {password}\n"
        f"SSH: ssh {entry.default_username}@<vm-ip>",
        err=False,
    )


def _build_cloud_image(name: str, entry: CatalogEntry, image_dir: Path) -> CloudImage:
    """Construct a :class:`CloudImage` from a catalog entry.

    Args:
        name: The catalog key (e.g. ``debian12``). Becomes the
            ``CloudImage.name`` attribute.
        entry: The catalog entry providing URLs and metadata.
        image_dir: Root directory under which the cloud image lives.
            ``CloudImage`` appends ``/cloud-images/`` itself.

    Returns:
        A :class:`CloudImage` configured to download to and read from
        ``image_dir/cloud-images/<filename>``.
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
            raise _fail(f"Failed to download cloud image from {cloud_image.image_url}")
    if cloud_image.checksum_url and not cloud_image.exists_locally("checksum"):
        cloud_image.download_checksum()
    if cloud_image.checksum_url_gpg and not cloud_image.exists_locally("checksum_gpg"):
        cloud_image.download_checksum_gpg()
    if cloud_image.checksum_url_gpg:
        cloud_image.gpg_verify_checksum_file()
    if cloud_image.checksum_url and not cloud_image.checksum_verify_image():
        raise _fail(
            f"Cloud image {cloud_image.image_fpath} failed checksum verification."
        )


# Backwards-compat alias for the entry point and external imports.
# pyproject.toml references "tkc_lvlab.scripts.createvm:run"; tests
# import ``run`` from this module. Typer ``app`` is callable, so the
# script entry point works identically.
run = app


if __name__ == "__main__":  # pragma: no cover
    app()
