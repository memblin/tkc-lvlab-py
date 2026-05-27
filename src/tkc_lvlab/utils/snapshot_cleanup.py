"""Undefine helpers with the virt-manager-style one-shot snapshot teardown.

Standalone ``deletevm`` and the manifest ``Machine.destroy`` both need to
deal with the same libvirt awkwardness: ``virsh undefine`` refuses to drop
a domain that still owns snapshots.

:func:`undefine_with_snapshot_cleanup` resolves this the way virt-manager
does — in **one** call:

1. ``virsh undefine <dom>`` is tried first.
2. If that fails *because the domain owns snapshots* (detected by stderr
    matching), it retries with ``virsh undefine <dom> --snapshots-metadata``,
    which drops the domain **and** all snapshot metadata together.

This supersedes the earlier two-step approach (loop ``snapshot-delete``,
falling back from ``--children`` to ``--metadata`` on external-snapshot
chains). The one-shot has **zero dependence on ``snapshot-delete`` error
wordings**, so it structurally cannot regress the way issue #95 did — a
single libvirt version emitting two different "external snapshot
unsupported" phrasings broke the old exact-string fallback. See issue #96
for the evaluation that chose this. The overlay qcow2 files are left on
disk (same as before); the callers ``rmtree`` the VM storage dir next, so
nothing is orphaned.

Everything goes through :func:`tkc_lvlab.utils.virsh.run_virsh` so the
locale lock and timeout handling are uniform; failures raise
:class:`VirshError` with the exact stderr captured.
"""

from __future__ import annotations

from .virsh import VirshError, run_virsh

_SNAPSHOT_UNDEFINE_MARKER = "cannot delete inactive domain"
_SNAPSHOT_KEYWORD = "snapshot"


def _is_snapshot_undefine_error(stderr: str) -> bool:
    """Return True when the stderr from ``virsh undefine`` says snapshots are in the way.

    Args:
        stderr: Captured stderr text from a failed ``virsh undefine``.

    Returns:
        True iff the stderr contains both the "cannot delete inactive
        domain" preamble and the word "snapshot". Exact-string matching
        would be brittle across libvirt versions; substring matching
        across both halves of the message is robust enough.
    """
    s = stderr.lower()
    return _SNAPSHOT_UNDEFINE_MARKER in s and _SNAPSHOT_KEYWORD in s


def undefine_with_snapshot_cleanup(uri: str, domain_name: str) -> None:
    """Undefine ``domain_name``, dropping any blocking snapshots in one shot.

    Tries ``virsh undefine`` directly. On the specific snapshot-related
    failure mode (detected via stderr matching), retries with
    ``virsh undefine --snapshots-metadata`` — which removes the domain and
    all of its snapshot metadata together, regardless of how the host's
    libvirt phrases its external-snapshot limitations (issue #96). All
    other undefine failures propagate so the caller can decide.

    The qcow2 overlay files for any external snapshots remain on disk;
    callers remove the VM's storage directory afterward.

    Args:
        uri: libvirt connection URI.
        domain_name: The libvirt domain to undefine.

    Raises:
        VirshError: The ``--snapshots-metadata`` retry also failed, OR the
            original undefine failed for a non-snapshot reason. The
            exception carries the failing ``virsh`` stderr.
    """
    try:
        run_virsh(uri, ["undefine", domain_name])
        return
    except VirshError as exc:
        if not _is_snapshot_undefine_error(exc.stderr):
            raise
        # Snapshots are blocking the undefine. Drop the domain and every
        # snapshot's metadata in a single call (virt-manager's approach).
        run_virsh(uri, ["undefine", domain_name, "--snapshots-metadata"])
