"""Unit tests for the ``lvlab up`` CLI command.

Focused on regression coverage for failure-path exit-code handling.
The ``up`` command goes through a long pipeline (parse_config ->
Machine -> CloudImage -> VirtualDisk -> CloudInitIso ->
Machine.deploy); these tests stub the collaborators at the
``tkc_lvlab.cli`` import boundary so the run never touches libvirt,
qemu, or the network.
"""

from __future__ import annotations

from unittest import mock

from typer.testing import CliRunner

from tkc_lvlab import cli
from tkc_lvlab.cli import app


SAMPLE_ENV = {"name": "demo", "libvirt_uri": "qemu:///session"}
SAMPLE_IMAGES = {"debian13": {"image_url": "https://example.invalid/debian.qcow2"}}
SAMPLE_MACHINES = [{"vm_name": "alpha", "os": "debian13"}]


def _patched_config() -> mock._patch:
    """Patch ``cli.parse_config`` with a single-machine manifest."""
    return mock.patch.object(
        cli,
        "parse_config",
        return_value=(SAMPLE_ENV, SAMPLE_IMAGES, {}, SAMPLE_MACHINES),
    )


def _make_fake_machine(deploy_returns: bool, tmp_path) -> mock.Mock:
    """Build a Machine mock that takes the 'create' branch of ``up``."""
    m = mock.Mock()
    m.vm_name = "alpha"
    m.libvirt_vm_name = "alpha_demo"
    m.config_fpath = str(tmp_path)
    m.os = "debian13"
    m.exists_in_libvirt.return_value = (False, None, None)
    m.cloud_init.return_value = ("metadata", "userdata", "network")
    m.deploy.return_value = deploy_returns
    return m


def _make_fake_iso(tmp_path) -> mock.Mock:
    iso = mock.Mock()
    iso.fpath = str(tmp_path / "cidata.iso")
    iso.write.return_value = True
    return iso


def test_up_exits_nonzero_when_deploy_fails(tmp_path) -> None:
    """``lvlab up`` must exit non-zero if ``Machine.deploy`` returns False.

    Regression for the bug where deploy's False return was logged but
    swallowed, leaving the CLI to exit 0 even when virt-install failed.
    Caught in the wild on hosts where virt-install rejected the
    ``--os-variant`` (old osinfo-db) or couldn't load ``gi`` (Debian 13
    venv PATH).
    """
    runner = CliRunner()
    fake_machine = _make_fake_machine(deploy_returns=False, tmp_path=tmp_path)
    fake_iso = _make_fake_iso(tmp_path)

    with (
        _patched_config(),
        mock.patch.object(cli, "Machine", return_value=fake_machine),
        mock.patch.object(cli, "CloudImage"),
        mock.patch.object(cli, "CloudInitIso", return_value=fake_iso),
    ):
        result = runner.invoke(app, ["up", "alpha"])

    assert result.exit_code != 0, result.output
    fake_machine.deploy.assert_called_once()


def test_up_exits_zero_when_deploy_succeeds(tmp_path) -> None:
    """Happy-path sanity check: deploy True -> exit 0.

    Pairs with the failure-path test above so a future refactor can't
    accidentally make ``up`` always exit non-zero.
    """
    runner = CliRunner()
    fake_machine = _make_fake_machine(deploy_returns=True, tmp_path=tmp_path)
    fake_iso = _make_fake_iso(tmp_path)

    with (
        _patched_config(),
        mock.patch.object(cli, "Machine", return_value=fake_machine),
        mock.patch.object(cli, "CloudImage"),
        mock.patch.object(cli, "CloudInitIso", return_value=fake_iso),
    ):
        result = runner.invoke(app, ["up", "alpha"])

    assert result.exit_code == 0, result.output
    fake_machine.deploy.assert_called_once()
