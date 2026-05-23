"""Snapshot deletion + undefine helpers with the lvscripts-style fallback.

Phase 6 step 5. Standalone ``destroyvm`` and the manifest ``Machine.destroy``
both need to deal with the same libvirt awkwardness: ``virsh undefine``
refuses to drop a domain that owns snapshots, and ``virsh snapshot-delete``
itself can fail when the snapshot has *external* children (the common case
for backing-file qcow2 chains).

Two helpers live here:

- :func:`delete_all_snapshots` walks the snapshot list and deletes each
    one, falling back from ``--children`` (cascading delete) to
    ``--metadata`` (orphan metadata so undefine can proceed) when the
    upstream qcow2 chain prevents the cleaner cascading delete. Includes
    progress detection: if the same snapshot list comes back twice in a
    row, the function refuses to loop forever.
- :func:`undefine_with_snapshot_cleanup` tries ``virsh undefine`` first,
    detects the snapshot-related failure mode by stderr matching, and
    calls into :func:`delete_all_snapshots` before retrying. Other
    failure modes propagate.

Everything goes through :func:`tkc_lvlab.utils.virsh.run_virsh` so the
locale lock and timeout handling are uniform; failures raise
:class:`VirshError` with the exact stderr captured.
"""

from __future__ import annotations

from .virsh import VirshError, run_virsh, virsh_snapshot_names


_SNAPSHOT_UNDEFINE_MARKER = "cannot delete inactive domain"
_SNAPSHOT_KEYWORD = "snapshot"
_EXTERNAL_CHILDREN_MARKER = "external children disk snapshots not supported"


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


def _is_external_children_error(stderr: str) -> bool:
    """Return True when ``virsh snapshot-delete --children`` reports external children.

    Args:
        stderr: Captured stderr text from a failed ``snapshot-delete``.

    Returns:
        True iff the stderr names the "external children disk snapshots
        not supported" condition, in which case ``--metadata`` is the
        right fallback.
    """
    return _EXTERNAL_CHILDREN_MARKER in stderr.lower()


def delete_all_snapshots(uri: str, domain_name: str) -> None:
    """Delete every snapshot for ``domain_name``, falling back on external children.

    Walks ``virsh snapshot-list <domain> --name`` repeatedly, deleting
    the first snapshot in the list each pass. ``--children`` is tried
    first (faster, cascades); on the "external children" failure,
    ``--metadata`` is tried (drops only the libvirt metadata, leaving
    the backing-chain qcow2 files in place — undefine can then proceed).

    A progress detector breaks the loop if the same snapshot set comes
    back twice in a row. Without it, a snapshot that won't delete
    under either flag would spin forever.

    Args:
        uri: libvirt URI to operate against.
        domain_name: The libvirt domain whose snapshots to delete.

    Raises:
        VirshError: A snapshot deletion failed for a reason other than
            external children, OR progress stalled (same snapshot set
            twice). The exception message names the specific failure.
    """
    previous: tuple[str, ...] | None = None
    while True:
        names = virsh_snapshot_names(uri, domain_name)
        if not names:
            return

        current = tuple(names)
        if previous == current:
            raise VirshError(
                1,
                (
                    f"Snapshot cleanup stalled for {domain_name}: same set seen "
                    f"twice. Remaining: {', '.join(current)}"
                ),
                ["snapshot-cleanup"],
            )
        previous = current
        snapshot_name = names[0]

        try:
            run_virsh(
                uri,
                [
                    "snapshot-delete",
                    domain_name,
                    "--snapshotname",
                    snapshot_name,
                    "--children",
                ],
            )
        except VirshError as exc:
            if not _is_external_children_error(exc.stderr):
                raise
            # External-children fallback: drop just the metadata.
            run_virsh(
                uri,
                [
                    "snapshot-delete",
                    domain_name,
                    "--snapshotname",
                    snapshot_name,
                    "--metadata",
                ],
            )


def undefine_with_snapshot_cleanup(uri: str, domain_name: str) -> None:
    """Undefine ``domain_name``, deleting any blocking snapshots first.

    Tries ``virsh undefine`` directly. On the specific snapshot-related
    failure mode (detected via stderr matching), calls
    :func:`delete_all_snapshots` and retries. All other undefine failures
    propagate so the caller can decide.

    Args:
        uri: libvirt URI.
        domain_name: The libvirt domain to undefine.

    Raises:
        VirshError: The retry also failed, OR the original undefine
            failed for a non-snapshot reason. The exception carries the
            failing virsh stderr.
    """
    try:
        run_virsh(uri, ["undefine", domain_name])
        return
    except VirshError as exc:
        if not _is_snapshot_undefine_error(exc.stderr):
            raise
        # Snapshots blocking. Clean them up and retry.
        delete_all_snapshots(uri, domain_name)
        run_virsh(uri, ["undefine", domain_name])
