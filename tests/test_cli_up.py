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
    # Realistic empties so the #106 one-time-password / SSH-hint reads behave:
    # opt out of the generated console password (that path has its own tests
    # below) so these orchestration tests stay deterministic and never shell
    # out to openssl; no interfaces -> DHCP -> generic SSH hint.
    m.cloud_init_config = {"password": False}
    m.interfaces = []
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


def test_up_exits_one_when_manifest_missing() -> None:
    """A missing Lvlab.yml (parse_config → None) → error log + exit 1 (#49).

    Locks the soft missing-file path through ConfigManager: the old code
    unpacked ``None`` into a TypeError; the manager surfaces ``loaded=False``
    and ``_load_config`` maps it to the same exit-1 the operator saw before.
    """
    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", return_value=None),
        mock.patch.object(cli, "logger") as mocked_logger,
        mock.patch.object(cli, "Machine") as machine_cls,
    ):
        result = runner.invoke(app, ["up", "alpha"])

    assert result.exit_code == 1, result.output
    machine_cls.assert_not_called()
    mocked_logger.error.assert_called_with("Could not parse config file.")


def test_up_create_parses_manifest_once_and_passes_machines(tmp_path) -> None:
    """The create path reads the manifest once and threads machines to cloud_init (#49).

    Proves the de-dup win at the command boundary: ``parse_config`` fires a
    single time for the whole ``up`` run, and the parsed machines list is
    handed to ``Machine.cloud_init`` (so it never re-reads the file itself).
    """
    runner = CliRunner()
    fake_machine = _make_fake_machine(deploy_returns=True, tmp_path=tmp_path)
    fake_iso = _make_fake_iso(tmp_path)

    with (
        _patched_config() as parse_config_mock,
        mock.patch.object(cli, "Machine", return_value=fake_machine),
        mock.patch.object(cli, "CloudImage"),
        mock.patch.object(cli, "CloudInitIso", return_value=fake_iso),
    ):
        result = runner.invoke(app, ["up", "alpha"])

    assert result.exit_code == 0, result.output
    parse_config_mock.assert_called_once()
    # cloud_init received the parsed machines list as its 3rd positional arg.
    _, _, machines_arg = fake_machine.cloud_init.call_args.args
    assert machines_arg == SAMPLE_MACHINES


# ---------------------------------------------------------------------------
# One-time console password + SSH hint (issue #106)
# ---------------------------------------------------------------------------


def test_up_generates_injects_and_prints_password_once(tmp_path) -> None:
    """First-time up with no configured password: generate, inject the hash,
    print the plaintext exactly once."""
    runner = CliRunner()
    fake_machine = _make_fake_machine(deploy_returns=True, tmp_path=tmp_path)
    fake_machine.cloud_init_config = {}  # no configured password -> generate
    fake_iso = _make_fake_iso(tmp_path)
    phrase = "Cedar-Spruce-Atlas-Pine"
    hashed = "$6$rounds=4096$salt$hash"

    with (
        _patched_config(),
        mock.patch.object(cli, "Machine", return_value=fake_machine),
        mock.patch.object(cli, "CloudImage"),
        mock.patch.object(cli, "CloudInitIso", return_value=fake_iso),
        mock.patch.object(
            cli, "generate_one_time_password", return_value=(phrase, hashed)
        ),
    ):
        result = runner.invoke(app, ["up", "alpha"])

    assert result.exit_code == 0, result.output
    # The generated hash is injected into cloud-init.
    assert fake_machine.cloud_init.call_args.kwargs["password_hash"] == hashed
    # The plaintext is shown exactly once.
    assert result.output.count(phrase) == 1


def test_up_respects_manifest_configured_password(tmp_path) -> None:
    """A manifest-configured cloud_init.passwd is respected: no generation,
    and no generated hash is injected."""
    runner = CliRunner()
    fake_machine = _make_fake_machine(deploy_returns=True, tmp_path=tmp_path)
    fake_machine.cloud_init_config = {"passwd": "$6$manifest$preset"}
    fake_iso = _make_fake_iso(tmp_path)

    with (
        _patched_config(),
        mock.patch.object(cli, "Machine", return_value=fake_machine),
        mock.patch.object(cli, "CloudImage"),
        mock.patch.object(cli, "CloudInitIso", return_value=fake_iso),
        mock.patch.object(cli, "generate_one_time_password") as gen,
    ):
        result = runner.invoke(app, ["up", "alpha"])

    assert result.exit_code == 0, result.output
    gen.assert_not_called()
    # The manifest passwd is rendered by Machine.cloud_init itself; up injects
    # no generated hash.
    assert fake_machine.cloud_init.call_args.kwargs["password_hash"] is None


def test_up_password_opt_out_generates_nothing(tmp_path) -> None:
    """cloud_init.password: false opts out — no generation, no injected hash."""
    runner = CliRunner()
    fake_machine = _make_fake_machine(deploy_returns=True, tmp_path=tmp_path)
    fake_machine.cloud_init_config = {"password": False}
    fake_iso = _make_fake_iso(tmp_path)

    with (
        _patched_config(),
        mock.patch.object(cli, "Machine", return_value=fake_machine),
        mock.patch.object(cli, "CloudImage"),
        mock.patch.object(cli, "CloudInitIso", return_value=fake_iso),
        mock.patch.object(cli, "generate_one_time_password") as gen,
    ):
        result = runner.invoke(app, ["up", "alpha"])

    assert result.exit_code == 0, result.output
    gen.assert_not_called()
    assert fake_machine.cloud_init.call_args.kwargs["password_hash"] is None
    assert "One-time VM password" not in result.output
