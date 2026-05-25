"""Standalone ``deletevm`` console script — VM removal by raw domain name.

A port of the ``lvscripts-py`` reference ``deletevm`` command's UI and
operations (colored output, ``--force`` confirmation, ``--version``, the
snapshot-deletion prompt) onto lvlab's storage layout, with one deliberate
behavioral difference: ``deletevm`` acts purely on the **raw libvirt
domain name**.

It looks up exactly the name you pass, force-offs it, undefines it
(prompting before deleting any blocking snapshots), and then removes the
per-VM storage directory under the one-off root **if one exists**. The
storage directory is not required — undefine is the operative effect.

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
from ..utils.snapshot_cleanup import delete_all_snapshots
from ..utils.virsh import VirshError, run_virsh, vm_exists
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


def _is_snapshot_undefine_error(stderr: str) -> bool:
    """Return True when ``virsh undefine`` failed because the domain has snapshots."""
    combined = (stderr or "").lower()
    return "cannot delete inactive domain" in combined and "snapshot" in combined


def _undefine_with_snapshot_prompt(vm_name: str) -> None:
    """Undefine ``vm_name``, prompting before deleting any blocking snapshots.

    Tries ``virsh undefine`` directly. On the snapshot-related failure, asks
    the operator for confirmation, deletes all snapshots via
    :func:`tkc_lvlab.utils.snapshot_cleanup.delete_all_snapshots`, then
    retries. Any other failure surfaces via :func:`_fail`.

    Raises:
        typer.Exit: Undefine (or snapshot deletion) failed, or the operator
            declined to delete snapshots.
    """
    try:
        run_virsh(_SYSTEM_URI, ["undefine", vm_name])
        return
    except VirshError as exc:
        if not _is_snapshot_undefine_error(exc.stderr):
            _fail(f"Failed to undefine VM '{vm_name}': {exc.stderr or exc}")

    typer.secho(
        f"VM '{vm_name}' has snapshots and cannot be undefined until they are removed.",
        fg=typer.colors.YELLOW,
    )
    if not typer.confirm("Delete all VM snapshots and continue?"):
        _fail("Aborted: snapshots were not deleted, so VM removal cannot continue.")

    typer.secho(f"Deleting snapshots for VM '{vm_name}'...", fg=typer.colors.RED)
    try:
        delete_all_snapshots(_SYSTEM_URI, vm_name)
    except VirshError as exc:
        _fail(f"Failed to delete snapshots for VM '{vm_name}': {exc.stderr or exc}")

    try:
        run_virsh(_SYSTEM_URI, ["undefine", vm_name])
    except VirshError as exc:
        _fail(
            f"Failed to undefine VM '{vm_name}' after deleting snapshots: "
            f"{exc.stderr or exc}"
        )


@app.command()
def deletevm(
    vm_name: str = typer.Argument(..., help="VM name to destroy and remove."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompt."),
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
    """Destroy, undefine, and remove a libvirt VM by its raw domain name."""
    domain_name = vm_name
    vm_dir = storage_dir_for(vm_name, root=storage_root)

    if not vm_exists(_SYSTEM_URI, domain_name):
        _fail(f"VM '{domain_name}' is not defined at {_SYSTEM_URI}.")

    if not force:
        typer.secho(
            f"This will destroy, undefine, and remove all data for VM '{domain_name}'.",
            fg=typer.colors.RED,
        )
        if not typer.confirm("Are you sure?"):
            typer.secho("Aborted.", fg=typer.colors.YELLOW)
            raise typer.Exit(code=0)

    typer.secho(f"Destroying VM '{domain_name}'...", fg=typer.colors.RED)
    # The domain may already be shut off; ignore a nonzero destroy.
    run_virsh(_SYSTEM_URI, ["destroy", domain_name], check=False)

    typer.secho(f"Undefining VM '{domain_name}'...", fg=typer.colors.RED)
    _undefine_with_snapshot_prompt(domain_name)

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
