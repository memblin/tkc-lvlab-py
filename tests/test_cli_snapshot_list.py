"""Unit tests for the ``lvlab snapshot list`` CLI command.

Locks the four code paths (success-with-snapshots, success-no-snapshots,
not-deployed, not-in-manifest, parse-failure) so the cognitive-complexity
refactor that consolidates the resolve+exists boilerplate can move with
a safety net.
"""

from __future__ import annotations

from unittest import mock

from typer.testing import CliRunner

from tkc_lvlab import cli
from tkc_lvlab.cli import app


SAMPLE_ENV = {"name": "demo", "libvirt_uri": "qemu:///session"}
SAMPLE_MACHINES = [{"vm_name": "alpha", "os": "debian13"}]


def _patched_config():
    return mock.patch.object(
        cli,
        "parse_config",
        return_value=(SAMPLE_ENV, {}, {}, SAMPLE_MACHINES),
    )


def _make_machine(*, exists: bool, snapshots: list[str] | None = None) -> mock.Mock:
    m = mock.Mock()
    m.vm_name = "alpha"
    m.libvirt_vm_name = "alpha_demo"
    m.exists_in_libvirt.return_value = (exists, "running" if exists else "", "")
    m.list_snapshots.return_value = snapshots or []
    return m


def test_snapshot_list_prints_each_snapshot() -> None:
    """Existing VM with snapshots → one line per snapshot."""
    machine = _make_machine(exists=True, snapshots=["baseline", "pre-upgrade"])
    runner = CliRunner()
    with (
        _patched_config(),
        mock.patch.object(cli, "Machine", return_value=machine),
    ):
        result = runner.invoke(app, ["snapshot", "list", "alpha"])

    assert result.exit_code == 0, result.output
    assert "Listing snapshots for alpha" in result.output
    assert "- baseline" in result.output
    assert "- pre-upgrade" in result.output


def test_snapshot_list_reports_none_when_empty() -> None:
    """Existing VM but no snapshots → the "No snapshots found" message."""
    machine = _make_machine(exists=True, snapshots=[])
    runner = CliRunner()
    with (
        _patched_config(),
        mock.patch.object(cli, "Machine", return_value=machine),
    ):
        result = runner.invoke(app, ["snapshot", "list", "alpha"])

    assert result.exit_code == 0, result.output
    assert "No snapshots found for alpha" in result.output


def test_snapshot_list_warns_when_machine_not_deployed() -> None:
    """Machine in manifest but absent from libvirt → warning, no crash."""
    machine = _make_machine(exists=False)
    runner = CliRunner()
    with (
        _patched_config(),
        mock.patch.object(cli, "Machine", return_value=machine),
        mock.patch.object(cli, "logger") as mocked_logger,
    ):
        result = runner.invoke(app, ["snapshot", "list", "alpha"])

    assert result.exit_code == 0, result.output
    machine.list_snapshots.assert_not_called()
    warn_fmts = [c.args[0] for c in mocked_logger.warning.call_args_list]
    assert any("is not deployed" in fmt for fmt in warn_fmts), warn_fmts


def test_snapshot_list_errors_when_vm_not_in_manifest() -> None:
    """Unknown vm_name → logger.error, no Machine construction, exit 0."""
    runner = CliRunner()
    with (
        _patched_config(),
        mock.patch.object(cli, "Machine") as machine_cls,
        mock.patch.object(cli, "logger") as mocked_logger,
    ):
        result = runner.invoke(app, ["snapshot", "list", "ghost"])

    assert result.exit_code == 0, result.output
    machine_cls.assert_not_called()
    error_fmts = [c.args[0] for c in mocked_logger.error.call_args_list]
    assert any("Machine not found in manifest" in fmt for fmt in error_fmts), error_fmts


def test_snapshot_list_exits_one_on_parse_config_typeerror() -> None:
    """parse_config raising TypeError → error log + exit 1."""
    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", side_effect=TypeError),
        mock.patch.object(cli, "logger") as mocked_logger,
    ):
        result = runner.invoke(app, ["snapshot", "list", "alpha"])

    assert result.exit_code == 1
    mocked_logger.error.assert_called_with("Could not parse config file.")
