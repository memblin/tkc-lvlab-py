"""Standalone ``deletevm`` console script — VM removal by raw domain name.

A port of the ``lvscripts-py`` reference ``deletevm`` command's UI and
operations (colored output, ``--force`` confirmation, ``--version``, the
snapshot-deletion prompt) onto lvlab's storage layout, with one deliberate
behavioral difference: ``deletevm`` acts purely on the **raw libvirt
domain name**.

It looks up exactly the name you pass, force-offs it, undefines it
(deleting any blocking snapshots first, under the confirmation tiers
below), and then removes the per-VM storage directory under the one-off
root **if one exists**. The storage directory is not required — undefine
is the operative effect.

Snapshot presence is detected **up front** so the confirmation tiers can
branch on it:

- No ``--force``: tier-1 prompt ("irreversible") → if snapshots exist, a
  tier-2 prompt ("snapshots present; remove them?") → cleanup.
- ``--force`` alone: tier-1 is skipped only when there are no snapshots; if
  snapshots exist, tier-2 still fires.
- ``--force --snapshots-too``: fully non-interactive (NEW flag, a deliberate
  divergence from the lvscripts-py parity port — ref #84).

``deletevm`` does NOT read ``Lvlab.yml`` and does no name translation: a
short manifest name like ``web01`` won't resolve (the real domain is
``web01_<env>``). But because the lookup is the raw libvirt name, passing a
manifest VM's actual ``<vm_name>_<env>`` domain name WILL remove it — its
disks live under ``<basedir>/<env>/<vm>/`` rather than the one-off root, so
they are left behind and the undefine is what takes effect. The
confirmation prompt (skip with ``--force``) echoes the exact domain about
to be removed; that is the guard.

``run = app`` is kept as a backwards-compat alias for the console-script
entry point (``[project.scripts] deletevm = "tkc_lvlab.scripts.deletevm:run"``)
and for test imports.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import typer

from .. import __version__
from ..utils.snapshot_cleanup import undefine_with_snapshot_cleanup
from ..utils.virsh import VirshError, run_virsh, virsh_snapshot_names, vm_exists
from .createvm import storage_dir_for


_SYSTEM_URI = "qemu:///system"
_ONEOFF_STORAGE_ROOT = Path("/var/lib/libvirt/images/lvlab/oneoff")


app = typer.Typer(help="Destroy, undefine, and remove a libvirt VM.")


def _fail(message: str) -> None:
    """Print ``message`` in red and exit nonzero.

    Raises:
        typer.Exit: Always, with code 1.
    """
    typer.secho(message, fg=typer.colors.RED)
    raise typer.Exit(code=1)


def _version_callback(value: bool) -> None:
    """Print the installed package version and exit when ``--version`` is set."""
    if value:
        typer.echo(f"deletevm {__version__}")
        raise typer.Exit()


def _domain_has_snapshots(vm_name: str) -> bool:
    """Return True when ``vm_name`` currently owns one or more snapshots.

    Detection is done UP FRONT (``virsh snapshot-list <domain> --name``) so
    the confirmation-tier logic can branch on snapshot presence before any
    destructive step — rather than discovering snapshots lazily from a
    failed ``undefine``. A failed snapshot query is fatal: we refuse to
    proceed when we cannot tell whether snapshots exist.

    Raises:
        typer.Exit: The ``virsh snapshot-list`` query failed.
    """
    try:
        return bool(virsh_snapshot_names(_SYSTEM_URI, vm_name))
    except VirshError as exc:
        _fail(f"Failed to list snapshots for VM '{vm_name}': {exc.stderr or exc}")
        return False  # unreachable; _fail raises. Keeps the type checker happy.


def _undefine_or_fail(vm_name: str) -> None:
    """Undefine ``vm_name``, dropping any snapshots in one shot.

    Delegates to
    :func:`tkc_lvlab.utils.snapshot_cleanup.undefine_with_snapshot_cleanup`,
    which retries with ``undefine --snapshots-metadata`` when the domain
    still owns snapshots (issue #96) — no separate snapshot-delete pass.
    The tier-2 consent in :func:`deletevm` still gates whether we reach
    this point for a snapshot-bearing VM.

    Raises:
        typer.Exit: ``virsh undefine`` failed.
    """
    try:
        undefine_with_snapshot_cleanup(_SYSTEM_URI, vm_name)
    except VirshError as exc:
        _fail(f"Failed to undefine VM '{vm_name}': {exc.stderr or exc}")


@app.command()
def deletevm(
    vm_name: str = typer.Argument(..., help="VM name to destroy and remove."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompt."),
    # --snapshots-too is a deliberate divergence from the lvscripts-py
    # deletevm parity port (ref #84): it pairs with --force to make a
    # snapshot-bearing teardown fully non-interactive (skips the tier-2
    # "snapshots present" prompt). lvscripts-py has no such flag.
    snapshots_too: bool = typer.Option(
        False,
        "--snapshots-too",
        help=(
            "With --force, also delete the VM's snapshots without prompting. "
            "Has no effect without --force."
        ),
    ),
    storage_root: Path = typer.Option(
        _ONEOFF_STORAGE_ROOT,
        "--storage-root",
        hidden=True,
        file_okay=False,
        help="Override the per-VM storage root (test seam).",
    ),
    version: bool = typer.Option(  # pylint: disable=unused-argument
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the installed tkc-lvlab package version and exit.",
    ),
) -> None:
    """Destroy, undefine, and remove a libvirt VM by its raw domain name.

    Confirmation tiers (snapshot presence is detected up front so the prompts
    can branch on it before any state is touched):

    - No ``--force``: tier-1 prompt ("irreversible, all data lost"), then —
      if snapshots exist — a tier-2 prompt ("snapshots present; remove them?").
    - ``--force`` alone: tier-1 is skipped ONLY when there are no snapshots; if
      snapshots exist, tier-2 still fires (deleting snapshots is extra-destructive
      and ``--force`` by itself does not consent to it).
    - ``--force --snapshots-too``: fully non-interactive (both tiers skipped).
    """
    domain_name = vm_name
    vm_dir = storage_dir_for(vm_name, root=storage_root)

    if not vm_exists(_SYSTEM_URI, domain_name):
        _fail(f"VM '{domain_name}' is not defined at {_SYSTEM_URI}.")

    has_snapshots = _domain_has_snapshots(domain_name)

    # Tier 1: the always-irreversible warning. --force skips it.
    if not force:
        typer.secho(
            f"This will destroy, undefine, and remove all data for VM '{domain_name}'.",
            fg=typer.colors.RED,
        )
        if not typer.confirm("Are you sure?"):
            typer.secho("Aborted.", fg=typer.colors.YELLOW)
            raise typer.Exit(code=0)

    # Tier 2: snapshot consent. Fires whenever snapshots exist UNLESS the
    # operator opted in non-interactively with --force --snapshots-too.
    if has_snapshots and not (force and snapshots_too):
        typer.secho(
            f"VM '{domain_name}' has snapshots and cannot be undefined until they "
            "are removed.",
            fg=typer.colors.YELLOW,
        )
        if not typer.confirm("Delete all VM snapshots and continue? (irreversible)"):
            _fail("Aborted: snapshots were not deleted, so VM removal cannot continue.")

    typer.secho(f"Destroying VM '{domain_name}'...", fg=typer.colors.RED)
    # The domain may already be shut off; ignore a nonzero destroy.
    run_virsh(_SYSTEM_URI, ["destroy", domain_name], check=False)

    # Consent for snapshot removal was obtained above (tier-2, or
    # --force --snapshots-too). The undefine drops the domain AND all its
    # snapshot metadata in one shot (issue #96), so there's no separate
    # snapshot-delete pass.
    if has_snapshots:
        typer.secho(
            f"Removing snapshots and undefining VM '{domain_name}'...",
            fg=typer.colors.RED,
        )
    else:
        typer.secho(f"Undefining VM '{domain_name}'...", fg=typer.colors.RED)
    _undefine_or_fail(domain_name)

    # Storage cleanup is best-effort: a one-off VM's dir lives here, but a
    # manifest VM removed by its raw domain name keeps its disks elsewhere,
    # so a missing dir is expected, not an error.
    if vm_dir.exists():
        typer.secho(f"Removing storage directory '{vm_dir}'...", fg=typer.colors.RED)
        shutil.rmtree(vm_dir)

    typer.secho(f"VM '{domain_name}' successfully removed.", fg=typer.colors.GREEN)


# Backwards-compat alias for the entry point and external imports.
run = app


if __name__ == "__main__":  # pragma: no cover
    app()
