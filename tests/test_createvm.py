"""Unit tests for :mod:`tkc_lvlab.scripts.createvm`.

These tests use Click's :class:`CliRunner` against the ``run`` command
and patch every external interaction at the import boundary so nothing
hits the real virsh, qemu-img, virt-install, openssl, or filesystem
beyond the test's own ``tmp_path``.

What they lock in:

- The raw libvirt domain name (no prefix) and ``<storage_root>/<vm_name>/``
    storage convention.
- ``--distro`` resolves against the built-in catalog merged with a cwd
    ``Lvlab.yml`` ``images:`` section (manifest wins on collision); an
    unknown name errors with the available list.
- ``DependencyError`` from the tooling check translates to a nonzero
    exit + ClickException-rendered message.
- The disk is copied (cp+resize) by default; ``--no-copy`` switches to
    ``qemu-img`` backing-file mode (``-b``).
- ``--ip4`` flows through to network validation (with the network's
    DHCP range honored).
- A bridge network without explicit defaults surfaces as a
    ``LibvirtNetworkError`` → nonzero exit.
- Missing SSH keys (no discovery + no --public-key) cause a clear
    refusal, not a silent VM creation.
- Cleanup-on-failure: if ``virt-install`` fails, the VM dir is wiped.

All subprocess calls are intercepted; pytest itself never invokes a
real binary.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from tkc_lvlab.scripts import createvm as cv_mod
from tkc_lvlab.scripts.createvm import (
    BUILTIN_IMAGES,
    derive_os_variant,
    derive_username,
    parse_ip4_option,
    resolve_catalog,
    resolve_image_entry,
    run,
    storage_dir_for,
)
from tkc_lvlab.utils.network import LibvirtNetworkError, LibvirtNetworkInfo
from tkc_lvlab.utils.requirements import DependencyError


# ---------------------------------------------------------------------------
# Storage path helper
# ---------------------------------------------------------------------------


def test_storage_dir_for_under_root(tmp_path: Path) -> None:
    """Per-VM storage lands under <root>/<vm_name>/, not in <root>/ directly."""
    assert storage_dir_for("alpha", root=tmp_path) == tmp_path / "alpha"


# ---------------------------------------------------------------------------
# Catalog resolution: merge + metadata derivation
# ---------------------------------------------------------------------------


def test_resolve_catalog_merges_manifest_over_builtins() -> None:
    """Manifest images override built-ins on collision; built-ins survive otherwise."""
    manifest = {
        "debian12": {
            "image_url": "http://host/custom-debian12.qcow2",
            "network_version": 1,
        },
        "myapp": {"image_url": "http://host/myapp.qcow2"},
    }
    catalog = resolve_catalog(manifest)
    # Manifest wins for the colliding key.
    assert catalog["debian12"]["image_url"] == "http://host/custom-debian12.qcow2"
    # New manifest-only image is present.
    assert "myapp" in catalog
    # A built-in not redefined by the manifest still resolves.
    assert "fedora44" in catalog


def test_resolve_catalog_none_is_builtins_only() -> None:
    """With no manifest, the catalog is exactly the built-ins."""
    assert resolve_catalog(None).keys() == BUILTIN_IMAGES.keys()


def test_resolve_image_entry_is_case_insensitive() -> None:
    """--distro matching ignores case (parity with the old click.Choice behavior)."""
    entry = resolve_image_entry("DEBIAN12", resolve_catalog(None))
    assert entry.os_variant == "debian12"
    assert entry.default_username == "debian"


def test_resolve_image_entry_unknown_distro_lists_available() -> None:
    """An unknown --distro raises ValueError naming the available keys."""
    with pytest.raises(ValueError, match="Unknown distro 'nope'. Available:"):
        resolve_image_entry("nope", resolve_catalog(None))


def test_resolve_image_entry_derives_metadata_for_manifest_image() -> None:
    """A manifest image with no os_variant/username gets derived values."""
    catalog = resolve_catalog({"debian12-salt": {"image_url": "http://h/x.qcow2"}})
    entry = resolve_image_entry("debian12-salt", catalog)
    assert entry.os_variant == "debian12"  # text before first '-'
    assert entry.default_username == "debian"  # family map
    assert entry.network_version == 2  # default when omitted


def test_resolve_image_entry_manifest_can_override_metadata() -> None:
    """Explicit os_variant/username in the manifest win over derivation."""
    catalog = resolve_catalog(
        {
            "debian12": {
                "image_url": "http://h/x.qcow2",
                "os_variant": "debian-custom",
                "username": "admin",
            }
        }
    )
    entry = resolve_image_entry("debian12", catalog)
    assert entry.os_variant == "debian-custom"
    assert entry.default_username == "admin"


def test_derive_helpers_fall_back_to_family_token() -> None:
    """Unknown families derive os_variant/username from the leading token."""
    assert derive_os_variant("alpine318", None) == "alpine318"
    assert derive_username("alpine318", None) == "alpine"
    # Explicit values always win.
    assert derive_os_variant("debian12", "x") == "x"
    assert derive_username("debian12", "x") == "x"


# ---------------------------------------------------------------------------
# parse_ip4_option
# ---------------------------------------------------------------------------


def test_parse_ip4_bare_uses_default_network() -> None:
    """Bare 'IP' uses the default network."""
    assert parse_ip4_option("192.168.122.50", "default") == (
        "default",
        "192.168.122.50",
    )


def test_parse_ip4_network_comma_ip() -> None:
    """'NETWORK,IP' splits cleanly."""
    assert parse_ip4_option("vlan10,100.64.10.50", "default") == (
        "vlan10",
        "100.64.10.50",
    )


def test_parse_ip4_rejects_empty_segments() -> None:
    """An 'IP,' or ',NETWORK' format is invalid — surfaces as BadParameter."""
    import click

    with pytest.raises(click.BadParameter):
        parse_ip4_option("vlan10,", "default")
    with pytest.raises(click.BadParameter):
        parse_ip4_option(",192.168.122.50", "default")


# ---------------------------------------------------------------------------
# CLI orchestration
# ---------------------------------------------------------------------------


def _nat_network_info() -> LibvirtNetworkInfo:
    """Build a NAT LibvirtNetworkInfo matching libvirt's default network."""
    return LibvirtNetworkInfo(
        name="default",
        forward_mode="nat",
        gateway_ip="192.168.122.1",
        netmask="255.255.255.0",
        dhcp_start="192.168.122.2",
        dhcp_end="192.168.122.254",
    )


@pytest.fixture
def all_external_mocked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """Patch every external dependency in the createvm module.

    Returns a dict of the relevant mock objects so individual tests can
    assert on call args.
    """
    mocks = {
        "tooling": mock.Mock(),  # succeed silently
        "get_network_info": mock.Mock(return_value=_nat_network_info()),
        "validate_static_ip": mock.Mock(),
        "resolve_network_settings": mock.Mock(
            return_value=(["192.168.122.1"], "192.168.122.1", [])
        ),
        "discover_default_public_keys": mock.Mock(
            return_value=[
                "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBBBBBBBBBB tester@laptop"
            ]
        ),
        "hash_password_sha512": mock.Mock(return_value="$6$rounds=4096$abc$xyz"),
        "ensure_image_available": mock.Mock(),
        "subprocess_run": mock.Mock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
        ),
        # createvm copies the cloud image by default; keep the copy inert
        # so the default path doesn't try to read a nonexistent fake image.
        "copyfile": mock.Mock(),
    }

    # Patch in the createvm namespace.
    monkeypatch.setattr(cv_mod, "check_createvm_tooling", mocks["tooling"])
    monkeypatch.setattr(cv_mod, "get_network_info", mocks["get_network_info"])
    monkeypatch.setattr(cv_mod, "validate_static_ip", mocks["validate_static_ip"])
    monkeypatch.setattr(
        cv_mod, "resolve_network_settings", mocks["resolve_network_settings"]
    )
    monkeypatch.setattr(
        cv_mod, "discover_default_public_keys", mocks["discover_default_public_keys"]
    )
    monkeypatch.setattr(cv_mod, "hash_password_sha512", mocks["hash_password_sha512"])
    monkeypatch.setattr(
        cv_mod, "_ensure_image_available", mocks["ensure_image_available"]
    )

    # Patch subprocess.run inside the createvm module (qemu-img, virt-install).
    monkeypatch.setattr(subprocess, "run", mocks["subprocess_run"])
    monkeypatch.setattr("tkc_lvlab.scripts.createvm.shutil.copyfile", mocks["copyfile"])

    # Bypass the osinfo-db lookup — it would otherwise add an extra
    # subprocess call ('virt-install --osinfo list') that pollutes the
    # call-count assertions in these tests. The fallback resolution is
    # exercised in tests/test_osinfo.py.
    monkeypatch.setattr(cv_mod, "resolve_os_variant", lambda v: (v, None))

    # Built-ins-only by default so these tests don't depend on whether the
    # cwd happens to have an Lvlab.yml. The merge path is covered by the
    # resolve_catalog unit tests above and the dedicated merge CLI test.
    monkeypatch.setattr(cv_mod, "load_manifest_images", lambda: None)

    # Stub out CloudInitIso.write so it doesn't touch real pycdlib.
    monkeypatch.setattr(
        "tkc_lvlab.scripts.createvm.CloudInitIso.write", lambda self: True
    )

    # Stub _build_cloud_image so we don't need real URLs / disk paths.
    fake_image = mock.Mock()
    fake_image.image_fpath = str(tmp_path / "fake-cloud.qcow2")
    fake_image.image_url = "https://example.invalid/img.qcow2"
    fake_image.checksum_url = "https://example.invalid/sum"
    monkeypatch.setattr(cv_mod, "_build_cloud_image", lambda *a, **kw: fake_image)
    mocks["fake_image"] = fake_image
    return mocks


def test_run_happy_path_with_defaults(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """End-to-end happy path with all defaults: returns 0, calls virt-install once."""
    runner = CliRunner()
    result = runner.invoke(
        run,
        [
            "testvm.local",
            "--distro",
            "debian12",
            "--storage-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output

    # virt-install was invoked with the raw vm_name as the domain name.
    subprocess_run = all_external_mocked["subprocess_run"]
    virt_install_calls = [
        c for c in subprocess_run.call_args_list if c.args[0][0] == "virt-install"
    ]
    assert len(virt_install_calls) == 1
    argv = virt_install_calls[0].args[0]
    assert "--name=testvm.local" in argv
    # Default network on a NAT setup.
    assert any(arg.startswith("network=default,") for arg in argv)


def test_run_copies_disk_by_default(all_external_mocked: dict, tmp_path: Path) -> None:
    """By default (no flag), the disk is shutil.copyfile'd then qemu-img resize'd.

    createvm produces a standalone qcow2 by default so a one-off VM
    survives a wipe of the shared cloud-images cache.
    """
    runner = CliRunner()
    result = runner.invoke(
        run, ["testvm.local", "--distro", "debian12", "--storage-root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output

    # The cloud image was copied (standalone disk), not backing-file linked.
    all_external_mocked["copyfile"].assert_called_once()

    # qemu-img was called for resize, not for create.
    qemu_img_calls = [
        c
        for c in all_external_mocked["subprocess_run"].call_args_list
        if c.args[0][0] == "qemu-img"
    ]
    assert len(qemu_img_calls) == 1
    assert qemu_img_calls[0].args[0][1] == "resize"


def test_run_no_copy_uses_backing_file_disk(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """With --no-copy, qemu-img is invoked with -b (backing-file mode)."""
    runner = CliRunner()
    result = runner.invoke(
        run,
        [
            "testvm.local",
            "--distro",
            "debian12",
            "--no-copy",
            "--storage-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output

    # No copy in backing-file mode.
    all_external_mocked["copyfile"].assert_not_called()

    qemu_img_calls = [
        c
        for c in all_external_mocked["subprocess_run"].call_args_list
        if c.args[0][0] == "qemu-img"
    ]
    # Exactly one qemu-img call (backing-file create).
    assert len(qemu_img_calls) == 1
    assert qemu_img_calls[0].args[0][1] == "create"
    assert "-b" in qemu_img_calls[0].args[0]


def test_run_dependency_failure_exits_nonzero(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """DependencyError from the tooling check produces a clear nonzero exit."""
    all_external_mocked["tooling"].side_effect = DependencyError(
        "Missing required system binaries for createvm:\n- virsh\nInstall them with apt: ..."
    )

    runner = CliRunner()
    result = runner.invoke(
        run,
        [
            "testvm.local",
            "--distro",
            "debian12",
            "--storage-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0
    assert "Missing required system binaries" in result.output


def test_run_bridge_network_without_defaults_fails(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """A bridge network rejected by resolve_network_settings exits nonzero."""
    all_external_mocked["resolve_network_settings"].side_effect = LibvirtNetworkError(
        "Network 'vlan10' is a bridge. Supply explicit default_dns and default_gateway."
    )

    runner = CliRunner()
    result = runner.invoke(
        run,
        [
            "testvm.local",
            "--distro",
            "debian12",
            "--network",
            "vlan10",
            "--storage-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0
    assert "bridge" in result.output.lower()


def test_run_no_ssh_keys_refuses(all_external_mocked: dict, tmp_path: Path) -> None:
    """Zero discovered keys + no --public-key → refusal with clear message.

    Creating a VM with no way to log in is the operator-error case worth
    catching early.
    """
    all_external_mocked["discover_default_public_keys"].return_value = []

    runner = CliRunner()
    result = runner.invoke(
        run,
        [
            "testvm.local",
            "--distro",
            "debian12",
            "--storage-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0
    assert "No SSH public keys" in result.output


def test_run_ip4_flows_into_validate_static_ip(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """A --ip4 argument is parsed and handed to validate_static_ip."""
    runner = CliRunner()
    result = runner.invoke(
        run,
        [
            "testvm.local",
            "--distro",
            "debian12",
            "--ip4",
            "192.168.122.50",
            "--storage-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output

    validate = all_external_mocked["validate_static_ip"]
    validate.assert_called_once()
    assert validate.call_args.args[0] == "192.168.122.50"


def test_run_ip4_in_dhcp_range_translates_to_clickexception(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """A ValueError from validate_static_ip surfaces as a clean CLI error."""
    all_external_mocked["validate_static_ip"].side_effect = ValueError(
        "IP address '192.168.122.100' falls within DHCP range '192.168.122.2-192.168.122.254' for network 'default'."
    )
    runner = CliRunner()
    result = runner.invoke(
        run,
        [
            "testvm.local",
            "--distro",
            "debian12",
            "--ip4",
            "192.168.122.100",
            "--storage-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0
    assert "DHCP range" in result.output


def test_run_virt_install_failure_cleans_up_vm_dir(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """If virt-install fails, the partial VM dir is removed (cleanup-on-failure)."""
    # Make subprocess.run fail specifically for virt-install.
    real_run = all_external_mocked["subprocess_run"]

    def fail_on_virt_install(argv: list[str], **kwargs):
        if argv[0] == "virt-install":
            raise subprocess.CalledProcessError(
                returncode=1, cmd=argv, stderr="virt-install boom\n"
            )
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="", stderr=""
        )

    real_run.side_effect = fail_on_virt_install

    runner = CliRunner()
    result = runner.invoke(
        run,
        [
            "testvm.local",
            "--distro",
            "debian12",
            "--storage-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0
    # Dir should have been created and then removed.
    assert not (tmp_path / "testvm.local").exists()


def test_run_collision_with_existing_storage_dir_refuses(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """If the VM dir already exists, refuse (don't clobber existing state)."""
    existing = tmp_path / "testvm.local"
    existing.mkdir()

    runner = CliRunner()
    result = runner.invoke(
        run,
        [
            "testvm.local",
            "--distro",
            "debian12",
            "--storage-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0
    assert "already exists" in result.output


# ---------------------------------------------------------------------------
# --network-type (Phase 12)
# ---------------------------------------------------------------------------


def test_run_network_type_user_skips_libvirt_network_introspection(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """With --network-type user, no libvirt network is looked up.

    Phase 12 invariant: user-mode networking (SLIRP/passt) is the path
    for qemu:///session, which often has no libvirt network defined at
    all. get_network_info must not be called — calling it would raise
    LibvirtNetworkError on a network-less URI and break the use case.
    """
    runner = CliRunner()
    result = runner.invoke(
        run,
        [
            "testvm.local",
            "--distro",
            "debian12",
            "--network-type",
            "user",
            "--storage-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output

    # No libvirt network introspection happened.
    all_external_mocked["get_network_info"].assert_not_called()
    all_external_mocked["validate_static_ip"].assert_not_called()
    all_external_mocked["resolve_network_settings"].assert_not_called()

    # virt-install received --network user,model=virtio (NOT network=...).
    virt_install_calls = [
        c
        for c in all_external_mocked["subprocess_run"].call_args_list
        if c.args[0][0] == "virt-install"
    ]
    assert len(virt_install_calls) == 1
    argv = virt_install_calls[0].args[0]
    assert "user,model=virtio" in argv
    assert not any(arg.startswith("network=") for arg in argv)


def test_run_network_type_passt_emits_passt_virt_install_arg(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """--network-type passt emits 'passt,model=virtio' to virt-install."""
    runner = CliRunner()
    result = runner.invoke(
        run,
        [
            "testvm.local",
            "--distro",
            "debian12",
            "--network-type",
            "passt",
            "--storage-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output

    all_external_mocked["get_network_info"].assert_not_called()

    argv = [
        c.args[0]
        for c in all_external_mocked["subprocess_run"].call_args_list
        if c.args[0][0] == "virt-install"
    ][0]
    assert "passt,model=virtio" in argv


def test_run_network_type_user_with_ip4_rejected(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """User-mode networking + --ip4 is a contradiction; refuse loudly.

    SLIRP and passt don't honour static IPs the way a managed libvirt
    NAT network does. If the operator passes both, fail at the CLI
    boundary before any state is created (no VM dir, no qcow2, no
    virt-install).
    """
    runner = CliRunner()
    result = runner.invoke(
        run,
        [
            "testvm.local",
            "--distro",
            "debian12",
            "--network-type",
            "user",
            "--ip4",
            "192.168.122.50",
            "--storage-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0
    assert "--ip4 is not supported" in result.output
    # No virt-install or qemu-img was invoked.
    assert not any(
        c.args[0][0] in {"virt-install", "qemu-img"}
        for c in all_external_mocked["subprocess_run"].call_args_list
    )


# ---------------------------------------------------------------------------
# Image catalog sanity
# ---------------------------------------------------------------------------


def test_run_passes_system_first_env_to_virt_install(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """``createvm`` invokes virt-install with system-first PATH.

    Regression for the Debian 13 portability bug: virt-install on
    bookworm-and-newer uses ``#!/usr/bin/env python3``. Without the
    env override, the venv's Python gets selected and ``import gi``
    fails. Asserts the ``env=`` kwarg on the virt-install call has
    ``/usr/bin:/usr/sbin`` at the front of PATH.
    """
    runner = CliRunner()
    result = runner.invoke(
        run, ["testvm.local", "--distro", "debian12", "--storage-root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output

    virt_install_calls = [
        c
        for c in all_external_mocked["subprocess_run"].call_args_list
        if c.args[0][0] == "virt-install"
    ]
    assert len(virt_install_calls) == 1
    env = virt_install_calls[0].kwargs["env"]
    assert env["PATH"].startswith(
        "/usr/bin:/usr/sbin"
    ), f"createvm must pass env with system bin paths first; got PATH={env['PATH']!r}"


def test_builtin_catalog_includes_known_distros() -> None:
    """The catalog has at least one stable distro family — regression guard.

    A future refactor that accidentally clears the catalog (or renames
    keys) would silently break ``--distro`` resolution. This locks the
    minimum surface and the shared image-entry schema.
    """
    assert "debian12" in BUILTIN_IMAGES
    assert "debian13" in BUILTIN_IMAGES
    # Every entry uses the Lvlab.yml `images:` schema (no os_variant/username
    # — those are derived). Required fields must be populated.
    for name, cfg in BUILTIN_IMAGES.items():
        assert cfg["image_url"].startswith("https://"), name
        assert cfg["checksum_type"] in {"sha256", "sha512"}, name
        assert cfg["network_version"] in {1, 2}, name


def test_run_unknown_distro_errors_with_available_list(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """An unknown --distro fails at the CLI with the available names."""
    runner = CliRunner()
    result = runner.invoke(
        run,
        ["testvm.local", "--distro", "nope", "--storage-root", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "Unknown distro 'nope'" in result.output
    assert "Available:" in result.output


def test_run_uses_manifest_image_when_present(
    all_external_mocked: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cwd Lvlab.yml contributes a manifest-only distro that resolves."""
    monkeypatch.setattr(
        cv_mod,
        "load_manifest_images",
        lambda: {"myapp": {"image_url": "http://h/myapp.qcow2", "network_version": 2}},
    )
    captured: dict = {}

    def fake_build(name, entry, image_dir):
        captured["name"] = name
        captured["image_dir"] = image_dir
        return all_external_mocked["fake_image"]

    monkeypatch.setattr(cv_mod, "_build_cloud_image", fake_build)

    runner = CliRunner()
    result = runner.invoke(
        run, ["testvm.local", "--distro", "myapp", "--storage-root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert captured["name"] == "myapp"
    # Cloud images share the lvlab cache with `lvlab up`.
    assert str(captured["image_dir"]) == "/var/lib/libvirt/images/lvlab"


def test_ensure_storage_root_writable_raises_with_guidance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The writability precheck raises with libvirt-group / 0771 guidance."""
    import typer

    monkeypatch.setattr(cv_mod.os, "access", lambda path, mode: False)
    with pytest.raises(typer.Exit):
        cv_mod._ensure_storage_root_writable(tmp_path)
    err = capsys.readouterr().err
    assert "not writable" in err
    assert "libvirt" in err


def test_run_writability_precheck_runs_before_image_work(
    all_external_mocked: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The storage precheck fails fast, before any image download/verify."""

    def boom(_root: Path) -> None:
        raise cv_mod._fail("storage root boom")

    monkeypatch.setattr(cv_mod, "_ensure_storage_root_writable", boom)

    runner = CliRunner()
    result = runner.invoke(
        run, ["testvm.local", "--distro", "debian12", "--storage-root", str(tmp_path)]
    )
    assert result.exit_code != 0
    assert "storage root boom" in result.output
    all_external_mocked["ensure_image_available"].assert_not_called()
