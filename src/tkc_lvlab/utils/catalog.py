"""Shared cloud-image entry resolution for both deploy paths.

A cloud image is described the same way everywhere — in
:data:`tkc_lvlab.scripts.createvm.BUILTIN_IMAGES`, in an ``Lvlab.yml``
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
class ImageEntry:
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
    """

    image_url: str
    checksum_url: str | None
    checksum_type: str | None
    checksum_url_gpg: str | None
    network_version: int
    os_variant: str
    default_username: str


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
    )
