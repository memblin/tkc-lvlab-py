"""Unit tests for the ``lvlab status`` CLI command.

These tests stub the virsh helpers and :func:`parse_config` at the
``tkc_lvlab.cli`` import boundary so nothing here ever invokes ``virsh``
or libvirt. They lock in the Phase 2 port: ``status`` now uses
``virsh_list_all_names`` + ``virsh_domstate`` and the user-visible
output no longer carries a parenthesized state-reason suffix.
"""

from __future__ import annotations

from unittest import mock

from click.testing import CliRunner

from tkc_lvlab import cli
from tkc_lvlab.cli import status
from tkc_lvlab.utils.virsh import VirshError


# A representative manifest tuple matching parse_config's return shape:
# (environment, images, config_defaults, machines).
SAMPLE_ENV = {"name": "demo", "libvirt_uri": "qemu:///session"}
SAMPLE_IMAGES = {
    "fedora-40": {"image_url": "https://example.invalid/fedora.qcow2"},
    "debian-12": {"image_url": "https://example.invalid/debian.qcow2"},
}
SAMPLE_MACHINES = [
    {"vm_name": "alpha"},
    {"vm_name": "beta"},
    {"vm_name": "gamma"},
]


def _patched_config(
    env: dict | None = None,
    images: dict | None = None,
    machines: list | None = None,
) -> mock._patch:
    """Patch ``cli.parse_config`` with a deterministic tuple."""
    return mock.patch.object(
        cli,
        "parse_config",
        return_value=(
            env if env is not None else SAMPLE_ENV,
            images if images is not None else SAMPLE_IMAGES,
            {},
            machines if machines is not None else SAMPLE_MACHINES,
        ),
    )


def test_status_happy_path_mixed_states_no_reason_suffix() -> None:
    """alpha running, beta shut off, gamma undeployed — all rendered humanly."""
    runner = CliRunner()
    # Only the two deployed VMs come back from virsh list.
    listed = ["alpha_demo", "beta_demo"]

    def domstate_side_effect(uri: str, name: str) -> str:
        assert uri == "qemu:///session"
        if name == "alpha_demo":
            return "running"
        if name == "beta_demo":
            return "shut off"
        raise AssertionError(f"unexpected domstate call for {name}")

    with (
        _patched_config(),
        mock.patch.object(cli, "virsh_list_all_names", return_value=listed),
        mock.patch.object(
            cli, "virsh_domstate", side_effect=domstate_side_effect
        ) as domstate_mock,
    ):
        result = runner.invoke(status, [])

    assert result.exit_code == 0, result.output
    assert "LvLab Environment Name: demo" in result.output
    assert "Machines Defined:" in result.output
    assert "Images Used:" in result.output

    # Per-machine lines, with humanized state and no reason suffix.
    assert "  - alpha is the machine is running" in result.output
    assert "  - beta is the machine is shut off" in result.output
    assert "  - gamma is undeployed" in result.output

    # Regression guard: dropped reason string. None of the successful
    # state lines may carry a parenthesized reason like ``(normal startup
    # from boot)``. The natural form to forbid is the substring ``is the
    # machine is <state> (`` which is exactly what the old output emitted.
    assert "is the machine is running (" not in result.output
    assert "is the machine is shut off (" not in result.output
    # And no successful-state line ends with ``)``.
    for line in result.output.splitlines():
        if "is the machine is" in line:
            assert not line.rstrip().endswith(")"), line

    # Image URLs surface in the Images Used block.
    assert "fedora-40 from https://example.invalid/fedora.qcow2" in result.output
    assert "debian-12 from https://example.invalid/debian.qcow2" in result.output

    # Only the two deployed VMs are queried; the undeployed one is not.
    assert domstate_mock.call_count == 2


def test_status_all_undeployed_skips_domstate_entirely() -> None:
    """No machines present on the hypervisor -> all 'undeployed', zero domstate calls."""
    runner = CliRunner()
    with (
        _patched_config(),
        mock.patch.object(cli, "virsh_list_all_names", return_value=[]),
        mock.patch.object(cli, "virsh_domstate") as domstate_mock,
    ):
        result = runner.invoke(status, [])

    assert result.exit_code == 0, result.output
    assert "  - alpha is undeployed" in result.output
    assert "  - beta is undeployed" in result.output
    assert "  - gamma is undeployed" in result.output
    domstate_mock.assert_not_called()


def test_status_list_failure_exits_nonzero() -> None:
    """When virsh list itself fails, the command logs an error and exits 1."""
    runner = CliRunner()
    err = VirshError(1, "error: failed to connect to the hypervisor", ["list"])
    with (
        _patched_config(),
        mock.patch.object(cli, "virsh_list_all_names", side_effect=err),
        mock.patch.object(cli, "virsh_domstate") as domstate_mock,
    ):
        result = runner.invoke(status, [])

    assert result.exit_code == 1
    # We never reach the per-machine loop if listing fails.
    domstate_mock.assert_not_called()
    # The "Machines Defined:" banner must not have been printed either —
    # the failure happens before that section.
    assert "Machines Defined:" not in result.output


def test_status_per_machine_domstate_failure_continues() -> None:
    """One VM's domstate failing renders an inline fallback; others render normally."""
    runner = CliRunner()
    listed = ["alpha_demo", "beta_demo"]

    def domstate_side_effect(uri: str, name: str) -> str:
        if name == "alpha_demo":
            raise VirshError(1, "error: Domain not found", ["domstate", name])
        if name == "beta_demo":
            return "running"
        raise AssertionError(f"unexpected domstate call for {name}")

    with (
        _patched_config(),
        mock.patch.object(cli, "virsh_list_all_names", return_value=listed),
        mock.patch.object(cli, "virsh_domstate", side_effect=domstate_side_effect),
    ):
        result = runner.invoke(status, [])

    # Per-machine failure does NOT take the whole command down.
    assert result.exit_code == 0, result.output
    assert "  - alpha is unknown (virsh error)" in result.output
    assert "  - beta is the machine is running" in result.output
    assert "  - gamma is undeployed" in result.output


def test_status_parse_config_typeerror_exits_nonzero() -> None:
    """If the manifest can't be parsed, status exits 1 (existing contract)."""
    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", side_effect=TypeError("bad config")),
        mock.patch.object(cli, "virsh_list_all_names") as list_mock,
    ):
        result = runner.invoke(status, [])

    assert result.exit_code == 1
    # We never reach the virsh layer once parse_config fails.
    list_mock.assert_not_called()


def test_status_section_headers_unchanged_for_ux_continuity() -> None:
    """The 'LvLab Environment Name', 'Machines Defined', and 'Images Used' headers stay."""
    runner = CliRunner()
    with (
        _patched_config(machines=[]),  # empty machines is fine; we only check headers
        mock.patch.object(cli, "virsh_list_all_names", return_value=[]),
        mock.patch.object(cli, "virsh_domstate"),
    ):
        result = runner.invoke(status, [])

    assert result.exit_code == 0, result.output
    # Exact strings, in this order.
    out = result.output
    env_idx = out.find("LvLab Environment Name: demo")
    machines_idx = out.find("Machines Defined:")
    images_idx = out.find("Images Used:")
    assert env_idx != -1
    assert machines_idx != -1
    assert images_idx != -1
    assert env_idx < machines_idx < images_idx


def test_status_does_not_reach_libvirt_python() -> None:
    """Regression guard: the command must not touch the libvirt-python helpers.

    After Phase 2 step 5, ``status`` is virsh-only. ``connect_to_libvirt`` /
    ``get_machine_state`` are also slated for deletion in step 6 — this test
    guarantees no late re-entry sneaks into ``status`` between now and then.
    """
    runner = CliRunner()
    # connect_to_libvirt / get_machine_state were removed from cli's imports
    # in this step. Patch them on the libvirt module (where they still live
    # until step 6 deletes them) and assert nothing in the status code path
    # imports or calls them.
    with (
        _patched_config(),
        mock.patch.object(cli, "virsh_list_all_names", return_value=[]),
        mock.patch.object(cli, "virsh_domstate"),
        mock.patch("tkc_lvlab.utils.libvirt.connect_to_libvirt") as connect_mock,
        mock.patch("tkc_lvlab.utils.libvirt.get_machine_state") as state_mock,
    ):
        result = runner.invoke(status, [])

    assert result.exit_code == 0, result.output
    connect_mock.assert_not_called()
    state_mock.assert_not_called()
