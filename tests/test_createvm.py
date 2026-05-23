"""Unit tests for :mod:`tkc_lvlab.scripts.createvm`.

These tests use Click's :class:`CliRunner` against the ``run`` command
and patch every external interaction at the import boundary so nothing
hits the real virsh, qemu-img, virt-install, openssl, or filesystem
beyond the test's own ``tmp_path``.

What they lock in:

- The ``oneoff-`` domain prefix and ``<storage_root>/<vm_name>/`` storage
    convention from the Phase 6 architecture lock.
- ``DependencyError`` from the tooling check translates to a nonzero
    exit + ClickException-rendered message.
- The ``--copy`` flag selects the cp+resize disk strategy; without it,
    qemu-img is invoked with ``-b`` (backing file).
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
from click.testing import CliRunner

from tkc_lvlab.scripts import createvm as cv_mod
from tkc_lvlab.scripts.createvm import (
    BUILTIN_IMAGES,
    domain_name_for,
    parse_ip4_option,
    run,
    storage_dir_for,
)
from tkc_lvlab.utils.network import LibvirtNetworkError, LibvirtNetworkInfo
from tkc_lvlab.utils.requirements import DependencyError


# ---------------------------------------------------------------------------
# Naming + storage path helpers
# ---------------------------------------------------------------------------


def test_domain_name_for_adds_oneoff_prefix() -> None:
    """The Phase 6 naming lock — oneoff-<name> is what hits libvirt."""
    assert domain_name_for("testvm.local") == "oneoff-testvm.local"
    assert domain_name_for("bare") == "oneoff-bare"


def test_storage_dir_for_under_oneoff_root(tmp_path: Path) -> None:
    """Per-VM storage lands under <root>/<vm_name>/, not in <root>/ directly."""
    assert storage_dir_for("alpha", root=tmp_path) == tmp_path / "alpha"


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

    # Stub out CloudInitIso.write so it doesn't touch real pycdlib.
    monkeypatch.setattr(
        "tkc_lvlab.scripts.createvm.CloudInitIso.write", lambda self, p: True
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

    # virt-install was invoked with the oneoff- prefix on the domain name.
    subprocess_run = all_external_mocked["subprocess_run"]
    virt_install_calls = [
        c for c in subprocess_run.call_args_list if c.args[0][0] == "virt-install"
    ]
    assert len(virt_install_calls) == 1
    argv = virt_install_calls[0].args[0]
    assert "--name=oneoff-testvm.local" in argv
    # Default network on a NAT setup.
    assert any(arg.startswith("network=default,") for arg in argv)


def test_run_uses_backing_file_disk_by_default(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """Without --copy, qemu-img is invoked with -b (backing-file mode)."""
    runner = CliRunner()
    result = runner.invoke(
        run, ["testvm.local", "--distro", "debian12", "--storage-root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output

    qemu_img_calls = [
        c
        for c in all_external_mocked["subprocess_run"].call_args_list
        if c.args[0][0] == "qemu-img"
    ]
    # Exactly one qemu-img call (backing-file create).
    assert len(qemu_img_calls) == 1
    assert qemu_img_calls[0].args[0][1] == "create"
    assert "-b" in qemu_img_calls[0].args[0]


def test_run_copy_flag_uses_cp_plus_resize(
    all_external_mocked: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With --copy, the disk is shutil.copyfile'd then qemu-img resize'd."""
    copy_mock = mock.Mock()
    monkeypatch.setattr("tkc_lvlab.scripts.createvm.shutil.copyfile", copy_mock)

    runner = CliRunner()
    result = runner.invoke(
        run,
        [
            "testvm.local",
            "--distro",
            "debian12",
            "--copy",
            "--storage-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output

    # shutil.copyfile was called once with image -> disk path.
    copy_mock.assert_called_once()

    # qemu-img was called for resize, not for create.
    qemu_img_calls = [
        c
        for c in all_external_mocked["subprocess_run"].call_args_list
        if c.args[0][0] == "qemu-img"
    ]
    assert len(qemu_img_calls) == 1
    assert qemu_img_calls[0].args[0][1] == "resize"


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
# Image catalog sanity
# ---------------------------------------------------------------------------


def test_builtin_catalog_includes_known_distros() -> None:
    """The catalog has at least one stable distro family — regression guard.

    A future refactor that accidentally clears the catalog (or renames
    keys) would silently break the --distro click.Choice. This locks
    the minimum surface.
    """
    assert "fedora40" in BUILTIN_IMAGES
    assert "debian12" in BUILTIN_IMAGES
    # Every entry must have the required fields populated.
    for name, entry in BUILTIN_IMAGES.items():
        assert entry.image_url.startswith("https://"), name
        assert entry.checksum_type in {"sha256", "sha512"}, name
        assert entry.network_version in {1, 2}, name
        assert entry.default_username, name
