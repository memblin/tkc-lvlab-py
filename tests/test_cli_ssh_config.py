"""Unit tests for the ``lvlab ssh-config`` CLI command.

Locks the per-machine snippet rendering (Host / HostName / User /
IdentityFile lines and the "no static ip4" comment fallback) so the
cognitive-complexity refactor of the command body can move with a
safety net. The command body had 0% coverage before.
"""

from __future__ import annotations

from unittest import mock

from typer.testing import CliRunner

from tkc_lvlab import cli
from tkc_lvlab.cli import app


def _make_machine(
    vm_name: str = "web01",
    *,
    ip4: str | None = "10.0.0.1/24",
    user: str | None = None,
    pubkey: str | None = None,
) -> dict:
    machine: dict = {"vm_name": vm_name, "interfaces": []}
    if ip4 is not None:
        machine["interfaces"] = [{"ip4": ip4}]
    cloud_init: dict = {}
    if user is not None:
        cloud_init["user"] = user
    if pubkey is not None:
        cloud_init["pubkey"] = pubkey
    if cloud_init:
        machine["cloud_init"] = cloud_init
    return machine


def _invoke(argv: list[str], machines: list[dict], defaults: dict | None = None):
    runner = CliRunner()
    parse_return = ({"name": "test-env"}, {}, defaults or {}, machines)
    with mock.patch.object(cli, "parse_config", return_value=parse_return):
        return runner.invoke(app, ["ssh-config", *argv])


def test_ssh_config_emits_one_snippet_per_machine_when_no_vm_name() -> None:
    """No VM_NAME → one Host block per machine in the manifest."""
    machines = [
        _make_machine("web01", ip4="10.0.0.1/24"),
        _make_machine("db01", ip4="10.0.0.2/24"),
    ]
    result = _invoke([], machines)

    assert result.exit_code == 0, result.output
    assert "Host web01" in result.output
    assert "HostName 10.0.0.1" in result.output
    assert "Host db01" in result.output
    assert "HostName 10.0.0.2" in result.output


def test_ssh_config_emits_single_snippet_when_vm_name_given() -> None:
    """VM_NAME selects exactly one machine."""
    machines = [
        _make_machine("web01", ip4="10.0.0.1/24"),
        _make_machine("db01", ip4="10.0.0.2/24"),
    ]
    result = _invoke(["web01"], machines)

    assert result.exit_code == 0, result.output
    assert "Host web01" in result.output
    assert "Host db01" not in result.output


def test_ssh_config_exits_with_error_when_vm_name_unknown() -> None:
    """Unknown VM_NAME → echo error and exit 1."""
    machines = [_make_machine("web01")]
    result = _invoke(["ghost"], machines)

    assert result.exit_code == 1
    assert "Machine ghost not found in manifest." in result.output


def test_ssh_config_emits_dhcp_comment_when_machine_has_no_static_ip() -> None:
    """No static ip4 → HostName line is replaced with the DHCP comment."""
    machines = [_make_machine("web01", ip4=None)]
    result = _invoke([], machines)

    assert result.exit_code == 0, result.output
    assert "HostName" not in result.output.split("# HostName")[0]
    assert "# HostName not resolvable from manifest" in result.output


def test_ssh_config_includes_user_line_when_user_configured() -> None:
    """``cloud_init.user`` (per-machine or default) → User line."""
    machines = [_make_machine("web01", user="ansible")]
    result = _invoke([], machines)

    assert result.exit_code == 0, result.output
    assert "User ansible" in result.output


def test_ssh_config_includes_identity_file_when_pubkey_is_a_path() -> None:
    """Pubkey containing '/' → IdentityFile derived by stripping .pub."""
    machines = [_make_machine("web01", pubkey="/home/me/.ssh/id_ed25519.pub")]
    result = _invoke([], machines)

    assert result.exit_code == 0, result.output
    assert "IdentityFile /home/me/.ssh/id_ed25519" in result.output


def test_ssh_config_omits_identity_file_when_pubkey_is_a_literal_key() -> None:
    """Pubkey without '/' or '~' is a literal ssh key, not a path."""
    machines = [_make_machine("web01", pubkey="ssh-ed25519 AAAA...")]
    result = _invoke([], machines)

    assert result.exit_code == 0, result.output
    assert "IdentityFile" not in result.output


def test_ssh_config_merges_default_cloud_init_with_per_machine_override() -> None:
    """``config_defaults.cloud_init`` provides user; machine overrides pubkey."""
    machines = [_make_machine("web01", pubkey="/keys/web01.pub")]
    defaults = {"cloud_init": {"user": "lab-admin"}}
    result = _invoke([], machines, defaults)

    assert result.exit_code == 0, result.output
    assert "User lab-admin" in result.output
    assert "IdentityFile /keys/web01" in result.output


def test_ssh_config_handles_parse_failure_via_typeerror() -> None:
    """parse_config raising TypeError → echo error and exit 1."""
    runner = CliRunner()
    with mock.patch.object(cli, "parse_config", side_effect=TypeError):
        result = runner.invoke(app, ["ssh-config"])

    assert result.exit_code == 1
    assert "Could not parse config file." in result.output
