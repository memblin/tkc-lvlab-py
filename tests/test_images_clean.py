"""Unit tests for the unreferenced cloud-image cleanup (``lvlab images clean``).

The cleanup command is safety-critical: it deletes files from the cloud-image
cache, and a wrong protected-set computation could remove an image a configured
machine still needs. These tests exercise the pure enumeration/grouping helpers
in :mod:`tkc_lvlab.utils.images` against real ``tmp_path`` cache dirs, then lock
the CLI command's dry-run-by-default, ``--force``, and lock-parameter behavior
with a mocked manifest. No ``virsh``, ``qemu-img``, or network is touched.
"""

from __future__ import annotations

import os
from typing import Any
from unittest import mock

from typer.testing import CliRunner

from tkc_lvlab import cli
from tkc_lvlab.cli import app
from tkc_lvlab.utils.images import (
    CloudImage,
    enumerate_protected_files,
    find_cleanup_candidates,
    resolve_cloud_image_dir,
)


def _image_config(filename: str, *, checksum: str | None, gpg: str | None) -> dict:
    """Build a manifest ``images:`` entry pointing at the given basenames."""
    config: dict[str, Any] = {
        "image_url": f"https://example.invalid/path/{filename}",
    }
    if checksum:
        config["checksum_url"] = f"https://example.invalid/path/{checksum}"
        config["checksum_type"] = "sha256"
    if gpg:
        config["checksum_url_gpg"] = f"https://example.invalid/path/{gpg}"
    return config


def _touch(path: str) -> None:
    """Create an empty file, making parent dirs as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("")


# --------------------------------------------------------------------------- #
# enumerate_protected_files / find_cleanup_candidates (pure)
# --------------------------------------------------------------------------- #


def test_protected_set_covers_defined_but_unused_image(tmp_path) -> None:
    """An image defined in ``images:`` but referenced by NO machine is still
    protected — its qcow2, checksum, .verified, and GPG paths are all in the set."""
    cache = tmp_path / "cloud-images"
    config_defaults = {"cloud_image_basedir": str(cache)}
    images = {
        "fedora44": _image_config(
            "fedora44.qcow2", checksum="CHECKSUM", gpg="fedora.gpg"
        ),
    }

    protected = enumerate_protected_files(images, {}, config_defaults)

    img = CloudImage("fedora44", images["fedora44"], {}, config_defaults)
    assert os.path.expanduser(img.image_fpath) in protected
    assert os.path.expanduser(img.checksum_fpath) in protected
    assert os.path.expanduser(img.checksum_fpath) + ".verified" in protected
    assert os.path.expanduser(img.checksum_gpg_fpath) in protected


def test_protected_image_never_listed_as_candidate(tmp_path) -> None:
    """A cache file matching a manifest image's derived name is never a removal
    candidate — covers both the image and its sidecars on disk."""
    cache = tmp_path / "cloud-images"
    config_defaults = {"cloud_image_basedir": str(cache)}
    images = {"debian12": _image_config("debian12.qcow2", checksum=None, gpg=None)}

    img = CloudImage("debian12", images["debian12"], {}, config_defaults)
    _touch(os.path.expanduser(img.image_fpath))

    protected = enumerate_protected_files(images, {}, config_defaults)
    candidates = find_cleanup_candidates(
        resolve_cloud_image_dir(config_defaults), protected
    )

    assert candidates == []


def test_genuinely_unreferenced_file_is_a_candidate(tmp_path) -> None:
    """A cache file no manifest entry derives is reported for removal."""
    cache = tmp_path / "cloud-images"
    config_defaults = {"cloud_image_basedir": str(cache)}
    images = {"debian12": _image_config("debian12.qcow2", checksum=None, gpg=None)}

    # Protected image present...
    img = CloudImage("debian12", images["debian12"], {}, config_defaults)
    _touch(os.path.expanduser(img.image_fpath))
    # ...alongside a stale image no manifest entry claims.
    stale = str(cache / "old-fedora39.qcow2")
    _touch(stale)

    protected = enumerate_protected_files(images, {}, config_defaults)
    candidates = find_cleanup_candidates(str(cache), protected)

    assert [c.image_fpath for c in candidates] == [stale]


def test_candidate_groups_its_sidecars(tmp_path) -> None:
    """An unreferenced image's checksum / .verified / GPG sidecars are grouped
    onto the same candidate so they are removed together, not orphaned."""
    cache = tmp_path / "cloud-images"
    cache.mkdir()
    image = str(cache / "stale.qcow2")
    checksum = str(cache / "stale.qcow2.SHA256SUMS")
    verified = str(cache / "stale.qcow2.SHA256SUMS.verified")
    gpg = str(cache / "stale.qcow2.keyring.gpg")
    for path in (image, checksum, verified, gpg):
        _touch(path)

    candidates = find_cleanup_candidates(str(cache), protected=set())

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.image_fpath == image
    assert set(candidate.sidecar_fpaths) == {checksum, verified, gpg}
    assert set(candidate.all_fpaths) == {image, checksum, verified, gpg}


def test_missing_cache_dir_yields_no_candidates(tmp_path) -> None:
    """A cache dir that does not exist yet produces no candidates (no crash)."""
    candidates = find_cleanup_candidates(str(tmp_path / "absent"), protected=set())
    assert candidates == []


# --------------------------------------------------------------------------- #
# lvlab images clean (CLI)
# --------------------------------------------------------------------------- #


def _parse_return(cache: str) -> tuple:
    """A parse_config() return tuple protecting ``debian12.qcow2``."""
    config_defaults = {"cloud_image_basedir": cache}
    images = {"debian12": _image_config("debian12.qcow2", checksum=None, gpg=None)}
    return ({"name": "test-env"}, images, config_defaults, [])


def _run_clean(tmp_path, *, args: list[str], parse_return) -> object:
    """Invoke ``lvlab images clean`` with parse_config + backing scan patched."""
    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", return_value=parse_return),
        mock.patch.object(cli, "backing_files_in_use", return_value=set()),
    ):
        return runner.invoke(app, ["images", "clean", *args])


def test_clean_dry_run_deletes_nothing(tmp_path) -> None:
    """Default invocation lists candidates but removes no files from disk."""
    cache = str(tmp_path / "cloud-images")
    parse_return = _parse_return(cache)
    protected_image = CloudImage(
        "debian12", parse_return[1]["debian12"], {}, parse_return[2]
    )
    _touch(os.path.expanduser(protected_image.image_fpath))
    stale = os.path.join(cache, "stale.qcow2")
    _touch(stale)

    result = _run_clean(tmp_path, args=[], parse_return=parse_return)

    assert result.exit_code == 0, result.output
    assert "Would remove" in result.output
    assert "stale.qcow2" in result.output
    assert "Dry run" in result.output
    # Nothing actually deleted.
    assert os.path.exists(stale)
    assert os.path.exists(os.path.expanduser(protected_image.image_fpath))


def test_clean_force_deletes_only_candidates(tmp_path) -> None:
    """``--force`` removes the unreferenced file and leaves protected ones."""
    cache = str(tmp_path / "cloud-images")
    parse_return = _parse_return(cache)
    protected_image = CloudImage(
        "debian12", parse_return[1]["debian12"], {}, parse_return[2]
    )
    protected_path = os.path.expanduser(protected_image.image_fpath)
    _touch(protected_path)
    stale = os.path.join(cache, "stale.qcow2")
    stale_sidecar = os.path.join(cache, "stale.qcow2.SHA256SUMS")
    _touch(stale)
    _touch(stale_sidecar)

    result = _run_clean(tmp_path, args=["--force"], parse_return=parse_return)

    assert result.exit_code == 0, result.output
    assert "Removing" in result.output
    assert not os.path.exists(stale)
    assert not os.path.exists(stale_sidecar)  # sidecar removed with its image
    assert os.path.exists(protected_path)  # protected survives


def test_clean_lock_prevents_all_deletion(tmp_path) -> None:
    """``prevent_cloud_image_cleanup: true`` refuses to delete even with --force."""
    cache = str(tmp_path / "cloud-images")
    environment, images, config_defaults, machines = _parse_return(cache)
    config_defaults["prevent_cloud_image_cleanup"] = True
    parse_return = (environment, images, config_defaults, machines)
    stale = os.path.join(cache, "stale.qcow2")
    _touch(stale)

    result = _run_clean(tmp_path, args=["--force"], parse_return=parse_return)

    assert result.exit_code == 1, result.output
    assert "prevent_cloud_image_cleanup" in result.output
    assert os.path.exists(stale)  # untouched


def test_clean_refuses_when_manifest_missing() -> None:
    """A missing Lvlab.yml (parse_config returns None) aborts rather than guessing."""
    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", return_value=None),
        mock.patch.object(cli, "logger") as mocked_logger,
    ):
        result = runner.invoke(app, ["images", "clean"])

    assert result.exit_code == 1
    assert mocked_logger.error.called


def test_clean_refuses_when_manifest_unparseable() -> None:
    """A structurally invalid manifest (ConfigError) aborts with exit 1."""
    from tkc_lvlab.exceptions import ConfigError

    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", side_effect=ConfigError("boom")),
        mock.patch.object(cli, "logger") as mocked_logger,
    ):
        result = runner.invoke(app, ["images", "clean"])

    assert result.exit_code == 1
    assert mocked_logger.error.called
