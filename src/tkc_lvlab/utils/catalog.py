"""Shared cloud-image catalog + entry resolution for both deploy paths.

A cloud image is described the same way everywhere — in the built-in
:data:`BUILTIN_IMAGES` catalog (defined here), in an ``Lvlab.yml``
``images:`` entry, and in the smoke manifest. This module turns one such
description plus its key into a fully-resolved :class:`ImageEntry`, with
two fields derived from the key unless the entry overrides them:

- ``os_variant`` — the ``virt-install --os-variant`` value. Derived as
    the key segment before the first ``-`` (``debian12-salt`` ->
    ``debian12``); the deploy path still runs it through
    :func:`tkc_lvlab.utils.osinfo.resolve_os_variant` for osinfo-db
    fuzzy fallback.
- ``default_username`` — the first-boot account cloud-init configures,
    from the family map (``fedora44`` -> ``fedora``).

Both the standalone ``createvm`` script and the manifest ``Machine``
path resolve through here so a custom or oddly-keyed image (e.g.
``ubuntu2204``, whose key does not derive to a valid osinfo variant) can
pin ``os_variant``/``username`` once and have **both** paths honour it.
Before this converged, only ``createvm`` read those overrides; the
manifest path derived ``os_variant`` inline from ``machine.os`` with no
override, so custom images silently fell back to ``generic``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# First-boot account name keyed by distro family. Cloud images create a
# conventional unprivileged user; cloud-init adds the SSH key to it.
_USERNAME_BY_FAMILY: dict[str, str] = {
    "debian": "debian",
    "ubuntu": "ubuntu",
    "fedora": "fedora",
    "almalinux": "almalinux",
    "rocky": "rocky",
    "centos": "cloud-user",
    "rhel": "cloud-user",
}


@dataclass(frozen=True)
class ImageEntry:  # pylint: disable=too-many-instance-attributes
    """A fully-resolved cloud image ready for either deploy path.

    Built by :func:`build_image_entry` from an image-description dict
    (a :data:`BUILTIN_IMAGES` value or an ``Lvlab.yml`` ``images:``
    entry) plus its key. ``os_variant`` and ``default_username`` are
    derived from the key unless the dict overrides them.

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
        os_variant: ``virt-install --os-variant`` argument, run through
            :func:`tkc_lvlab.utils.osinfo.resolve_os_variant` at deploy.
        default_username: First-boot account name cloud-init creates.
        username_explicit: ``True`` when ``default_username`` came from an
            explicit ``username:`` in the image dict (a deliberate per-image
            pin), ``False`` when it was derived from the key. Lets a caller
            tell a pinned username apart from a guessed one — e.g. so a
            host-wide ``default_vm_username`` can override the guess but yield
            to a pin (#138).
    """

    image_url: str
    checksum_url: str | None
    checksum_type: str | None
    checksum_url_gpg: str | None
    network_version: int
    os_variant: str
    default_username: str
    username_explicit: bool = False


def _family_token(key: str) -> str:
    """Return the leading alphabetic family token of a catalog key.

    ``debian12`` -> ``debian``, ``debian12-salt`` -> ``debian``,
    ``fedora44`` -> ``fedora``. Falls back to the whole lower-cased key
    when it has no leading letters.

    Args:
        key: A catalog / ``VM_DISTRO`` / ``machine.os`` key.

    Returns:
        The family token, lower-cased.
    """
    match = re.match(r"[a-z]+", key.lower())
    return match.group(0) if match else key.lower()


def derive_os_variant(key: str, explicit: str | None) -> str:
    """Resolve the ``--os-variant`` value for a catalog key.

    Args:
        key: The catalog / ``VM_DISTRO`` / ``machine.os`` key (e.g.
            ``debian12-salt``).
        explicit: An ``os_variant`` provided by the catalog dict /
            manifest entry, or ``None`` to derive.

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
        key: The catalog / ``VM_DISTRO`` / ``machine.os`` key.
        explicit: A ``username`` provided by the catalog dict / manifest
            entry, or ``None`` to derive.

    Returns:
        ``explicit`` when set; otherwise the family-conventional user from
        :data:`_USERNAME_BY_FAMILY`, falling back to the family token.
    """
    if explicit:
        return explicit
    family = _family_token(key)
    return _USERNAME_BY_FAMILY.get(family, family)


# Best-guess version parsers for the built-in catalog's filename / URL
# patterns. Order matters: the first matcher whose regex matches wins. Each
# matcher reads the filename + URL and returns a short, human-readable
# version token (e.g. ``"20260518-2482"``, ``"44-1.7"``, ``"jammy"``); the
# tokens are surfaced by ``lvlab init`` (#124) so an operator can tell at a
# glance *which* upstream build each cached image actually is. Add a new
# matcher when an upstream's naming convention changes; keep them
# string-only (no network, no file I/O) so they stay cheap in the init
# hot path.
_DEBIAN_DATED_BUILD_RE = re.compile(
    r"^debian-\d+-generic-amd64-(\d{6,8})-(\d+)\.qcow2$"
)
_FEDORA_RELEASE_BUILD_RE = re.compile(
    r"^Fedora-Cloud-.+-(\d+)-([\d.]+)\.x86_64\.qcow2$"
)
_ALMALINUX_RE = re.compile(
    r"^AlmaLinux-(\d+)-GenericCloud-(latest|[\d.]+)\.x86_64\.qcow2$"
)
_UBUNTU_CODENAME_RE = re.compile(r"^([a-z]+)-server-cloudimg-amd64\.(?:img|qcow2)$")
_DEBIAN_URL_CODENAME_RE = re.compile(r"/images/cloud/([a-z]+)/([^/]+)/[^/]+\.qcow2$")


def image_version(image_url: str, filename: str) -> str:
    """Best-guess upstream version token for a cloud image.

    Reads the source-of-truth strings (URL + filename) the way an operator
    would — pulls the most specific version token from the **filename**
    first, falls back to the **URL path** when the filename is generic
    (e.g. Debian's ``latest`` tree), and returns ``"?"`` when nothing
    matches rather than crashing. Pure string work — no network, no file
    I/O — so it's cheap to call once per image in the init hot path.

    Args:
        image_url: The image URL (used as a fallback signal).
        filename: The bare image filename (the last URL segment is fine).

    Returns:
        A short, human-readable version token (``"20260518-2482"`` /
        ``"44-1.7"`` / ``"jammy"`` / ``"trixie/latest"`` / ``"10 (latest)"``)
        or ``"?"`` when no parser recognises either string.

    Examples:
        >>> image_version(
        ...     "https://cloud.debian.org/images/cloud/bookworm/20260518-2482/"
        ...     "debian-12-generic-amd64-20260518-2482.qcow2",
        ...     "debian-12-generic-amd64-20260518-2482.qcow2",
        ... )
        '20260518-2482'
        >>> image_version("", "")
        '?'
    """
    if filename:
        m = _DEBIAN_DATED_BUILD_RE.match(filename)
        if m:
            return f"{m.group(1)}-{m.group(2)}"

        m = _FEDORA_RELEASE_BUILD_RE.match(filename)
        if m:
            return f"{m.group(1)}-{m.group(2)}"

        m = _ALMALINUX_RE.match(filename)
        if m:
            return f"{m.group(1)} ({m.group(2)})"

        m = _UBUNTU_CODENAME_RE.match(filename)
        if m:
            return m.group(1)

    if image_url:
        m = _DEBIAN_URL_CODENAME_RE.search(image_url)
        if m:
            return f"{m.group(1)}/{m.group(2)}"

    return "?"


def build_image_entry(key: str, cfg: dict[str, Any]) -> ImageEntry:
    """Resolve one image-description dict + its key into an :class:`ImageEntry`.

    ``os_variant`` and ``default_username`` come from the dict's
    ``os_variant`` / ``username`` keys when present, else they're derived
    from ``key``. ``network_version`` defaults to ``2`` (netplan).

    Args:
        key: The image key (``debian12``, ``ubuntu2204``, ``machine.os``).
        cfg: The image-description dict (``image_url`` required; the rest
            optional).

    Returns:
        A fully-populated :class:`ImageEntry`.

    Raises:
        KeyError: ``cfg`` has no ``image_url``.
    """
    return ImageEntry(
        image_url=cfg["image_url"],
        checksum_url=cfg.get("checksum_url"),
        checksum_type=cfg.get("checksum_type"),
        checksum_url_gpg=cfg.get("checksum_url_gpg"),
        network_version=cfg.get("network_version", 2),
        os_variant=derive_os_variant(key, cfg.get("os_variant")),
        default_username=derive_username(key, cfg.get("username")),
        username_explicit=bool(cfg.get("username")),
    )


# ---------------------------------------------------------------------------
# Built-in cloud-image catalog
# ---------------------------------------------------------------------------


# Built-in cloud images resolvable out of the box by both the standalone
# ``createvm`` script (via ``VM_DISTRO``) and ``lvlab init`` when no
# manifest is present. Each value uses the **same schema as an
# ``Lvlab.yml`` ``images:`` entry** so the built-in catalog and a cwd
# manifest merge through one code path (:func:`resolve_catalog`). The
# optional ``os_variant`` / ``username`` keys are omitted here: they're
# derived from the key (debian12 -> debian12 / debian) via
# :func:`build_image_entry` and only need to appear when the derivation is
# wrong (both createvm and ``lvlab up`` honour them — see this module).
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
    "almalinux10": {
        "image_url": (
            "https://repo.almalinux.org/almalinux/10/cloud/x86_64/images/"
            "AlmaLinux-10-GenericCloud-latest.x86_64.qcow2"
        ),
        "checksum_url": (
            "https://repo.almalinux.org/almalinux/10/cloud/x86_64/images/CHECKSUM"
        ),
        "checksum_type": "sha256",
        "checksum_url_gpg": None,
        "network_version": 2,
    },
    "almalinux9": {
        "image_url": (
            "https://repo.almalinux.org/almalinux/9/cloud/x86_64/images/"
            "AlmaLinux-9-GenericCloud-latest.x86_64.qcow2"
        ),
        "checksum_url": (
            "https://repo.almalinux.org/almalinux/9/cloud/x86_64/images/CHECKSUM"
        ),
        "checksum_type": "sha256",
        "checksum_url_gpg": None,
        "network_version": 2,
    },
    "debian11": {
        "image_url": (
            "https://cloud.debian.org/images/cloud/bullseye/20260518-2482/"
            "debian-11-generic-amd64-20260518-2482.qcow2"
        ),
        "checksum_url": (
            "https://cloud.debian.org/images/cloud/bullseye/20260518-2482/SHA512SUMS"
        ),
        "checksum_type": "sha512",
        "checksum_url_gpg": None,
        # Debian 11 stays on network-config v1 (ENI): the v2/netplan path
        # stalls networking.service ~5 min via the ifupdown DHCPv6 hang.
        "network_version": 1,
    },
    # Ubuntu publishes its cloud image as a ``.img`` (still qcow2 inside),
    # checksums as ``hex *filename`` (binary marker), and a *detached* GPG
    # signature (``SHA256SUMS.gpg``) that our clearsign verifier can't use
    # — so ``checksum_url_gpg`` is left unset. The key ``ubuntu2204`` would
    # derive the osinfo-unknown ``ubuntu2204``, so pin ``os_variant``.
    "ubuntu2204": {
        "image_url": (
            "https://cloud-images.ubuntu.com/jammy/current/"
            "jammy-server-cloudimg-amd64.img"
        ),
        "checksum_url": "https://cloud-images.ubuntu.com/jammy/current/SHA256SUMS",
        "checksum_type": "sha256",
        "checksum_url_gpg": None,
        "network_version": 2,
        "os_variant": "ubuntu22.04",
    },
    "ubuntu2404": {
        "image_url": (
            "https://cloud-images.ubuntu.com/noble/current/"
            "noble-server-cloudimg-amd64.img"
        ),
        "checksum_url": "https://cloud-images.ubuntu.com/noble/current/SHA256SUMS",
        "checksum_type": "sha256",
        "checksum_url_gpg": None,
        "network_version": 2,
        "os_variant": "ubuntu24.04",
    },
}


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


def resolve_image_entry(distro: str, catalog: dict[str, dict[str, Any]]) -> ImageEntry:
    """Resolve a ``VM_DISTRO`` value against a merged catalog.

    Matching is case-insensitive. ``os_variant`` and ``default_username``
    are taken from the source dict when present, else derived from the key.

    Args:
        distro: The user-supplied ``VM_DISTRO`` value.
        catalog: A merged catalog from :func:`resolve_catalog`.

    Returns:
        A fully-populated :class:`ImageEntry`.

    Raises:
        ValueError: ``distro`` isn't in the catalog (message lists the
            available names).
    """
    index = {name.lower(): name for name in catalog}
    real_key = index.get(distro.lower())
    if real_key is None:
        available = ", ".join(sorted(catalog))
        raise ValueError(f"Unknown distro '{distro}'. Available: {available}")
    return build_image_entry(real_key, catalog[real_key])
