"""Unit tests for :mod:`tkc_lvlab.scripts.deletevm`.

``deletevm`` ports the ``lvscripts-py`` ``deletevm`` UI (colored output,
``--force`` confirmation, ``--version``, the snapshot-deletion prompt) but
acts on the **raw libvirt domain name**. Locked-in contracts:

- The user-supplied name is the raw domain name — looked up exactly, no
    ``Lvlab.yml`` translation. A name with no defined domain errors before
    any mutation.
- The VM is destroyed (force-off, ignored if already off) and undefined.
    Snapshot presence is detected **up front** (``virsh snapshot-list
    --name``) so the confirmation tiers branch on it:
    - no ``--force``: tier-1 ("irreversible") then, if snapshots present,
        tier-2 ("snapshots present; remove them?").
    - ``--force`` alone: tier-1 skipped only when there are no snapshots;
        tier-2 still fires when snapshots are present.
    - ``--force --snapshots-too``: fully non-interactive (no prompts).
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
from tkc_lvlab.utils import snapshot_cleanup as sc_mod
from tkc_lvlab.utils.virsh import VirshError


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch every libvirt-touching helper in the deletevm namespace.

    ``virsh_snapshot_names`` defaults to "no snapshots"; tests that exercise
    the snapshot tiers set ``stub["virsh_snapshot_names"].return_value`` to a
    non-empty list.
    """
    mocks = {
        "run_virsh": mock.Mock(
            return_value=CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        ),
        "vm_exists": mock.Mock(return_value=True),
        "virsh_snapshot_names": mock.Mock(return_value=[]),
    }
    monkeypatch.setattr(dv_mod, "run_virsh", mocks["run_virsh"])
    monkeypatch.setattr(dv_mod, "vm_exists", mocks["vm_exists"])
    monkeypatch.setattr(dv_mod, "virsh_snapshot_names", mocks["virsh_snapshot_names"])
    # The one-shot undefine (issue #96) routes through snapshot_cleanup's
    # run_virsh; funnel it into the same mock so `_virsh_calls(stub, "undefine")`
    # captures it.
    monkeypatch.setattr(sc_mod, "run_virsh", mocks["run_virsh"])
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


def test_no_color_flag_disables_color(
    stub: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``deletevm --no-color`` flips the global colour switch before any output,
    so the shared ``secho`` wrapper strips ANSI even on a TTY (issue #131)."""
    from tkc_lvlab.utils import output

    monkeypatch.delenv("NO_COLOR", raising=False)
    output.set_no_color(False)
    try:
        result = CliRunner().invoke(
            run,
            ["--no-color", "testvm.local", "--force", "--storage-root", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert output.color_disabled() is True
    finally:
        output.set_no_color(False)


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


# ---------------------------------------------------------------------------
# Snapshot tier matrix. Snapshot presence is detected UP FRONT via
# virsh_snapshot_names, so the prompts branch on it before any mutation.
# Ordering invariant for the snapshot path: destroy -> undefine (the undefine
# drops any snapshots in one shot via --snapshots-metadata, issue #96).
# ---------------------------------------------------------------------------


def test_no_force_tier1_then_tier2_deletes_snapshots(
    stub: dict, tmp_path: Path
) -> None:
    """No --force, snapshots present: tier-1 then tier-2, both 'y' -> cleanup.

    Both prompts must appear and the snapshot deletion must happen between
    destroy and undefine.
    """
    stub["virsh_snapshot_names"].return_value = ["snap-a", "snap-b"]
    vm_dir = tmp_path / "testvm.local"
    vm_dir.mkdir()

    result = CliRunner().invoke(
        run,
        ["testvm.local", "--storage-root", str(tmp_path)],
        input="y\ny\n",
    )
    assert result.exit_code == 0, result.output
    assert "Are you sure?" in result.output  # tier-1
    assert "snapshots" in result.output.lower()  # tier-2
    # Ordering: destroy precedes undefine (which drops snapshots in one shot,
    # issue #96).
    assert _virsh_calls(stub, "destroy")
    assert _virsh_calls(stub, "undefine")
    assert not vm_dir.exists()


def test_no_force_tier1_declined_aborts_before_snapshot_check(
    stub: dict, tmp_path: Path
) -> None:
    """Answering 'n' to tier-1 aborts (exit 0); tier-2 never runs."""
    stub["virsh_snapshot_names"].return_value = ["snap-a"]
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


def test_no_force_tier2_declined_fails(stub: dict, tmp_path: Path) -> None:
    """Tier-1 'y' but tier-2 'n' fails nonzero and deletes nothing."""
    stub["virsh_snapshot_names"].return_value = ["snap-a"]
    vm_dir = tmp_path / "testvm.local"
    vm_dir.mkdir()

    result = CliRunner().invoke(
        run, ["testvm.local", "--storage-root", str(tmp_path)], input="y\nn\n"
    )
    assert result.exit_code != 0
    assert "Aborted: snapshots were not deleted" in result.output
    # We refuse before undefining a snapshot-bearing domain.
    assert _virsh_calls(stub, "undefine") == []
    assert vm_dir.exists()


def test_force_no_snapshots_is_fully_noninteractive(stub: dict, tmp_path: Path) -> None:
    """--force with NO snapshots skips tier-1 and never prompts."""
    # Default stub: no snapshots.
    vm_dir = tmp_path / "testvm.local"
    vm_dir.mkdir()

    result = CliRunner().invoke(
        run, ["testvm.local", "--force", "--storage-root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "Are you sure?" not in result.output
    assert not vm_dir.exists()


def test_force_with_snapshots_still_fires_tier2(stub: dict, tmp_path: Path) -> None:
    """--force alone skips tier-1 but tier-2 STILL fires when snapshots exist.

    --force consents to destroying the VM, not to the extra-destructive
    snapshot removal — that needs its own confirmation (or --snapshots-too).
    """
    stub["virsh_snapshot_names"].return_value = ["snap-a"]
    vm_dir = tmp_path / "testvm.local"
    vm_dir.mkdir()

    result = CliRunner().invoke(
        run,
        ["testvm.local", "--force", "--storage-root", str(tmp_path)],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert "Are you sure?" not in result.output  # tier-1 skipped by --force
    assert "snapshots" in result.output.lower()  # tier-2 still fired
    assert _virsh_calls(stub, "undefine")
    assert not vm_dir.exists()


def test_force_with_snapshots_tier2_declined_fails(stub: dict, tmp_path: Path) -> None:
    """--force + snapshots + declining tier-2 fails and deletes nothing."""
    stub["virsh_snapshot_names"].return_value = ["snap-a"]
    vm_dir = tmp_path / "testvm.local"
    vm_dir.mkdir()

    result = CliRunner().invoke(
        run,
        ["testvm.local", "--force", "--storage-root", str(tmp_path)],
        input="n\n",
    )
    assert result.exit_code != 0
    assert "Aborted: snapshots were not deleted" in result.output
    assert _virsh_calls(stub, "undefine") == []
    assert vm_dir.exists()


def test_force_snapshots_too_is_fully_noninteractive(
    stub: dict, tmp_path: Path
) -> None:
    """--force --snapshots-too deletes snapshots with NO prompts at all."""
    stub["virsh_snapshot_names"].return_value = ["snap-a", "snap-b"]
    vm_dir = tmp_path / "testvm.local"
    vm_dir.mkdir()

    # No input provided: a stray prompt would make CliRunner raise/abort.
    result = CliRunner().invoke(
        run,
        ["testvm.local", "--force", "--snapshots-too", "--storage-root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "Are you sure?" not in result.output
    assert "Delete all VM snapshots" not in result.output
    assert _virsh_calls(stub, "undefine")
    assert not vm_dir.exists()


def test_snapshots_too_without_force_still_prompts_tier2(
    stub: dict, tmp_path: Path
) -> None:
    """--snapshots-too without --force has no effect: both tiers still apply.

    The flag's help says it pairs with --force; on its own the interactive
    tiers govern, so a snapshot-bearing VM still asks tier-1 and tier-2.
    """
    stub["virsh_snapshot_names"].return_value = ["snap-a"]
    vm_dir = tmp_path / "testvm.local"
    vm_dir.mkdir()

    result = CliRunner().invoke(
        run,
        ["testvm.local", "--snapshots-too", "--storage-root", str(tmp_path)],
        input="y\ny\n",
    )
    assert result.exit_code == 0, result.output
    assert "Are you sure?" in result.output  # tier-1 still fired (no --force)
    assert "snapshots" in result.output.lower()  # tier-2 still fired
    assert _virsh_calls(stub, "undefine")


def test_snapshot_list_failure_aborts_before_mutation(
    stub: dict, tmp_path: Path
) -> None:
    """A failed up-front snapshot query is fatal and touches nothing."""
    stub["virsh_snapshot_names"].side_effect = VirshError(
        1, "error: failed to get domain", ["snapshot-list"]
    )
    vm_dir = tmp_path / "testvm.local"
    vm_dir.mkdir()

    result = CliRunner().invoke(
        run, ["testvm.local", "--force", "--storage-root", str(tmp_path)]
    )
    assert result.exit_code != 0
    assert "Failed to list snapshots" in result.output
    assert _virsh_calls(stub, "destroy") == []
    assert _virsh_calls(stub, "undefine") == []
    assert vm_dir.exists()


def test_undefine_failure_fails_and_leaves_dir(stub: dict, tmp_path: Path) -> None:
    """If the one-shot undefine raises, deletevm fails and leaves the dir.

    With snapshots present, the undefine retries with --snapshots-metadata;
    if even that fails, the error surfaces and storage is left for
    inspection (issue #96 folded snapshot teardown into undefine).
    """
    stub["virsh_snapshot_names"].return_value = ["snap-a"]

    def fake_run(uri, args, **kwargs):
        if args[0] == "undefine":
            raise VirshError(1, "error: undefine failed somehow", args)
        return CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    stub["run_virsh"].side_effect = fake_run
    vm_dir = tmp_path / "testvm.local"
    vm_dir.mkdir()

    result = CliRunner().invoke(
        run,
        ["testvm.local", "--force", "--snapshots-too", "--storage-root", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "Failed to undefine" in result.output
    # Undefine failed before file cleanup; the dir survives for inspection.
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
