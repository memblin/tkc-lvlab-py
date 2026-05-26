"""Unit tests for :class:`tkc_lvlab.utils.vdisk.VirtualDisk`.

Covers the issue #99 disk strategy: ``copy`` (standalone, the new default)
vs ``backing`` (cloud-image overlay, opt-in), strategy resolution
(per-disk override > config default > ``copy``), and the create paths.
``shutil.copyfile`` and ``subprocess.run`` are mocked at the module
boundary so no real ``cp`` / ``qemu-img`` runs; the parent dir lands under
``tmp_path``.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest import mock

from tkc_lvlab.utils.vdisk import DEFAULT_DISK_STRATEGY, VirtualDisk


def _vdisk(tmp_path, disk: dict, config_defaults: dict | None = None) -> VirtualDisk:
    cloud_image = SimpleNamespace(image_fpath=str(tmp_path / "base.qcow2"))
    defaults = {"disk_image_basedir": str(tmp_path)}
    if config_defaults:
        defaults.update(config_defaults)
    return VirtualDisk("web01", disk, 0, cloud_image, {"name": "env"}, defaults)


# --- strategy resolution ---------------------------------------------------


def test_default_strategy_is_copy(tmp_path) -> None:
    """No strategy anywhere -> copy (the cache-safe default)."""
    assert _vdisk(tmp_path, {"size": "10G"}).strategy == "copy"
    assert DEFAULT_DISK_STRATEGY == "copy"


def test_config_default_selects_backing(tmp_path) -> None:
    """config_defaults.disk_strategy: backing is honored."""
    vd = _vdisk(tmp_path, {"size": "10G"}, {"disk_strategy": "backing"})
    assert vd.strategy == "backing"


def test_per_disk_strategy_overrides_config_default(tmp_path) -> None:
    """A per-disk strategy wins over the config default."""
    vd = _vdisk(
        tmp_path, {"size": "10G", "strategy": "copy"}, {"disk_strategy": "backing"}
    )
    assert vd.strategy == "copy"


def test_unknown_strategy_falls_back_to_copy(tmp_path) -> None:
    """An unrecognized strategy degrades to copy rather than failing."""
    vd = _vdisk(tmp_path, {"size": "10G", "strategy": "wat"})
    assert vd.strategy == "copy"


# --- create paths ----------------------------------------------------------


def test_create_copy_copies_then_resizes(tmp_path) -> None:
    """copy strategy: cp the base image, then qemu-img resize to size."""
    vd = _vdisk(tmp_path, {"size": "25G"})
    with (
        mock.patch("tkc_lvlab.utils.vdisk.shutil.copyfile") as copyfile,
        mock.patch("tkc_lvlab.utils.vdisk.subprocess.run") as run,
    ):
        assert vd.create() is True

    copyfile.assert_called_once_with(vd.backing_image_fpath, vd.fpath)
    # qemu-img resize <fpath> 25G — and NOT a `create -b` backing call.
    argv = run.call_args.args[0]
    assert argv == ["qemu-img", "resize", vd.fpath, "25G"]


def test_create_copy_no_size_skips_resize(tmp_path) -> None:
    """copy with no size: keep the base image size, no resize call."""
    vd = _vdisk(tmp_path, {})
    with (
        mock.patch("tkc_lvlab.utils.vdisk.shutil.copyfile") as copyfile,
        mock.patch("tkc_lvlab.utils.vdisk.subprocess.run") as run,
    ):
        assert vd.create() is True

    copyfile.assert_called_once()
    run.assert_not_called()


def test_create_copy_tolerates_resize_failure(tmp_path) -> None:
    """A resize failure (qcow2 can't shrink) is non-fatal: copy still succeeds."""
    vd = _vdisk(tmp_path, {"size": "1G"})
    with (
        mock.patch("tkc_lvlab.utils.vdisk.shutil.copyfile") as copyfile,
        mock.patch(
            "tkc_lvlab.utils.vdisk.subprocess.run",
            side_effect=subprocess.CalledProcessError(
                1, ["qemu-img", "resize"], stderr=b"use --shrink"
            ),
        ),
    ):
        assert vd.create() is True  # resize failure does not fail the create

    copyfile.assert_called_once()


def test_create_backing_uses_qemu_img_create_b(tmp_path) -> None:
    """backing strategy: qemu-img create -b <base>, and NO cp of the image."""
    vd = _vdisk(tmp_path, {"size": "25G", "strategy": "backing"})
    with (
        mock.patch("tkc_lvlab.utils.vdisk.shutil.copyfile") as copyfile,
        mock.patch("tkc_lvlab.utils.vdisk.subprocess.run") as run,
    ):
        assert vd.create() is True

    copyfile.assert_not_called()
    argv = run.call_args.args[0]
    assert argv[:3] == ["qemu-img", "create", "-b"]
    assert vd.backing_image_fpath in argv
    assert vd.fpath in argv


def test_create_copy_failure_returns_false(tmp_path) -> None:
    """A copy (cp) failure returns False so the caller can report it."""
    vd = _vdisk(tmp_path, {"size": "25G"})
    with (
        mock.patch(
            "tkc_lvlab.utils.vdisk.shutil.copyfile", side_effect=OSError("no space")
        ),
        mock.patch("tkc_lvlab.utils.vdisk.subprocess.run") as run,
    ):
        assert vd.create() is False
    run.assert_not_called()
