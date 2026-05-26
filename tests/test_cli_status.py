"""Unit tests for the ``lvlab status`` CLI command.

These tests stub the virsh helpers and :func:`parse_config` at the
``tkc_lvlab.cli`` import boundary so nothing here ever invokes ``virsh``
or libvirt. They lock in two things:

- The Phase 2 port: ``status`` uses ``virsh_list_all_names`` +
    ``virsh_domstate`` and only queries state for *deployed* domains
    (no N+1 ``virsh domstate`` for undeployed machines).
- The issue #103 reshape: machines and images render as the shared-style
    tables, the Images table merges the built-in default catalog with the
    manifest (labelling each image's ``source``), and built-in defaults
    appear even when the manifest doesn't reference them.

``cached`` reflects real on-disk state, so tests that assert it point
``cloud_image_basedir`` at an empty ``tmp_path`` to keep it deterministic;
the others simply don't assert on the cached column.
"""

from __future__ import annotations

from unittest import mock

from typer.testing import CliRunner

from tkc_lvlab import cli
from tkc_lvlab.cli import app
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
    config_defaults: dict | None = None,
) -> mock._patch:
    """Patch ``cli.parse_config`` with a deterministic tuple."""
    return mock.patch.object(
        cli,
        "parse_config",
        return_value=(
            env if env is not None else SAMPLE_ENV,
            images if images is not None else SAMPLE_IMAGES,
            config_defaults if config_defaults is not None else {},
            machines if machines is not None else SAMPLE_MACHINES,
        ),
    )


def test_status_happy_path_mixed_states_no_reason_suffix() -> None:
    """alpha running, beta shut off, gamma undeployed — rendered in the Machines table."""
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
        result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    assert "LvLab Environment Name: demo" in result.output
    # Table titles (shared-style tables, issue #103).
    assert "Machines" in result.output
    assert "Images" in result.output

    # Machine rows: name + bare state (running / shut off), undeployed for gamma.
    out = result.output
    assert "alpha" in out and "running" in out
    assert "beta" in out and "shut off" in out
    assert "gamma" in out and "undeployed" in out

    # Regression guard: the dropped state-reason suffix must not reappear.
    # The N+1 ``virsh domstate --reason`` avoidance is the real contract —
    # only the two DEPLOYED VMs are queried, never the undeployed one.
    assert "normal startup" not in out
    assert domstate_mock.call_count == 2

    # Image URLs surface in the Images table.
    assert "https://example.invalid/fedora.qcow2" in out
    assert "https://example.invalid/debian.qcow2" in out


def test_status_all_undeployed_skips_domstate_entirely() -> None:
    """No machines present on the hypervisor -> all 'undeployed', zero domstate calls."""
    runner = CliRunner()
    with (
        _patched_config(),
        mock.patch.object(cli, "virsh_list_all_names", return_value=[]),
        mock.patch.object(cli, "virsh_domstate") as domstate_mock,
    ):
        result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    out = result.output
    for vm in ("alpha", "beta", "gamma"):
        assert vm in out
    assert "undeployed" in out
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
        result = runner.invoke(app, ["status"])

    assert result.exit_code == 1
    # We never reach the per-machine loop if listing fails.
    domstate_mock.assert_not_called()
    # The Machines table is built only after a successful list, so its
    # header column must not have been rendered.
    assert "undeployed" not in result.output


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
        result = runner.invoke(app, ["status"])

    # Per-machine failure does NOT take the whole command down.
    assert result.exit_code == 0, result.output
    out = result.output
    assert "unknown (virsh error)" in out
    assert "running" in out
    assert "undeployed" in out  # gamma


def test_status_parse_config_typeerror_exits_nonzero() -> None:
    """If the manifest can't be parsed, status exits 1 (existing contract)."""
    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", side_effect=TypeError("bad config")),
        mock.patch.object(cli, "virsh_list_all_names") as list_mock,
    ):
        result = runner.invoke(app, ["status"])

    assert result.exit_code == 1
    # We never reach the virsh layer once parse_config fails.
    list_mock.assert_not_called()


def test_status_renders_env_name_then_machines_then_images_in_order() -> None:
    """The environment line, Machines table, and Images table appear in that order."""
    runner = CliRunner()
    with (
        _patched_config(machines=[]),  # empty machines is fine; we check section order
        mock.patch.object(cli, "virsh_list_all_names", return_value=[]),
        mock.patch.object(cli, "virsh_domstate"),
    ):
        result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    out = result.output
    env_idx = out.find("LvLab Environment Name: demo")
    machines_idx = out.find("Machines")
    images_idx = out.find("Images")
    assert env_idx != -1
    assert machines_idx != -1
    assert images_idx != -1
    assert env_idx < machines_idx < images_idx


def test_status_images_table_labels_source_and_shows_defaults(tmp_path) -> None:
    """Images table: manifest entries labelled 'manifest', built-ins 'default' and shown."""
    runner = CliRunner()
    # Manifest names two images, one of which (debian12) *overrides* a
    # built-in (collision -> manifest wins) and one custom (rocky9).
    images = {
        "debian12": {"image_url": "https://example.invalid/custom-debian12.qcow2"},
        "rocky9": {"image_url": "https://example.invalid/rocky9.qcow2"},
    }
    config_defaults = {"cloud_image_basedir": str(tmp_path)}  # empty -> cached "no"
    with (
        _patched_config(images=images, machines=[], config_defaults=config_defaults),
        mock.patch.object(cli, "virsh_list_all_names", return_value=[]),
        mock.patch.object(cli, "virsh_domstate"),
    ):
        result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    out = result.output

    # Both source labels are present.
    assert "manifest" in out
    assert "default" in out
    # A built-in NOT named by the manifest still shows up (defaults shown).
    assert "fedora44" in out
    assert "almalinux10" in out
    # The manifest's custom image shows up.
    assert "rocky9" in out
    # Collision: the manifest's debian12 URL wins over the built-in one.
    assert "https://example.invalid/custom-debian12.qcow2" in out
    assert (
        "https://cloud.debian.org/images/cloud/bookworm" not in out
    ), "built-in debian12 URL should be overridden by the manifest entry"


def test_status_image_missing_url_does_not_crash(tmp_path) -> None:
    """An images entry without image_url renders a placeholder, not a traceback."""
    runner = CliRunner()
    images = {"broken": {}}  # no image_url
    config_defaults = {"cloud_image_basedir": str(tmp_path)}
    with (
        _patched_config(images=images, machines=[], config_defaults=config_defaults),
        mock.patch.object(cli, "virsh_list_all_names", return_value=[]),
        mock.patch.object(cli, "virsh_domstate"),
    ):
        result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    assert "broken" in result.output
    assert "missing image_url" in result.output
