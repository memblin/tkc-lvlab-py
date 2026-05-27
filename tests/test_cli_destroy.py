"""Unit tests for the ``lvlab destroy`` CLI command.

Locks the four code paths (force-success, force-failure, prompt-aborted,
not-deployed, not-in-manifest, parse-failure) before the cognitive-complexity
refactor moves ``destroy`` to the shared :func:`_resolve_existing_machine`
helper. The command body had no test coverage before this file.
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


def _make_machine(*, exists: bool, destroy_returns: bool = True) -> mock.Mock:
    m = mock.Mock()
    m.vm_name = "alpha"
    m.libvirt_vm_name = "alpha_demo"
    m.exists_in_libvirt.return_value = (exists, "running" if exists else "", "")
    m.destroy.return_value = destroy_returns
    return m


def test_destroy_force_success_prints_success_message() -> None:
    """``--force`` + ``Machine.destroy`` returning True → success echo."""
    machine = _make_machine(exists=True, destroy_returns=True)
    runner = CliRunner()
    with (
        _patched_config(),
        mock.patch.object(cli, "Machine", return_value=machine),
    ):
        result = runner.invoke(app, ["destroy", "alpha", "--force"])

    assert result.exit_code == 0, result.output
    assert "Destruction appears successful for alpha" in result.output
    machine.destroy.assert_called_once()


def test_destroy_force_failure_logs_error_and_does_not_crash() -> None:
    """``Machine.destroy`` returning False → error log, no crash."""
    machine = _make_machine(exists=True, destroy_returns=False)
    runner = CliRunner()
    with (
        _patched_config(),
        mock.patch.object(cli, "Machine", return_value=machine),
        mock.patch.object(cli, "logger") as mocked_logger,
    ):
        result = runner.invoke(app, ["destroy", "alpha", "--force"])

    assert result.exit_code == 0, result.output
    machine.destroy.assert_called_once()
    error_fmts = [c.args[0] for c in mocked_logger.error.call_args_list]
    assert any(
        "Destruction appears to have failed" in fmt for fmt in error_fmts
    ), error_fmts


def test_destroy_aborted_when_confirm_returns_false() -> None:
    """Without --force, a "no" at the prompt aborts cleanly."""
    machine = _make_machine(exists=True)
    runner = CliRunner()
    with (
        _patched_config(),
        mock.patch.object(cli, "Machine", return_value=machine),
    ):
        result = runner.invoke(app, ["destroy", "alpha"], input="n\n")

    assert result.exit_code == 0, result.output
    assert "Destruction aborted for alpha" in result.output
    machine.destroy.assert_not_called()


def test_destroy_warns_when_machine_not_deployed() -> None:
    """Machine in manifest but absent from libvirt → warning, no destroy call."""
    machine = _make_machine(exists=False)
    runner = CliRunner()
    with (
        _patched_config(),
        mock.patch.object(cli, "Machine", return_value=machine),
        mock.patch.object(cli, "logger") as mocked_logger,
    ):
        result = runner.invoke(app, ["destroy", "alpha", "--force"])

    assert result.exit_code == 0, result.output
    machine.destroy.assert_not_called()
    warn_fmts = [c.args[0] for c in mocked_logger.warning.call_args_list]
    assert any("is not deployed" in fmt for fmt in warn_fmts), warn_fmts


def test_destroy_errors_when_vm_not_in_manifest() -> None:
    """Unknown vm_name → logger.error, no Machine construction, exit 0."""
    runner = CliRunner()
    with (
        _patched_config(),
        mock.patch.object(cli, "Machine") as machine_cls,
        mock.patch.object(cli, "logger") as mocked_logger,
    ):
        result = runner.invoke(app, ["destroy", "ghost", "--force"])

    assert result.exit_code == 0, result.output
    machine_cls.assert_not_called()
    error_fmts = [c.args[0] for c in mocked_logger.error.call_args_list]
    assert any("not found in manifest" in fmt for fmt in error_fmts), error_fmts


def test_destroy_exits_one_on_parse_config_typeerror() -> None:
    """parse_config raising TypeError → error log + exit 1."""
    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", side_effect=TypeError),
        mock.patch.object(cli, "logger") as mocked_logger,
    ):
        result = runner.invoke(app, ["destroy", "alpha", "--force"])

    assert result.exit_code == 1
    mocked_logger.error.assert_called_with("Could not parse config file.")
