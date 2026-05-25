"""Unit tests for :mod:`tkc_lvlab.scripts.deletevm`.

``deletevm`` ports the ``lvscripts-py`` ``deletevm`` UI (colored output,
``--force`` confirmation, ``--version``, the snapshot-deletion prompt) but
acts on the **raw libvirt domain name**. Locked-in contracts:

- The user-supplied name is the raw domain name — looked up exactly, no
    ``Lvlab.yml`` translation. A name with no defined domain errors before
    any mutation.
- The VM is destroyed (force-off, ignored if already off) and undefined
    (prompting before deleting blocking snapshots).
- The per-VM storage directory under the one-off root is removed **if it
    exists** — it is not required. Passing a manifest VM's real
    ``<vm>_<env>`` domain name therefore removes the domain and leaves its
    (elsewhere-nested) disks behind.
- A failed undefine leaves the storage directory for inspection.
"""

from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

import pytest
from typer.testing import CliRunner

from tkc_lvlab.scripts import deletevm as dv_mod
from tkc_lvlab.scripts.deletevm import run
from tkc_lvlab.utils.virsh import VirshError


_SNAPSHOT_STDERR = (
    "error: Requested operation is not valid: cannot delete inactive domain "
    "with 2 snapshot metadata"
)


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch every libvirt-touching helper in the deletevm namespace."""
    mocks = {
        "run_virsh": mock.Mock(
            return_value=CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        ),
        "vm_exists": mock.Mock(return_value=True),
        "delete_all_snapshots": mock.Mock(),
    }
    monkeypatch.setattr(dv_mod, "run_virsh", mocks["run_virsh"])
    monkeypatch.setattr(dv_mod, "vm_exists", mocks["vm_exists"])
    monkeypatch.setattr(dv_mod, "delete_all_snapshots", mocks["delete_all_snapshots"])
    return mocks


def _virsh_calls(stub: dict, subcommand: str) -> list:
    """Return run_virsh calls whose argv starts with ``subcommand``."""
    return [c for c in stub["run_virsh"].call_args_list if c.args[1][0] == subcommand]


def test_happy_path_force_removes_domain_and_dir(stub: dict, tmp_path: Path) -> None:
    """A defined VM with a storage dir is destroyed, undefined, and dir removed."""
    vm_dir = tmp_path / "testvm.local"
    vm_dir.mkdir()
    (vm_dir / "disk0.qcow2").write_text("fake")

    result = CliRunner().invoke(
        run, ["testvm.local", "--force", "--storage-root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output

    destroy = _virsh_calls(stub, "destroy")
    assert len(destroy) == 1
    assert destroy[0].args[1] == ["destroy", "testvm.local"]
    # Force-off ignores a nonzero exit (VM may already be off).
    assert destroy[0].kwargs.get("check") is False

    undefine = _virsh_calls(stub, "undefine")
    assert len(undefine) == 1
    assert undefine[0].args[1] == ["undefine", "testvm.local"]

    assert not vm_dir.exists()


def test_removes_domain_by_raw_name_without_storage_dir(
    stub: dict, tmp_path: Path
) -> None:
    """A defined domain is removed even with no one-off storage dir.

    This is the manifest-by-full-domain-name case: ``deletevm
    web01_lab`` matches the libvirt domain and undefines it; the missing
    flat storage dir is expected (its disks are nested elsewhere) and is
    not an error — undefine is the operative effect.
    """
    # No vm_dir created under tmp_path.
    result = CliRunner().invoke(
        run, ["web01_lab", "--force", "--storage-root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output

    undefine = _virsh_calls(stub, "undefine")
    assert len(undefine) == 1
    assert undefine[0].args[1] == ["undefine", "web01_lab"]


def test_undefined_domain_refuses(stub: dict, tmp_path: Path) -> None:
    """A name with no defined libvirt domain errors before any mutation."""
    stub["vm_exists"].return_value = False

    result = CliRunner().invoke(
        run, ["testvm.local", "--force", "--storage-root", str(tmp_path)]
    )
    assert result.exit_code != 0
    assert "is not defined" in result.output
    assert _virsh_calls(stub, "destroy") == []
    assert _virsh_calls(stub, "undefine") == []


def test_confirmation_no_aborts(stub: dict, tmp_path: Path) -> None:
    """Without --force, answering 'n' aborts cleanly (exit 0, nothing destroyed)."""
    vm_dir = tmp_path / "testvm.local"
    vm_dir.mkdir()

    result = CliRunner().invoke(
        run, ["testvm.local", "--storage-root", str(tmp_path)], input="n\n"
    )
    assert result.exit_code == 0, result.output
    assert "Aborted." in result.output
    assert _virsh_calls(stub, "destroy") == []
    assert _virsh_calls(stub, "undefine") == []
    assert vm_dir.exists()


def test_snapshot_undefine_prompts_then_retries(stub: dict, tmp_path: Path) -> None:
    """A snapshot-blocked undefine prompts, deletes snapshots, and retries."""
    vm_dir = tmp_path / "testvm.local"
    vm_dir.mkdir()
    undefine_attempts = {"n": 0}

    def fake_run(uri, args, **kwargs):
        if args[0] == "undefine":
            undefine_attempts["n"] += 1
            if undefine_attempts["n"] == 1:
                raise VirshError(1, _SNAPSHOT_STDERR, ["undefine"])
        return CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    stub["run_virsh"].side_effect = fake_run

    result = CliRunner().invoke(
        run,
        ["testvm.local", "--force", "--storage-root", str(tmp_path)],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    stub["delete_all_snapshots"].assert_called_once()
    assert undefine_attempts["n"] == 2  # initial failure + retry after cleanup
    assert not vm_dir.exists()


def test_snapshot_prompt_declined_aborts(stub: dict, tmp_path: Path) -> None:
    """Declining the snapshot-deletion prompt aborts and leaves the dir."""
    vm_dir = tmp_path / "testvm.local"
    vm_dir.mkdir()

    def fake_run(uri, args, **kwargs):
        if args[0] == "undefine":
            raise VirshError(1, _SNAPSHOT_STDERR, ["undefine"])
        return CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    stub["run_virsh"].side_effect = fake_run

    result = CliRunner().invoke(
        run,
        ["testvm.local", "--force", "--storage-root", str(tmp_path)],
        input="n\n",
    )
    assert result.exit_code != 0
    assert "Aborted: snapshots were not deleted" in result.output
    stub["delete_all_snapshots"].assert_not_called()
    assert vm_dir.exists()


def test_undefine_failure_leaves_storage_dir(stub: dict, tmp_path: Path) -> None:
    """A non-snapshot undefine failure errors and leaves the dir for inspection."""
    vm_dir = tmp_path / "testvm.local"
    vm_dir.mkdir()
    sentinel = vm_dir / "i-should-survive"
    sentinel.write_text("evidence")

    def fake_run(uri, args, **kwargs):
        if args[0] == "undefine":
            raise VirshError(1, "error: some other undefine failure", ["undefine"])
        return CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    stub["run_virsh"].side_effect = fake_run

    result = CliRunner().invoke(
        run, ["testvm.local", "--force", "--storage-root", str(tmp_path)]
    )
    assert result.exit_code != 0
    assert "Failed to undefine" in result.output
    assert sentinel.exists(), "deletevm wiped files even though undefine failed"


def test_version_flag() -> None:
    """--version prints 'deletevm <version>' and exits 0."""
    result = CliRunner().invoke(run, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("deletevm ")
