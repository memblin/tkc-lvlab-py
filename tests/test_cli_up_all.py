"""Unit tests for the experimental ``lvlab up --all`` flag.

`--all` walks every machine in the manifest sequentially. ``vm_name`` and
``--all`` are mutually exclusive; specifying neither is an error.
"""

from __future__ import annotations

from unittest import mock

from typer.testing import CliRunner

from tkc_lvlab import cli
from tkc_lvlab.cli import app


def _machine(vm_name: str) -> dict:
    return {
        "vm_name": vm_name,
        "hostname": vm_name,
        "os": "debian12",
        "interfaces": [{"name": "eth0"}],
    }


def _invoke(argv: list[str], machines: list[dict]):
    runner = CliRunner()
    parse_return = (
        {"name": "test-env", "libvirt_uri": "qemu:///system"},
        {"debian12": {"image_url": "https://example/debian12.qcow2"}},
        {"interfaces": {}, "domain": "test.local"},
        machines,
    )
    # Stub the per-machine worker — the tests assert on its invocation
    # pattern, not on Machine / virsh / virt-install.
    with (
        mock.patch.object(cli, "parse_config", return_value=parse_return),
        mock.patch.object(cli, "_up_one") as up_one_mock,
    ):
        result = runner.invoke(app, ["up", *argv])
    return result, up_one_mock


def test_up_all_invokes_one_call_per_manifest_machine() -> None:
    """--all walks every machine in manifest order, sequentially."""
    machines = [_machine("web01"), _machine("db01"), _machine("queue01")]
    result, up_one_mock = _invoke(["--all"], machines)

    assert result.exit_code == 0, result.output
    assert up_one_mock.call_count == 3
    # Manifest order preserved.
    called_vm_names = [call.args[0]["vm_name"] for call in up_one_mock.call_args_list]
    assert called_vm_names == ["web01", "db01", "queue01"]


def test_up_all_with_single_machine_still_works() -> None:
    """--all on a one-machine manifest is a no-op except for the one boot."""
    machines = [_machine("web01")]
    result, up_one_mock = _invoke(["--all"], machines)
    assert result.exit_code == 0
    assert up_one_mock.call_count == 1


def test_up_with_vm_name_still_targets_that_machine() -> None:
    """Existing behaviour: lvlab up <vm> boots exactly that machine."""
    machines = [_machine("web01"), _machine("db01")]
    result, up_one_mock = _invoke(["web01"], machines)
    assert result.exit_code == 0, result.output
    assert up_one_mock.call_count == 1
    assert up_one_mock.call_args.args[0]["vm_name"] == "web01"


def test_up_vm_name_and_all_together_is_a_mutex_error() -> None:
    """`lvlab up web01 --all` is contradictory → exit 1 with explanation."""
    machines = [_machine("web01")]
    result, up_one_mock = _invoke(["web01", "--all"], machines)
    assert result.exit_code == 1
    assert "VM_NAME" in result.output or "vm_name" in result.output
    assert "--all" in result.output
    up_one_mock.assert_not_called()


def test_up_no_args_at_all_is_an_error_with_helpful_message() -> None:
    """No VM_NAME, no --all → exit 1 saying what to do."""
    machines = [_machine("web01")]
    result, up_one_mock = _invoke([], machines)
    assert result.exit_code == 1
    # Either form of the helpful message
    assert "--all" in result.output
    up_one_mock.assert_not_called()


def test_up_all_skips_remaining_when_machines_is_empty() -> None:
    """--all against an empty manifest → exit 0, no calls, plain message."""
    result, up_one_mock = _invoke(["--all"], [])
    assert result.exit_code == 0, result.output
    up_one_mock.assert_not_called()
    assert (
        "no machines" in result.output.lower() or "0 machines" in result.output.lower()
    )
