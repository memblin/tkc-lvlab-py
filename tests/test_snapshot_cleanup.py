"""Unit tests for :mod:`tkc_lvlab.utils.snapshot_cleanup`.

After issue #96 the teardown uses the virt-manager-style **one-shot**
``undefine --snapshots-metadata`` instead of a snapshot-delete loop.
Locked-in contracts:

- A clean ``undefine`` succeeds without the ``--snapshots-metadata`` flag
    (no snapshots to remove).
- A non-snapshot ``VirshError`` propagates unchanged (we don't paper over
    unrelated failures like an unreachable URI).
- A snapshot-blocked ``undefine`` retries **once** with
    ``--snapshots-metadata`` — wording-independent, so it can't regress the
    way issue #95's exact-string ``snapshot-delete`` matcher did.
- A failure of the ``--snapshots-metadata`` retry propagates.
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from tkc_lvlab.utils import snapshot_cleanup as sc_mod
from tkc_lvlab.utils.snapshot_cleanup import undefine_with_snapshot_cleanup
from tkc_lvlab.utils.virsh import VirshError

URI = "qemu:///system"
DOMAIN = "oneoff-testvm.local"

# The snapshot-blocked undefine stderr libvirt emits. The detector matches
# "cannot delete inactive domain" + "snapshot", not a fixed phrase.
_SNAPSHOT_BLOCK = (
    "error: Failed to undefine domain: cannot delete inactive domain with 2 snapshots"
)


def _ok() -> subprocess.CompletedProcess[str]:
    """A shape-correct success CompletedProcess for run_virsh mocks."""
    return subprocess.CompletedProcess(
        args=["virsh"], returncode=0, stdout="", stderr=""
    )


def test_undefine_clean_path_uses_no_snapshots_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A domain with no snapshots is undefined plainly — no --snapshots-metadata."""
    run = mock.Mock(return_value=_ok())
    monkeypatch.setattr(sc_mod, "run_virsh", run)

    undefine_with_snapshot_cleanup(URI, DOMAIN)  # Must not raise.

    run.assert_called_once_with(URI, ["undefine", DOMAIN])


def test_undefine_non_snapshot_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure unrelated to snapshots is re-raised as-is.

    Real-bug surface: silently retrying on unrelated errors (URI
    unreachable, permission denied) masks bugs. Lock the snapshot-specific
    match — and prove no --snapshots-metadata retry happens.
    """
    err = VirshError(1, "error: failed to connect to the hypervisor", ["undefine"])
    run = mock.Mock(side_effect=err)
    monkeypatch.setattr(sc_mod, "run_virsh", run)

    with pytest.raises(VirshError, match="failed to connect"):
        undefine_with_snapshot_cleanup(URI, DOMAIN)

    # Only the first plain undefine was attempted; no metadata retry.
    assert run.call_count == 1


def test_undefine_snapshot_blocked_retries_with_snapshots_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot-blocked undefine retries once with --snapshots-metadata (the one-shot).

    This is the issue #96 behaviour: drop the domain + all snapshot
    metadata in a single wording-independent call, replacing the old
    snapshot-delete loop.
    """
    calls: list[list[str]] = []

    def fake_run(uri: str, args: list[str], **kwargs):
        calls.append(args)
        # First (plain) undefine: snapshot-blocked. Retry: success.
        if args == ["undefine", DOMAIN]:
            raise VirshError(1, _SNAPSHOT_BLOCK, ["undefine"])
        return _ok()

    monkeypatch.setattr(sc_mod, "run_virsh", fake_run)

    undefine_with_snapshot_cleanup(URI, DOMAIN)

    assert calls == [
        ["undefine", DOMAIN],
        ["undefine", DOMAIN, "--snapshots-metadata"],
    ]


def test_undefine_snapshots_metadata_retry_failure_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the --snapshots-metadata retry also fails, that error propagates."""

    def fake_run(uri: str, args: list[str], **kwargs):
        if args == ["undefine", DOMAIN]:
            raise VirshError(1, _SNAPSHOT_BLOCK, ["undefine"])
        raise VirshError(1, "error: something else went wrong", ["undefine"])

    monkeypatch.setattr(sc_mod, "run_virsh", fake_run)

    with pytest.raises(VirshError, match="something else went wrong"):
        undefine_with_snapshot_cleanup(URI, DOMAIN)
