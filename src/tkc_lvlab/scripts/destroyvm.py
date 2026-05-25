"""Standalone ``destroyvm`` console script — VM removal by raw domain name.

The companion to ``createvm``. Operates on **raw libvirt domain names**,
matching the ``lvscripts-py`` reference: it looks up exactly the name you
pass, forcibly powers it off, undefines (with snapshot fallback), and
removes the per-VM storage directory.

``destroyvm`` does NOT read ``Lvlab.yml`` — short manifest names like
``web01`` are not translated into their ``<vm_name>_<env>`` domain. But
because the lookup is the raw name, passing the actual domain name of a
manifest VM (e.g. ``destroyvm web01_lab``) WILL destroy it if it exists.
There is no ``oneoff-`` guard. The confirmation prompt (skip with
``--force``) echoes the exact domain about to be removed — that is the
guard against deleting the wrong VM.

Note: a manifest VM's disks live at
``/var/lib/libvirt/images/lvlab/<env>/<vm>/diskN.qcow2``, which the
one-off storage convention (``<storage-root>/<name>/``) won't match — so
destroying a manifest domain by raw name undefines it but leaves its disk
files behind.

``run = app`` is kept as a backwards-compat alias so the pyproject
``[project.scripts]`` entry point (``tkc_lvlab.scripts.destroyvm:run``)
and test imports continue to work unchanged.

Wired up as a console script via
``[project.scripts] destroyvm = "tkc_lvlab.scripts.destroyvm:run"``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import typer

from ..utils.snapshot_cleanup import undefine_with_snapshot_cleanup
from ..utils.virsh import (
    DEAD_STATES,
    VirshError,
    run_virsh,
    virsh_domstate,
    virsh_list_all_names,
)
from .createvm import storage_dir_for


_DEFAULT_URI = "qemu:///system"
_ONEOFF_STORAGE_ROOT = Path("/var/lib/libvirt/images/lvlab/oneoff")


app = typer.Typer(
    help="Destroy and undefine a libvirt VM by its raw domain name.",
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _fail(message: str) -> typer.Exit:
    """Print ``Error: <message>`` to stderr and return an ``Exit(1)``.

    Returning the ``Exit`` instance (rather than raising here) lets
    callers write ``raise _fail("...") from exc`` so the original
    ``VirshError`` cause is preserved in tracebacks.
    """
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(code=1)


@app.command()
def destroyvm(
    vm_name: str = typer.Argument(
        ...,
        help="Raw libvirt domain name to destroy (exactly as 'virsh list' shows it).",
    ),
    force: bool = typer.Option(False, "--force", help="Skip the confirmation prompt."),
    uri: str = typer.Option(
        _DEFAULT_URI, "--uri", show_default=True, help="libvirt connection URI."
    ),
    storage_root: Path = typer.Option(
        _ONEOFF_STORAGE_ROOT,
        "--storage-root",
        show_default=True,
        file_okay=False,
        help="Override the per-VM storage root (test seam).",
    ),
) -> None:
    """Destroy and undefine the libvirt domain named ``VM_NAME``.

    Looks up exactly ``VM_NAME`` on the domain list — no prefixing, no
    ``Lvlab.yml`` translation. If it isn't defined, this exits with a
    clear error.
    """
    domain_name = vm_name
    vm_dir = storage_dir_for(vm_name, root=storage_root)

    try:
        present_domains = virsh_list_all_names(uri)
    except VirshError as exc:
        raise _fail(
            f"Could not list libvirt domains at {uri}: {exc.stderr or exc}"
        ) from exc

    if domain_name not in present_domains:
        raise _fail(f"No libvirt domain named '{domain_name}' is defined at {uri}.")

    if not force:
        typer.echo(
            f"This will destroy, undefine, and remove all data for VM "
            f"'{domain_name}' at {uri}.",
            err=True,
        )
        if not typer.confirm("Are you sure?", err=True):
            typer.echo("Aborted.", err=True)
            return

    # Step 1: force-off if not already shut off.
    try:
        state = virsh_domstate(uri, domain_name)
    except VirshError as exc:
        raise _fail(
            f"Could not query state of '{domain_name}': {exc.stderr or exc}"
        ) from exc

    if state not in DEAD_STATES:
        try:
            run_virsh(uri, ["destroy", domain_name])
        except VirshError as exc:
            raise _fail(
                f"Could not force-off '{domain_name}': {exc.stderr or exc}"
            ) from exc

    # Step 2: undefine (with snapshot cleanup fallback).
    try:
        undefine_with_snapshot_cleanup(uri, domain_name)
    except VirshError as exc:
        raise _fail(f"Could not undefine '{domain_name}': {exc.stderr or exc}") from exc

    # Step 3: storage cleanup. Only after undefine succeeds; if undefine
    # failed the files are useful evidence and should NOT be wiped.
    if vm_dir.exists():
        shutil.rmtree(vm_dir, ignore_errors=False)

    typer.echo(f"VM '{domain_name}' destroyed and removed.", err=False)


# Backwards-compat alias for the entry point and external imports.
# pyproject.toml references "tkc_lvlab.scripts.destroyvm:run"; tests
# import ``run`` from this module. Typer ``app`` is callable, so the
# script entry point works identically.
run = app


if __name__ == "__main__":  # pragma: no cover
    app()
