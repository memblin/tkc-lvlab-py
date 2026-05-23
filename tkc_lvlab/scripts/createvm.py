"""Standalone ``createvm`` console script — one-off VM creation.

Phase 6 step 4. The orchestrator that turns a single command-line into a
fully-deployed libvirt VM without going through ``Lvlab.yml``. Per the
Phase 6 architecture lock the script:

- prefixes every libvirt domain name with ``oneoff-`` so it can never
    collide with an lvlab manifest VM's ``<vm_name>_<env>`` pattern;
- writes per-VM state under ``/var/lib/libvirt/images/oneoff/<vm_name>/``;
- defaults to ``qemu:///system`` (most lab setups), with a ``--uri`` flag
    to override;
- uses the lvlab-native qcow2 backing-file strategy by default, with an
    opt-in ``--copy`` flag that produces a standalone qcow2 with no
    cloud-images dependency (image-upgrade safety).

Phase 9 follow-up ported this file from Click to Typer. UX is
preserved: same positional, same 9 options, same defaults,
same ``case_sensitive=False`` matching on ``--distro``. The script
keeps a minimal ``import click`` for ``click.Choice`` (passed to
Typer via ``click_type=``) since Typer doesn't have a native
case-insensitive choice idiom. ``run = app`` is kept as a
backwards-compat alias for the entry point and tests.

Wired up as a console script via
``[project.scripts] createvm = "tkc_lvlab.scripts.createvm:run"``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import click
import typer

from ..utils.cloud_init import CloudInitIso, NetworkConfig
from ..utils.images import CloudImage
from ..utils.network import (
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
from ..utils.requirements import DependencyError, check_createvm_tooling
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
    """Built-in cloud-image catalog entry.

    Attributes:
        image_url: Direct URL to the qcow2 cloud image.
        checksum_url: URL to the checksum manifest. Required.
        checksum_type: Hash algorithm — ``sha256`` or ``sha512``.
        checksum_url_gpg: Optional URL to a GPG keyring for verifying
            the checksum file. When ``None``, GPG verification is
            skipped (still strongly recommended where the upstream
            provides one).
        network_version: cloud-init network-config schema version,
            ``1`` (ENI-style) or ``2`` (netplan-style).
        os_variant: ``virt-install --os-variant`` argument. Looked
            up via ``virt-install --os-variant list`` /
            ``osinfo-query os``.
        default_username: First-boot account name cloud-init creates.
    """

    image_url: str
    checksum_url: str
    checksum_type: str
    checksum_url_gpg: str | None
    network_version: int
    os_variant: str
    default_username: str


# Catalog of cloud-image URLs the standalone ``createvm`` script can
# resolve via ``--distro``. Entries point at specific pinned point
# releases; when a distro release goes EOL, its URL eventually returns
# a 404 HTML page (the checksum-verification path defensively catches
# that — discovered in the Phase 9 destructive smoke test on
# 2026-05-23, when Fedora 40's URL had aged out). The
# ``refresh-cloud-images`` skill under ``.claude/skills/`` keeps these
# in sync with upstream and surfaces new-major / EOL drift for user
# confirmation.
BUILTIN_IMAGES: dict[str, CatalogEntry] = {
    "debian12": CatalogEntry(
        image_url=(
            "https://cloud.debian.org/images/cloud/bookworm/20260518-2482/"
            "debian-12-generic-amd64-20260518-2482.qcow2"
        ),
        checksum_url=(
            "https://cloud.debian.org/images/cloud/bookworm/20260518-2482/" "SHA512SUMS"
        ),
        checksum_type="sha512",
        checksum_url_gpg=None,
        network_version=2,
        os_variant="debian12",
        default_username="debian",
    ),
    "debian13": CatalogEntry(
        image_url=(
            "https://cloud.debian.org/images/cloud/trixie/latest/"
            "debian-13-generic-amd64.qcow2"
        ),
        checksum_url=("https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS"),
        checksum_type="sha512",
        checksum_url_gpg=None,
        network_version=2,
        os_variant="debian13",
        default_username="debian",
    ),
    "fedora44": CatalogEntry(
        image_url=(
            "https://download.fedoraproject.org/pub/fedora/linux/releases/44/"
            "Cloud/x86_64/images/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2"
        ),
        checksum_url=(
            "https://download.fedoraproject.org/pub/fedora/linux/releases/44/"
            "Cloud/x86_64/images/Fedora-Cloud-44-1.7-x86_64-CHECKSUM"
        ),
        checksum_type="sha256",
        checksum_url_gpg="https://fedoraproject.org/fedora.gpg",
        network_version=2,
        os_variant="fedora44",
        default_username="fedora",
    ),
}


# ---------------------------------------------------------------------------
# Naming + storage path conventions (Phase 6 lock)
# ---------------------------------------------------------------------------


_ONEOFF_PREFIX = "oneoff-"
_ONEOFF_STORAGE_ROOT = Path("/var/lib/libvirt/images/oneoff")


def domain_name_for(vm_name: str) -> str:
    """Return the libvirt domain name a one-off VM gets.

    The ``oneoff-`` prefix is Phase 6's collision-prevention against
    lvlab manifest VMs (which use ``<vm_name>_<env>``).

    Args:
        vm_name: The user-supplied short name (e.g. ``testvm.local``).

    Returns:
        ``f"{_ONEOFF_PREFIX}{vm_name}"``.
    """
    return f"{_ONEOFF_PREFIX}{vm_name}"


def storage_dir_for(vm_name: str, root: Path = _ONEOFF_STORAGE_ROOT) -> Path:
    """Return the per-VM storage directory under the oneoff root.

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

    Default (``copy_strategy=False``): ``qemu-img create -F qcow2 -b <image_path> -f qcow2 <disk_path> <size>`` — backing-file mode, storage-efficient but ties the VM to the
    cloud image's lifetime.

    Opt-in (``copy_strategy=True``): ``cp`` the cloud image then
    ``qemu-img resize`` it to ``<size>``. Standalone qcow2, no cloud-images
    dependency, but takes the full image size.

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

    Args:
        argv: Command line, first element is the binary name.

    Raises:
        typer.Exit: Non-zero exit code on subprocess failure; the
            stderr text is folded into the error message printed
            before exit so the operator sees the real failure.
    """
    try:
        subprocess.run(argv, check=True, capture_output=True, text=True)
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
) -> None:
    """Build and run the ``virt-install`` command for a one-off VM.

    Args:
        uri: libvirt URI to deploy against.
        domain_name: ``oneoff-<vm_name>`` per the Phase 6 lock.
        memory_mib: Guest memory in MiB.
        cpus: vCPU count.
        disk_path: Primary qcow2 disk.
        cidata_path: cloud-init seed ISO (attached as cdrom).
        os_variant: ``virt-install --os-variant`` value.
        network_name: libvirt network name to attach NIC to.

    Raises:
        typer.Exit: ``virt-install`` exited non-zero.
    """
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
        f"--os-variant={os_variant}",
        "--network",
        f"network={network_name},model=virtio",
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


@app.command()
def createvm(  # pylint: disable=too-many-arguments,too-many-locals
    vm_name: str = typer.Argument(..., help="Short name for the one-off VM."),
    distro: str = typer.Option(
        ...,
        "--distro",
        click_type=click.Choice(sorted(BUILTIN_IMAGES.keys()), case_sensitive=False),
        help="Built-in cloud image to use.",
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
        False,
        "--copy",
        help=(
            "Create a standalone qcow2 via cp + qemu-img resize instead of the "
            "default qemu-img create -b backing-file. Lets you wipe and re-init "
            "the cloud-images directory without breaking this VM."
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
    """Create a one-off libvirt VM named ``VM_NAME`` from a built-in cloud image.

    The libvirt domain becomes ``oneoff-<VM_NAME>`` so it can't collide
    with an lvlab manifest VM. Storage lands under
    ``<storage-root>/<VM_NAME>/`` (default ``/var/lib/libvirt/images/oneoff/``).
    """
    domain_name = domain_name_for(vm_name)
    vm_dir = storage_dir_for(vm_name, root=storage_root)

    typer.echo(f"Creating one-off VM '{domain_name}' under {vm_dir}", err=True)

    # Step 1: dependency precheck.
    try:
        check_createvm_tooling()
    except DependencyError as exc:
        raise _fail(str(exc)) from exc

    # Step 2: resolve image. Use lvlab's CloudImage for download + verify.
    entry = BUILTIN_IMAGES[distro.lower()]
    image_dir = Path("/var/lib/libvirt/images")
    cloud_image = _build_cloud_image(distro, entry, image_dir)
    _ensure_image_available(cloud_image)

    # Step 3: network + IP validation.
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

    oneoff = OneoffCloudInit(
        libvirt_vm_name=domain_name,
        hostname=vm_name.split(".")[0],
        fqdn=vm_name,
        username=entry.default_username,
        ssh_public_keys=ssh_keys,
        password_hash=password_hash_value,
    )

    network_config_path = vm_dir / "network-config"
    user_data_path = vm_dir / "user-data"
    meta_data_path = vm_dir / "meta-data"
    cidata_path = vm_dir / "cidata.iso"
    disk_path = vm_dir / "disk0.qcow2"

    # network-config: use NetworkConfig with a default-shaped interface
    # so cloud-init brings up eth0 via DHCP on NAT networks; static IPs
    # via --ip4 are a follow-up (the validation above is already in
    # place, the rendering wiring lands with createvm hardening).
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
    if not iso.write(str(vm_dir)):
        raise _fail("Failed to build cidata.iso.")

    # Step 7: create the qcow2 disk.
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
