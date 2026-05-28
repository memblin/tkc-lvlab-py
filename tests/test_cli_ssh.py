"""Unit tests for the experimental ``lvlab ssh`` command.

Two layers:

- ``_ssh_command_argv`` is a pure helper — tested directly for argv shape.
- ``lvlab ssh`` integration tests patch ``parse_config`` and ``os.execvp``
  at the ``tkc_lvlab.cli`` boundary so the command resolves end-to-end
  without spawning a subprocess.
"""

from __future__ import annotations

from unittest import mock

from typer.testing import CliRunner

from tkc_lvlab import cli
from tkc_lvlab.cli import _ssh_command_argv, app

# --- _ssh_command_argv (pure) -------------------------------------------------


def test_ssh_command_argv_carries_ephemeral_lab_opts() -> None:
    """Argv embeds the four ephemeral-lab opts that match #127's ssh-config."""
    argv = _ssh_command_argv("10.0.0.5", user="debian", identity_file=None)
    joined = " ".join(argv)
    assert "StrictHostKeyChecking=no" in joined
    assert "UserKnownHostsFile=/dev/null" in joined
    assert "CheckHostIP=no" in joined
    assert "LogLevel=ERROR" in joined


def test_ssh_command_argv_targets_user_at_host_when_user_given() -> None:
    argv = _ssh_command_argv("10.0.0.5", user="debian", identity_file=None)
    assert argv[-1] == "debian@10.0.0.5"


def test_ssh_command_argv_falls_back_to_bare_host_when_no_user() -> None:
    argv = _ssh_command_argv("10.0.0.5", user=None, identity_file=None)
    assert argv[-1] == "10.0.0.5"


def test_ssh_command_argv_passes_identity_file_when_given() -> None:
    argv = _ssh_command_argv(
        "10.0.0.5", user="debian", identity_file="/home/me/.ssh/id_ed25519"
    )
    assert "-i" in argv
    assert "/home/me/.ssh/id_ed25519" in argv


def test_ssh_command_argv_omits_identity_flag_when_no_file() -> None:
    argv = _ssh_command_argv("10.0.0.5", user="debian", identity_file=None)
    assert "-i" not in argv


# --- lvlab ssh integration ----------------------------------------------------


def _machine(
    vm_name: str = "web01",
    *,
    ip4: str | None = "10.0.0.5/24",
    user: str | None = None,
    pubkey: str | None = None,
    os_value: str = "debian12",
) -> dict:
    machine: dict = {"vm_name": vm_name, "os": os_value, "interfaces": []}
    if ip4 is not None:
        machine["interfaces"] = [{"name": "eth0", "ip4": ip4}]
    cloud_init: dict = {}
    if user is not None:
        cloud_init["user"] = user
    if pubkey is not None:
        cloud_init["pubkey"] = pubkey
    if cloud_init:
        machine["cloud_init"] = cloud_init
    return machine


def _invoke_ssh(
    argv: list[str],
    machines: list[dict],
    defaults: dict | None = None,
    *,
    dhcp_lease_ip: str | None = None,
):
    """Invoke ``lvlab ssh`` with parse_config + os.execvp + run_virsh stubbed."""
    runner = CliRunner()
    # Machine.__init__ reads config_defaults["interfaces"] unconditionally,
    # so a realistic defaults dict always carries an (possibly empty) one.
    full_defaults = {"interfaces": {}, **(defaults or {})}
    parse_return = (
        {"name": "test-env", "libvirt_uri": "qemu:///system"},
        {"debian12": {"image_url": "https://example/debian12.qcow2"}},
        full_defaults,
        machines,
    )

    captured: dict[str, object] = {}

    def _capture_execvp(file: str, args: list[str]) -> None:
        captured["execvp"] = (file, list(args))
        # don't actually exec — return so the test can observe

    # virsh domifaddr stub: return an output the parser can read
    if dhcp_lease_ip:
        domifaddr_stdout = (
            " Name       MAC address          Protocol     Address\n"
            "-----------------------------------------------------\n"
            f" vnet0      52:54:00:ab:cd:ef    ipv4         {dhcp_lease_ip}/24\n"
        )
    else:
        domifaddr_stdout = (
            " Name       MAC address          Protocol     Address\n"
            "-----------------------------------------------------\n"
        )
    virsh_result = mock.Mock(returncode=0, stdout=domifaddr_stdout, stderr="")

    with (
        mock.patch.object(cli, "parse_config", return_value=parse_return),
        mock.patch.object(cli.os, "execvp", side_effect=_capture_execvp),
        mock.patch("tkc_lvlab.cli.run_virsh", return_value=virsh_result),
    ):
        result = runner.invoke(app, ["ssh", *argv])
    return result, captured


def test_ssh_uses_static_ip_when_machine_has_one() -> None:
    """Static ip4 on the first interface → ssh targets that IP."""
    result, captured = _invoke_ssh(["web01"], [_machine("web01", ip4="10.0.0.5/24")])
    assert result.exit_code == 0, result.output
    _file, args = captured["execvp"]  # type: ignore[misc]
    assert args[-1].endswith("@10.0.0.5")


def test_ssh_resolves_dhcp_lease_when_no_static_ip() -> None:
    """No static ip4 → look up the running domain's DHCP lease via virsh."""
    result, captured = _invoke_ssh(
        ["web01"], [_machine("web01", ip4=None)], dhcp_lease_ip="192.168.122.123"
    )
    assert result.exit_code == 0, result.output
    _file, args = captured["execvp"]  # type: ignore[misc]
    assert args[-1].endswith("@192.168.122.123")


def test_ssh_exits_with_error_when_no_ip_resolvable() -> None:
    """No static + no DHCP lease → exit 1 with a helpful message."""
    result, _captured = _invoke_ssh(
        ["web01"], [_machine("web01", ip4=None)], dhcp_lease_ip=None
    )
    assert result.exit_code == 1
    assert "Could not resolve" in result.output or "not running" in result.output


def test_ssh_uses_manifest_user_when_configured() -> None:
    """cloud_init.user wins over the image default."""
    result, captured = _invoke_ssh(["web01"], [_machine("web01", user="ansible")])
    assert result.exit_code == 0
    _file, args = captured["execvp"]  # type: ignore[misc]
    assert args[-1].startswith("ansible@")


def test_ssh_falls_back_to_image_default_username() -> None:
    """No manifest user → derive from the image (debian12 → debian)."""
    result, captured = _invoke_ssh(["web01"], [_machine("web01", user=None)])
    assert result.exit_code == 0
    _file, args = captured["execvp"]  # type: ignore[misc]
    assert args[-1].startswith("debian@")


def test_ssh_exits_with_error_when_vm_name_unknown() -> None:
    """Unknown VM_NAME → echo error and exit 1."""
    result, _captured = _invoke_ssh(["ghost"], [_machine("web01")])
    assert result.exit_code == 1
    assert "Machine ghost not found" in result.output
