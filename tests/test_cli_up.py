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


def test_up_exits_with_clear_error_when_machine_os_missing_from_images(
    tmp_path,
) -> None:
    """``lvlab up`` must exit non-zero with a readable error when the
    machine's ``os`` value has no matching key in the manifest's
    ``images`` dict.

    Regression for the AttributeError crash a smoke test surfaced when
    a manifest entry had ``os: debian13.local`` (typo / wrong field) —
    ``images.get("debian13.local")`` returned None, and CloudImage.__init__
    crashed on ``config.get(...)`` with an opaque NoneType traceback.
    The operator-readable message names the missing key and lists what
    image keys are actually defined.
    """
    runner = CliRunner()
    # Machine.os intentionally not in SAMPLE_IMAGES.
    fake_machine = _make_fake_machine(deploy_returns=True, tmp_path=tmp_path)
    fake_machine.os = "debian13.local"

    with (
        _patched_config(),
        mock.patch.object(cli, "Machine", return_value=fake_machine),
        # CloudImage must NOT be called — the error gates before it.
        mock.patch.object(cli, "CloudImage") as cloud_image_mock,
    ):
        result = runner.invoke(app, ["up", "alpha"])

    assert result.exit_code != 0
    cloud_image_mock.assert_not_called()
    # Deploy must also not run.
    fake_machine.deploy.assert_not_called()


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


def _make_existing_machine(status_state: str) -> mock.Mock:
    """Build a Machine mock that takes the 'already exists' branch."""
    m = mock.Mock()
    m.vm_name = "alpha"
    m.libvirt_vm_name = "alpha_demo"
    m.exists_in_libvirt.return_value = (True, status_state, None)
    m.poweron.return_value = 0
    return m


def test_up_powers_on_when_machine_exists_and_is_shut_off() -> None:
    """``exists`` + state ∈ {shut off, crashed} → poweron is invoked."""
    runner = CliRunner()
    fake_machine = _make_existing_machine("shut off")

    with (
        _patched_config(),
        mock.patch.object(cli, "Machine", return_value=fake_machine),
    ):
        result = runner.invoke(app, ["up", "alpha"])

    assert result.exit_code == 0, result.output
    fake_machine.poweron.assert_called_once()
    assert "Starting virtual machine alpha" in result.output


def test_up_logs_error_when_poweron_returns_nonzero() -> None:
    """Existing machine + poweron > 0 → error log, but exit 0 still."""
    runner = CliRunner()
    fake_machine = _make_existing_machine("shut off")
    fake_machine.poweron.return_value = 1

    with (
        _patched_config(),
        mock.patch.object(cli, "Machine", return_value=fake_machine),
        mock.patch.object(cli, "logger") as mocked_logger,
    ):
        result = runner.invoke(app, ["up", "alpha"])

    assert result.exit_code == 0, result.output
    error_fmts = [c.args[0] for c in mocked_logger.error.call_args_list]
    assert any("Problem powering on VM" in f for f in error_fmts), error_fmts


def test_up_is_noop_when_machine_already_running() -> None:
    """``exists`` + state == running → echo + no poweron."""
    runner = CliRunner()
    fake_machine = _make_existing_machine("running")

    with (
        _patched_config(),
        mock.patch.object(cli, "Machine", return_value=fake_machine),
    ):
        result = runner.invoke(app, ["up", "alpha"])

    assert result.exit_code == 0, result.output
    fake_machine.poweron.assert_not_called()
    assert "is running already" in result.output


def test_up_logs_error_when_vm_not_in_manifest() -> None:
    """Unknown VM name → logger.error, no Machine construction, exit 0."""
    runner = CliRunner()
    with (
        _patched_config(),
        mock.patch.object(cli, "Machine") as machine_cls,
        mock.patch.object(cli, "logger") as mocked_logger,
    ):
        result = runner.invoke(app, ["up", "ghost"])

    assert result.exit_code == 0, result.output
    machine_cls.assert_not_called()
    error_fmts = [c.args[0] for c in mocked_logger.error.call_args_list]
    assert any("Machine %s not found" in f for f in error_fmts), error_fmts


def test_up_exits_one_on_parse_config_typeerror() -> None:
    """``parse_config`` raising TypeError → error log + exit 1."""
    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", side_effect=TypeError),
        mock.patch.object(cli, "logger") as mocked_logger,
    ):
        result = runner.invoke(app, ["up", "alpha"])

    assert result.exit_code == 1, result.output
    mocked_logger.error.assert_called_with("Could not parse config file.")
