"""Unit tests for :mod:`tkc_lvlab.scripts.destroyvm`.

Locked-in contracts:

- The user-supplied name is translated to ``oneoff-<vm_name>`` before
    any libvirt lookup happens. Bare names are NEVER consulted — that
    keeps manifest VMs invisible.
- A missing one-off domain produces a clear error mentioning
    ``lvlab destroy`` as the alternative.
- Running VMs are force-off'd before undefine; already-shut-off VMs
    skip the force-off step.
- Snapshot fallback wires through ``undefine_with_snapshot_cleanup``.
- Storage directory is removed only after a successful undefine; a
    failed undefine leaves the files for inspection.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from tkc_lvlab.scripts import destroyvm as dv_mod
from tkc_lvlab.scripts.destroyvm import run
from tkc_lvlab.utils.virsh import VirshError


URI = "qemu:///system"


@pytest.fixture
def stub_libvirt(monkeypatch: pytest.MonkeyPatch):
    """Patch every libvirt-touching helper in the destroyvm namespace."""
    mocks = {
        "virsh_list_all_names": mock.Mock(return_value=["oneoff-testvm.local"]),
        "virsh_domstate": mock.Mock(return_value="running"),
        "run_virsh": mock.Mock(),
        "undefine_with_snapshot_cleanup": mock.Mock(),
    }
    monkeypatch.setattr(dv_mod, "virsh_list_all_names", mocks["virsh_list_all_names"])
    monkeypatch.setattr(dv_mod, "virsh_domstate", mocks["virsh_domstate"])
    monkeypatch.setattr(dv_mod, "run_virsh", mocks["run_virsh"])
    monkeypatch.setattr(
        dv_mod,
        "undefine_with_snapshot_cleanup",
        mocks["undefine_with_snapshot_cleanup"],
    )
    return mocks


def test_happy_path_force_running_vm(stub_libvirt, tmp_path: Path) -> None:
    """A running VM is force-off'd, undefined, and its dir removed."""
    vm_dir = tmp_path / "testvm.local"
    vm_dir.mkdir()
    (vm_dir / "disk0.qcow2").write_text("fake")

    runner = CliRunner()
    result = runner.invoke(
        run,
        ["testvm.local", "--force", "--storage-root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output

    # The libvirt name used is oneoff-prefixed.
    stub_libvirt["virsh_domstate"].assert_called_once_with(
        mock.ANY, "oneoff-testvm.local"
    )
    # Running state → force-off via virsh destroy.
    destroy_calls = [
        c for c in stub_libvirt["run_virsh"].call_args_list if c.args[1][0] == "destroy"
    ]
    assert len(destroy_calls) == 1
    assert destroy_calls[0].args[1] == ["destroy", "oneoff-testvm.local"]

    # Undefine via the snapshot-cleanup helper.
    stub_libvirt["undefine_with_snapshot_cleanup"].assert_called_once()

    # Storage dir is gone.
    assert not vm_dir.exists()


def test_shut_off_vm_skips_force_off(stub_libvirt, tmp_path: Path) -> None:
    """An already-shut-off VM does NOT get a virsh destroy call (it's a no-op anyway, but cleanly skip).

    Regression guard: a redundant destroy on a shut-off VM is harmless
    but emits a warning. Cleaner to skip it.
    """
    stub_libvirt["virsh_domstate"].return_value = "shut off"
    vm_dir = tmp_path / "testvm.local"
    vm_dir.mkdir()

    runner = CliRunner()
    result = runner.invoke(
        run,
        ["testvm.local", "--force", "--storage-root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output

    destroy_calls = [
        c for c in stub_libvirt["run_virsh"].call_args_list if c.args[1][0] == "destroy"
    ]
    assert destroy_calls == []


def test_missing_one_off_domain_does_not_consult_bare_name(
    stub_libvirt, tmp_path: Path
) -> None:
    """If oneoff-<name> isn't on the domain list, error out — don't peek at the bare name.

    This is THE cross-contamination guarantee from the Phase 6
    architecture lock: a manifest VM named 'testvm.local' must stay
    invisible to destroyvm.
    """
    # Domain list contains the bare name (manifest VM) but NOT the oneoff prefix.
    stub_libvirt["virsh_list_all_names"].return_value = [
        "testvm.local_dev",  # a manifest VM in env 'dev'
        "testvm.local",  # someone's bare-named domain
    ]

    runner = CliRunner()
    result = runner.invoke(
        run,
        ["testvm.local", "--force", "--storage-root", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "oneoff-testvm.local" in result.output
    # And we never queried domstate at all — early exit before that.
    stub_libvirt["virsh_domstate"].assert_not_called()
    # No destroy/undefine attempted.
    stub_libvirt["undefine_with_snapshot_cleanup"].assert_not_called()


def test_undefine_failure_leaves_storage_dir(stub_libvirt, tmp_path: Path) -> None:
    """If undefine fails, the VM dir stays — operator can inspect what's left."""
    stub_libvirt["undefine_with_snapshot_cleanup"].side_effect = VirshError(
        1, "error: snapshot deletion failed", ["undefine"]
    )

    vm_dir = tmp_path / "testvm.local"
    vm_dir.mkdir()
    sentinel = vm_dir / "i-should-survive"
    sentinel.write_text("evidence")

    runner = CliRunner()
    result = runner.invoke(
        run,
        ["testvm.local", "--force", "--storage-root", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert sentinel.exists(), "destroyvm wiped files even though undefine failed"


def test_list_failure_exits_with_clear_message(stub_libvirt, tmp_path: Path) -> None:
    """A virsh list failure (URI unreachable, virsh missing) surfaces cleanly."""
    stub_libvirt["virsh_list_all_names"].side_effect = VirshError(
        127, "virsh binary not found in PATH; install libvirt-clients", ["list"]
    )

    runner = CliRunner()
    result = runner.invoke(
        run,
        ["testvm.local", "--force", "--storage-root", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "libvirt-clients" in result.output


def test_confirmation_prompt_no_aborts(stub_libvirt, tmp_path: Path) -> None:
    """Without --force, answering 'n' aborts cleanly (exit 0, nothing destroyed)."""
    runner = CliRunner()
    result = runner.invoke(
        run,
        ["testvm.local", "--storage-root", str(tmp_path)],
        input="n\n",
    )
    assert result.exit_code == 0, result.output
    stub_libvirt["undefine_with_snapshot_cleanup"].assert_not_called()
    # virsh destroy also never fires.
    destroy_calls = [
        c for c in stub_libvirt["run_virsh"].call_args_list if c.args[1][0] == "destroy"
    ]
    assert destroy_calls == []
