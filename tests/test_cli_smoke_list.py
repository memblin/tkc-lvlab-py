"""Unit tests for the experimental ``lvlab smoke --list`` preview.

`--list` resolves every manifest machine into a SmokeCase (the same
``build_cases`` the runner uses), renders a preview table, and exits 0
without booting anything. Hard guarantee: when ``--list`` is set,
``run_smoke`` is never called.
"""

from __future__ import annotations

from unittest import mock

from typer.testing import CliRunner

from tkc_lvlab import cli
from tkc_lvlab.cli import app


def _machine(
    vm_name: str,
    *,
    ip4: str | None = None,
    os_value: str = "debian12",
    memory: int = 1024,
) -> dict:
    machine: dict = {
        "vm_name": vm_name,
        "hostname": vm_name,
        "os": os_value,
        "interfaces": [{"name": "eth0"}],
        "memory": memory,
        "cpu": 1,
    }
    if ip4 is not None:
        machine["interfaces"][0]["ip4"] = ip4
    return machine


def _invoke(argv: list[str], machines: list[dict]):
    runner = CliRunner()
    parse_return = (
        {"name": "test-env", "libvirt_uri": "qemu:///system"},
        {
            "debian12": {"image_url": "https://example/debian12.qcow2"},
            "fedora44": {"image_url": "https://example/fedora44.qcow2"},
        },
        {"interfaces": {"network": "default"}, "domain": "test.local"},
        machines,
    )

    # Patch `parse_config` at TWO seams: cli (for everywhere the cli reads
    # it) and smoke (so build_cases doesn't re-read the disk).
    with (
        mock.patch.object(cli, "parse_config", return_value=parse_return),
        mock.patch("tkc_lvlab.smoke.parse_config", return_value=parse_return),
        mock.patch("tkc_lvlab.cli.run_smoke") as run_smoke_mock,
    ):
        result = runner.invoke(app, ["smoke", *argv])
    return result, run_smoke_mock


def test_smoke_list_exits_zero_with_one_row_per_machine() -> None:
    """--list resolves every machine and exits 0 without running."""
    machines = [
        _machine("web01", ip4="10.0.0.5/24", os_value="debian12"),
        _machine("db01", os_value="fedora44"),
    ]
    result, run_smoke_mock = _invoke(["--list"], machines)

    assert result.exit_code == 0, result.output
    # Both machine names appear in the rendered preview
    assert "web01" in result.output
    assert "db01" in result.output
    # And the resolved metadata: distro + mode + IP
    assert "debian12" in result.output
    assert "fedora44" in result.output
    assert "10.0.0.5" in result.output  # static
    # No VMs were booted
    run_smoke_mock.assert_not_called()


def test_smoke_list_classifies_static_vs_dhcp() -> None:
    """The preview shows ``static`` for an ip4-having machine, ``dhcp`` otherwise."""
    machines = [
        _machine("web01", ip4="10.0.0.5/24"),  # static
        _machine("db01"),  # dhcp
    ]
    result, _ = _invoke(["--list"], machines)
    assert result.exit_code == 0
    assert "static" in result.output
    assert "dhcp" in result.output


def test_smoke_list_does_not_invoke_run_smoke() -> None:
    """--list must NEVER call run_smoke (which boots real VMs)."""
    machines = [_machine("web01", ip4="10.0.0.5/24")]
    _result, run_smoke_mock = _invoke(["--list"], machines)
    run_smoke_mock.assert_not_called()


def test_smoke_without_list_still_invokes_run_smoke() -> None:
    """No --list → existing behaviour calls run_smoke."""
    machines = [_machine("web01", ip4="10.0.0.5/24")]
    runner = CliRunner()
    parse_return = (
        {"name": "test-env"},
        {},
        {"interfaces": {}},
        machines,
    )
    with mock.patch.object(cli, "parse_config", return_value=parse_return):
        with mock.patch("tkc_lvlab.cli.run_smoke", return_value=0) as run_smoke_mock:
            result = runner.invoke(app, ["smoke"])
    assert result.exit_code == 0, result.output
    run_smoke_mock.assert_called_once()


def test_smoke_list_handles_empty_manifest_gracefully() -> None:
    """No machines in manifest → exit 0, plain "no machines" message."""
    result, run_smoke_mock = _invoke(["--list"], [])
    assert result.exit_code == 0, result.output
    assert "no machines" in result.output.lower() or "0" in result.output
    run_smoke_mock.assert_not_called()
