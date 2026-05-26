"""Unit tests for :mod:`tkc_lvlab.utils.snapshot_cleanup`.

Locked-in contracts:

- ``undefine_with_snapshot_cleanup`` succeeds on a clean undefine
    without touching snapshots at all.
- A non-snapshot VirshError from undefine propagates unchanged (we
    don't paper over unrelated failures).
- A snapshot-blocked undefine triggers ``delete_all_snapshots`` then
    retries.
- ``delete_all_snapshots`` walks the snapshot list, prefers
    ``--children``, and falls back to ``--metadata`` on the specific
    "external children" error.
- A stalled progress loop (same snapshot set seen twice) raises
    rather than spinning forever.
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from tkc_lvlab.utils import snapshot_cleanup as sc_mod
from tkc_lvlab.utils.snapshot_cleanup import (
    delete_all_snapshots,
    undefine_with_snapshot_cleanup,
)
from tkc_lvlab.utils.virsh import VirshError


URI = "qemu:///system"
DOMAIN = "oneoff-testvm.local"


def _ok() -> subprocess.CompletedProcess[str]:
    """A shape-correct success CompletedProcess for run_virsh mocks."""
    return subprocess.CompletedProcess(
        args=["virsh"], returncode=0, stdout="", stderr=""
    )


# ---------------------------------------------------------------------------
# undefine_with_snapshot_cleanup
# ---------------------------------------------------------------------------


def test_undefine_clean_path_does_not_touch_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When undefine succeeds, snapshot helpers aren't consulted."""
    snap_names_mock = mock.Mock()
    monkeypatch.setattr(sc_mod, "virsh_snapshot_names", snap_names_mock)
    monkeypatch.setattr(sc_mod, "run_virsh", mock.Mock(return_value=_ok()))

    undefine_with_snapshot_cleanup(URI, DOMAIN)  # Must not raise.

    snap_names_mock.assert_not_called()


def test_undefine_non_snapshot_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure unrelated to snapshots is re-raised as-is.

    Real-bug surface: silently retrying on unrelated errors masks bugs
    (e.g. URI unreachable, permission denied). Lock the
    snapshot-specific match.
    """
    err = VirshError(1, "error: failed to connect to the hypervisor", ["undefine"])
    monkeypatch.setattr(sc_mod, "run_virsh", mock.Mock(side_effect=err))

    with pytest.raises(VirshError, match="failed to connect"):
        undefine_with_snapshot_cleanup(URI, DOMAIN)


def test_undefine_snapshot_blocked_triggers_cleanup_then_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The snapshot-blocked failure path runs delete_all_snapshots, then retries undefine."""
    # First undefine: snapshot-blocked. Second undefine: success.
    snapshot_block = VirshError(
        1,
        "error: Failed to undefine domain: cannot delete inactive domain with 2 snapshots",
        ["undefine"],
    )

    calls: list[list[str]] = []

    def fake_run(uri: str, args: list[str], **kwargs):
        calls.append(args)
        # First undefine fails; subsequent virsh calls (snapshot-delete + retry undefine) succeed.
        if args[0] == "undefine" and not any(
            c[0] == "snapshot-delete" for c in calls[:-1]
        ):
            raise snapshot_block
        return _ok()

    snap_names = mock.Mock(
        side_effect=[["snap1"], []]
    )  # one snap, then empty after delete

    monkeypatch.setattr(sc_mod, "run_virsh", fake_run)
    monkeypatch.setattr(sc_mod, "virsh_snapshot_names", snap_names)

    undefine_with_snapshot_cleanup(URI, DOMAIN)

    # Sequence: undefine (fails) -> snapshot-delete --children -> undefine (succeeds)
    cmds = [c[0] for c in calls]
    assert cmds == ["undefine", "snapshot-delete", "undefine"], cmds


# ---------------------------------------------------------------------------
# delete_all_snapshots
# ---------------------------------------------------------------------------


def test_delete_all_snapshots_no_snapshots_is_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty snapshot list means immediate return; run_virsh is never called."""
    snap_names = mock.Mock(return_value=[])
    run = mock.Mock()
    monkeypatch.setattr(sc_mod, "virsh_snapshot_names", snap_names)
    monkeypatch.setattr(sc_mod, "run_virsh", run)

    delete_all_snapshots(URI, DOMAIN)

    run.assert_not_called()


def test_delete_all_snapshots_uses_children_flag_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each snapshot is deleted with the --children cascading flag first."""
    # Three snapshots; each deletion shrinks the list by one.
    snap_lists = [["a", "b", "c"], ["b", "c"], ["c"], []]
    snap_names = mock.Mock(side_effect=snap_lists)
    monkeypatch.setattr(sc_mod, "virsh_snapshot_names", snap_names)

    deleted: list[str] = []

    def fake_run(uri: str, args: list[str], **kwargs):
        # args is like ["snapshot-delete", domain, "--snapshotname", name, "--children"]
        assert args[0] == "snapshot-delete"
        assert args[-1] == "--children"
        deleted.append(args[3])
        return _ok()

    monkeypatch.setattr(sc_mod, "run_virsh", fake_run)

    delete_all_snapshots(URI, DOMAIN)

    assert deleted == ["a", "b", "c"]


# Real ``virsh snapshot-delete --children`` stderr seen in the wild. libvirt
# 12.0.0 emits BOTH of these on one host depending on the snapshot's position
# in the chain, and the exact phrasing has shifted across versions — so the
# detector must match the family, not one fixed phrase (issue #95).
_EXTERNAL_SNAPSHOT_WORDINGS = [
    # Parent-with-children (matched the pre-#95 exact-string marker).
    "error: unsupported configuration: external children disk snapshots not supported",
    # Leaf / newer wording (the one that aborted teardown on the user's host).
    "error: unsupported configuration: "
    "deletion of external disk snapshots with children not supported",
]


@pytest.mark.parametrize("stderr_msg", _EXTERNAL_SNAPSHOT_WORDINGS)
def test_delete_all_snapshots_falls_back_to_metadata_on_external_children(
    monkeypatch: pytest.MonkeyPatch,
    stderr_msg: str,
) -> None:
    """A --children failure naming an unsupported external snapshot → retry with --metadata.

    Regression for issue #95: the fallback must fire for every libvirt
    wording of the external-snapshot limitation, not just the one phrase
    the original exact-substring marker happened to carry.
    """
    snap_lists = [["only-snap"], []]
    snap_names = mock.Mock(side_effect=snap_lists)
    monkeypatch.setattr(sc_mod, "virsh_snapshot_names", snap_names)

    external_children_err = VirshError(1, stderr_msg, ["snapshot-delete"])

    calls: list[list[str]] = []

    def fake_run(uri: str, args: list[str], **kwargs):
        calls.append(args)
        # First call (--children): fail with external-snapshot message.
        # Second call (--metadata): succeed.
        if args[-1] == "--children":
            raise external_children_err
        return _ok()

    monkeypatch.setattr(sc_mod, "run_virsh", fake_run)

    delete_all_snapshots(URI, DOMAIN)

    # Two calls: one with --children (failing), one with --metadata (succeeding).
    assert len(calls) == 2
    assert calls[0][-1] == "--children"
    assert calls[1][-1] == "--metadata"


def test_delete_all_snapshots_other_children_failure_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A --children failure that isn't 'external children' propagates."""
    snap_names = mock.Mock(side_effect=[["snap1"], []])
    monkeypatch.setattr(sc_mod, "virsh_snapshot_names", snap_names)

    other_err = VirshError(
        1,
        "error: invalid argument: somehow this is a different problem",
        ["snapshot-delete"],
    )
    monkeypatch.setattr(sc_mod, "run_virsh", mock.Mock(side_effect=other_err))

    with pytest.raises(VirshError, match="different problem"):
        delete_all_snapshots(URI, DOMAIN)


def test_delete_all_snapshots_progress_stall_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the snapshot list is the same on consecutive passes, we refuse to loop forever."""
    # virsh_snapshot_names returns the same list twice in a row.
    snap_names = mock.Mock(side_effect=[["stuck"], ["stuck"]])
    monkeypatch.setattr(sc_mod, "virsh_snapshot_names", snap_names)
    monkeypatch.setattr(sc_mod, "run_virsh", mock.Mock(return_value=_ok()))

    with pytest.raises(VirshError, match="stalled"):
        delete_all_snapshots(URI, DOMAIN)
