"""Unit tests for :mod:`tkc_lvlab.utils.catalog` — the shared image-entry
resolution used by BOTH the standalone ``createvm`` script and the
manifest ``Machine`` deploy path.

The behaviour that matters: ``os_variant`` and ``default_username`` are
derived from the image key, but a per-entry override always wins. That
override is what lets a custom or oddly-keyed image (e.g. ``ubuntu2204``,
whose key does not derive to a valid osinfo variant) pin a correct
``os_variant`` once and have both deploy paths honour it.
"""

from __future__ import annotations

from tkc_lvlab.utils.catalog import (
    ImageEntry,
    build_image_entry,
    derive_os_variant,
    derive_username,
)


def test_derive_os_variant_uses_key_segment_before_dash() -> None:
    """Without an override, os_variant is the key up to the first dash."""
    assert derive_os_variant("debian12", None) == "debian12"
    assert derive_os_variant("debian12-salt", None) == "debian12"


def test_derive_os_variant_override_wins() -> None:
    """A custom image keyed ``ubuntu2204`` can pin the osinfo-valid
    ``ubuntu22.04`` (the key alone would derive the unknown ``ubuntu2204``)."""
    assert derive_os_variant("ubuntu2204", "ubuntu22.04") == "ubuntu22.04"


def test_derive_username_family_map_and_fallback() -> None:
    """Known families map to their conventional user; unknown families
    fall back to the leading token."""
    assert derive_username("fedora44", None) == "fedora"
    assert derive_username("almalinux10", None) == "almalinux"
    assert derive_username("centos9", None) == "cloud-user"
    assert derive_username("alpine318", None) == "alpine"  # unknown family


def test_derive_username_override_wins() -> None:
    """An explicit username overrides family derivation."""
    assert derive_username("debian12", "salt") == "salt"


def test_build_image_entry_derives_when_no_overrides() -> None:
    """A plain entry derives os_variant + username from the key and
    defaults network_version to 2."""
    entry = build_image_entry(
        "fedora44",
        {
            "image_url": "https://example/fedora.qcow2",
            "checksum_url": "https://example/CHECKSUM",
            "checksum_type": "sha256",
        },
    )
    assert isinstance(entry, ImageEntry)
    assert entry.os_variant == "fedora44"
    assert entry.default_username == "fedora"
    assert entry.network_version == 2  # defaulted
    assert entry.checksum_url_gpg is None  # absent -> None


def test_build_image_entry_honours_overrides() -> None:
    """Per-entry os_variant/username/network_version override derivation —
    the converged behaviour that fixes custom images on the manifest path."""
    entry = build_image_entry(
        "ubuntu2204",
        {
            "image_url": "https://example/jammy.img",
            "os_variant": "ubuntu22.04",
            "username": "ubuntu",
            "network_version": 1,
        },
    )
    assert entry.os_variant == "ubuntu22.04"
    assert entry.default_username == "ubuntu"
    assert entry.network_version == 1
