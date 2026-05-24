"""Unit tests for the ``lvlab snapshot {list,create,delete}`` CLI commands.

These tests lock in the Phase 2 step 3C touch-ups: cli.py must consume the
new return contracts from :class:`tkc_lvlab.utils.libvirt.Machine` snapshot
methods (``list_snapshots`` returns ``list[str]``, ``create_snapshot``
returns ``True`` or raises ``VirshError``, ``delete_snapshot`` returns
``None`` or raises ``VirshError``).

Strategy: stub ``parse_config``, ``get_machine_by_vm_name`` and ``Machine``
at the ``tkc_lvlab.cli`` import boundary so nothing here ever reads a
manifest, builds a Machine, or invokes ``virsh``.
"""

from __future__ import annotations

import logging
from unittest import mock

from typer.testing import CliRunner

from tkc_lvlab import cli
from tkc_lvlab.cli import snapshot
from tkc_lvlab.utils.virsh import VirshError


URI = "qemu:///session"


def _stub_parse_config() -> tuple[dict, dict, dict, list]:
    """Return a minimal ``parse_config`` tuple that the snapshot commands
    accept. Real values don't matter because :class:`Machine` is patched."""
    environment = {"name": "lab", "libvirt_uri": URI}
    images: dict = {}
    config_defaults: dict = {}
    machines = [{"vm_name": "web01"}]
    return environment, images, config_defaults, machines


def _make_machine_stub(**overrides) -> mock.MagicMock:
    """Build a MagicMock that looks enough like a Machine for the snapshot
    commands. The truthy ``exists_in_libvirt`` tuple lets the command pass
    its guard and reach the method under test."""
    machine = mock.MagicMock()
    machine.vm_name = "web01"
    machine.libvirt_vm_name = "web01_lab"
    machine.exists_in_libvirt.return_value = (True, "running", "booted")
    for key, value in overrides.items():
        setattr(machine, key, value)
    return machine


# ---------------------------------------------------------------------------
# snapshot list
# ---------------------------------------------------------------------------


def test_snapshot_list_renders_string_names_with_bullet_prefix() -> None:
    """Each name from ``list_snapshots`` lands in stdout with the bullet prefix."""
    machine = _make_machine_stub()
    machine.list_snapshots.return_value = ["snap1", "snap2"]

    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", return_value=_stub_parse_config()),
        mock.patch.object(
            cli, "get_machine_by_vm_name", return_value={"vm_name": "web01"}
        ),
        mock.patch.object(cli, "Machine", return_value=machine),
    ):
        result = runner.invoke(snapshot, ["list", "web01"])

    assert result.exit_code == 0, result.output
    assert "Listing snapshots for web01" in result.output
    assert "  - snap1" in result.output
    assert "  - snap2" in result.output


def test_snapshot_list_empty_prints_no_snapshots_message() -> None:
    """An empty ``list_snapshots`` result triggers the "No snapshots" branch."""
    machine = _make_machine_stub()
    machine.list_snapshots.return_value = []

    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", return_value=_stub_parse_config()),
        mock.patch.object(
            cli, "get_machine_by_vm_name", return_value={"vm_name": "web01"}
        ),
        mock.patch.object(cli, "Machine", return_value=machine),
    ):
        result = runner.invoke(snapshot, ["list", "web01"])

    assert result.exit_code == 0, result.output
    assert "No snapshots found for web01" in result.output


def test_snapshot_list_does_not_call_getName_on_string() -> None:
    """Regression guard for the step 3C contract change.

    Before 3B, list_snapshots returned libvirt virDomainSnapshot objects
    and cli.py called ``.getName()``. After 3B the return type is
    ``list[str]``. If cli.py still called ``.getName()`` on a string, the
    output would contain a ``<MagicMock`` repr or an ``AttributeError``
    crash. This test asserts the rendered output is exactly the bullet
    plus the name — no ``getName`` artifacts.
    """
    machine = _make_machine_stub()
    machine.list_snapshots.return_value = ["only-snap"]

    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", return_value=_stub_parse_config()),
        mock.patch.object(
            cli, "get_machine_by_vm_name", return_value={"vm_name": "web01"}
        ),
        mock.patch.object(cli, "Machine", return_value=machine),
    ):
        result = runner.invoke(snapshot, ["list", "web01"])

    assert result.exit_code == 0, result.output
    assert "getName" not in result.output
    assert "MagicMock" not in result.output
    assert "  - only-snap" in result.output


# ---------------------------------------------------------------------------
# snapshot create
# ---------------------------------------------------------------------------


def test_snapshot_create_happy_path_prints_success_using_snapshot_name() -> None:
    """``create_snapshot`` returning ``True`` produces a success line containing
    the user-supplied snapshot name (the return value no longer carries it)."""
    machine = _make_machine_stub()
    machine.create_snapshot.return_value = True

    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", return_value=_stub_parse_config()),
        mock.patch.object(
            cli, "get_machine_by_vm_name", return_value={"vm_name": "web01"}
        ),
        mock.patch.object(cli, "Machine", return_value=machine),
    ):
        result = runner.invoke(snapshot, ["create", "web01", "pre-upgrade"])

    assert result.exit_code == 0, result.output
    assert "Snapshot pre-upgrade created for web01" in result.output
    machine.create_snapshot.assert_called_once_with(URI, "pre-upgrade", None)


def test_snapshot_create_virsh_error_is_logged_and_does_not_crash(
    caplog,
) -> None:
    """When ``create_snapshot`` raises ``VirshError`` the CLI logs and exits
    cleanly (no traceback escaping to the user)."""
    machine = _make_machine_stub()
    machine.create_snapshot.side_effect = VirshError(
        1, "operation failed", ["snapshot-create", "web01_lab"]
    )

    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", return_value=_stub_parse_config()),
        mock.patch.object(
            cli, "get_machine_by_vm_name", return_value={"vm_name": "web01"}
        ),
        mock.patch.object(cli, "Machine", return_value=machine),
        caplog.at_level(logging.ERROR, logger="tkc_lvlab.cli"),
    ):
        result = runner.invoke(snapshot, ["create", "web01", "pre-upgrade"])

    assert result.exit_code == 0, result.output
    assert "Snapshot pre-upgrade created" not in result.output
    assert any(
        "Failed to create snapshot pre-upgrade for web01" in record.getMessage()
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# snapshot delete
# ---------------------------------------------------------------------------


def test_snapshot_delete_happy_path_prints_success_message() -> None:
    """``delete_snapshot`` returning ``None`` produces the success line."""
    machine = _make_machine_stub()
    machine.delete_snapshot.return_value = None

    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", return_value=_stub_parse_config()),
        mock.patch.object(
            cli, "get_machine_by_vm_name", return_value={"vm_name": "web01"}
        ),
        mock.patch.object(cli, "Machine", return_value=machine),
    ):
        result = runner.invoke(snapshot, ["delete", "web01", "pre-upgrade", "--force"])

    assert result.exit_code == 0, result.output
    assert "Snapshot pre-upgrade deleted from web01" in result.output
    machine.delete_snapshot.assert_called_once_with(URI, "pre-upgrade")


def test_snapshot_delete_virsh_error_is_logged_and_does_not_crash(
    caplog,
) -> None:
    """When ``delete_snapshot`` raises ``VirshError`` the CLI logs and exits 0."""
    machine = _make_machine_stub()
    machine.delete_snapshot.side_effect = VirshError(
        1, "snapshot not found", ["snapshot-delete", "web01_lab", "ghost"]
    )

    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", return_value=_stub_parse_config()),
        mock.patch.object(
            cli, "get_machine_by_vm_name", return_value={"vm_name": "web01"}
        ),
        mock.patch.object(cli, "Machine", return_value=machine),
        caplog.at_level(logging.ERROR, logger="tkc_lvlab.cli"),
    ):
        result = runner.invoke(snapshot, ["delete", "web01", "ghost", "--force"])

    assert result.exit_code == 0, result.output
    assert "Snapshot ghost deleted" not in result.output
    assert any(
        "Failed to delete snapshot ghost from web01" in record.getMessage()
        for record in caplog.records
    )


def test_snapshot_delete_aborted_when_confirm_returns_false() -> None:
    """Without --force, a "no" at the typer.confirm prompt aborts cleanly
    without invoking ``delete_snapshot``."""
    machine = _make_machine_stub()

    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", return_value=_stub_parse_config()),
        mock.patch.object(
            cli, "get_machine_by_vm_name", return_value={"vm_name": "web01"}
        ),
        mock.patch.object(cli, "Machine", return_value=machine),
    ):
        # Pipe "n" as the confirm input; no --force flag.
        result = runner.invoke(
            snapshot, ["delete", "web01", "pre-upgrade"], input="n\n"
        )

    assert result.exit_code == 0, result.output
    assert "deletion aborted for web01" in result.output
    machine.delete_snapshot.assert_not_called()
