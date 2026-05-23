"""Regression tests for ``CloudImage.__init__``'s ``cloud_image_basedir`` handling.

The 2026-05-23 destructive smoke test surfaced a double-append bug:
setting ``cloud_image_basedir: /var/lib/libvirt/images/cloud-images``
in a manifest (a natural way to share a cache with the standalone
``createvm`` script, which writes to ``/var/lib/libvirt/images/cloud-images/``)
produced an ``image_dir`` of
``/var/lib/libvirt/images/cloud-images/cloud-images``. The unconditional
``os.path.join(basedir, "cloud-images")`` was the cause.

The fix is tail-aware: if the configured basedir already ends in
``cloud-images``, use it as-is; otherwise append. These tests pin
both behaviors plus a few edge cases (trailing slashes, expanded ``~``).
"""

from __future__ import annotations

from tkc_lvlab.utils.images import CloudImage


_DEBIAN_CONFIG = {
    "image_url": "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2",
    "checksum_url": "https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS",
    "checksum_type": "sha512",
    "checksum_url_gpg": None,
    "network_version": 2,
}


def _make_cloud_image(basedir: str) -> CloudImage:
    """Construct a CloudImage with the configured ``cloud_image_basedir``."""
    return CloudImage(
        name="debian13",
        config=_DEBIAN_CONFIG,
        environment={},
        config_defaults={"cloud_image_basedir": basedir},
    )


def test_basedir_without_cloud_images_suffix_gets_one_appended() -> None:
    """Legacy convention: basedir is the PARENT dir, ``cloud-images`` is
    appended automatically."""
    image = _make_cloud_image("/var/lib/libvirt/images/lvlab")
    assert image.image_dir == "/var/lib/libvirt/images/lvlab/cloud-images"


def test_basedir_already_ending_in_cloud_images_is_left_alone() -> None:
    """The 2026-05-23 smoke-test bug: a basedir whose tail is
    ``cloud-images`` must not get the suffix doubled."""
    image = _make_cloud_image("/var/lib/libvirt/images/cloud-images")
    assert image.image_dir == "/var/lib/libvirt/images/cloud-images"


def test_basedir_with_trailing_slash_still_detects_cloud_images_suffix() -> None:
    """A trailing slash shouldn't fool the tail check.

    ``os.path.basename("/a/b/")`` returns ``""`` rather than ``"b"``;
    the fix strips trailing separators before the tail comparison so
    this case still resolves correctly.
    """
    image = _make_cloud_image("/var/lib/libvirt/images/cloud-images/")
    assert image.image_dir.rstrip("/") == "/var/lib/libvirt/images/cloud-images"


def test_basedir_default_appends_cloud_images() -> None:
    """When ``cloud_image_basedir`` is not set, the default
    ``/var/lib/libvirt/images/lvlab`` still gets ``/cloud-images``
    appended."""
    image = CloudImage(
        name="debian13",
        config=_DEBIAN_CONFIG,
        environment={},
        config_defaults={},  # no cloud_image_basedir
    )
    assert image.image_dir == "/var/lib/libvirt/images/lvlab/cloud-images"


def test_basedir_with_tilde_expands_at_filesystem_use_time() -> None:
    """``~`` expansion happens at filesystem-use time (in
    ``_manage_image_dir``, ``image_fpath``, etc.) rather than at
    ``__init__`` time. The stored ``image_dir`` keeps the literal
    ``~`` until that point.

    Pinning this so any future refactor that moves expansion into
    ``__init__`` is deliberate. The suffix-doubling fix must not
    change when expansion happens.
    """
    image = _make_cloud_image("~/lvlab-images")
    assert image.image_dir == "~/lvlab-images/cloud-images"


def test_basedir_with_tilde_and_cloud_images_suffix_is_idempotent() -> None:
    """Combining ``~`` with a ``cloud-images`` tail still resolves
    without doubling."""
    image = _make_cloud_image("~/cloud-images")
    assert image.image_dir == "~/cloud-images"
