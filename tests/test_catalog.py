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
    image_version,
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
    assert entry.username_explicit is False  # derived, not pinned
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
    assert entry.username_explicit is True  # came from the explicit username:
    assert entry.network_version == 1


# --- #124: best-guess image version --------------------------------------------


def test_image_version_debian_dated_build_from_filename() -> None:
    """Debian dated build → ``<date>-<build>`` token (#124)."""
    assert (
        image_version(
            "https://cloud.debian.org/images/cloud/bookworm/20260518-2482/"
            "debian-12-generic-amd64-20260518-2482.qcow2",
            "debian-12-generic-amd64-20260518-2482.qcow2",
        )
        == "20260518-2482"
    )


def test_image_version_debian_latest_falls_back_to_url_path() -> None:
    """Debian trixie latest → ``trixie/latest`` (from URL, filename has no date)."""
    assert (
        image_version(
            "https://cloud.debian.org/images/cloud/trixie/latest/"
            "debian-13-generic-amd64.qcow2",
            "debian-13-generic-amd64.qcow2",
        )
        == "trixie/latest"
    )


def test_image_version_fedora_release_build_from_filename() -> None:
    """Fedora ``<release>-<build>`` → ``44-1.7`` (#124)."""
    assert (
        image_version(
            "https://download.fedoraproject.org/pub/fedora/linux/releases/44/"
            "Cloud/x86_64/images/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2",
            "Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2",
        )
        == "44-1.7"
    )


def test_image_version_almalinux_latest() -> None:
    """AlmaLinux ``-latest`` → ``10 (latest)`` (major from filename, build = latest)."""
    assert (
        image_version(
            "https://repo.almalinux.org/almalinux/10/cloud/x86_64/images/"
            "AlmaLinux-10-GenericCloud-latest.x86_64.qcow2",
            "AlmaLinux-10-GenericCloud-latest.x86_64.qcow2",
        )
        == "10 (latest)"
    )


def test_image_version_ubuntu_codename_only() -> None:
    """Ubuntu cloud images encode the version as a codename — ``jammy``, ``noble``."""
    assert (
        image_version(
            "https://cloud-images.ubuntu.com/jammy/current/"
            "jammy-server-cloudimg-amd64.img",
            "jammy-server-cloudimg-amd64.img",
        )
        == "jammy"
    )
    assert (
        image_version(
            "https://cloud-images.ubuntu.com/noble/current/"
            "noble-server-cloudimg-amd64.img",
            "noble-server-cloudimg-amd64.img",
        )
        == "noble"
    )


def test_image_version_unknown_format_returns_question_mark() -> None:
    """Unrecognised filename + URL → graceful ``?`` fallback (never crash)."""
    assert image_version("https://example/random/path.qcow2", "path.qcow2") == "?"


def test_image_version_handles_empty_inputs_gracefully() -> None:
    """Empty inputs → ``?`` rather than raising."""
    assert image_version("", "") == "?"
