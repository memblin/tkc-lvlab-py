"""Standalone ``destroyvm`` console script — one-off VM removal.

Phase 6 step 5. The companion to ``createvm``. Translates the
user-supplied short name into the ``oneoff-<vm_name>`` libvirt domain,
forcibly powers it off, undefines (with snapshot fallback), and removes
the storage directory.

Per the Phase 6 architecture lock, ``destroyvm`` MUST NOT read
``Lvlab.yml`` and MUST NOT operate on names that don't start with the
``oneoff-`` prefix on the libvirt side. The user types
``destroyvm testvm.local`` and we look up domain ``oneoff-testvm.local``
— if it's not present, that's the end of it (we do NOT silently fall
through to looking up the bare ``testvm.local``, which could be a
manifest VM).

Phase 9 follow-up ported this file from Click to Typer. The UX
contract is preserved: same option names, same defaults, same exit
codes, same error-message phrasing. ``run = app`` is kept as a
backwards-compat alias so the pyproject ``[project.scripts]`` entry
point (``tkc_lvlab.scripts.destroyvm:run``) and test imports continue
to work unchanged.

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
from .createvm import domain_name_for, storage_dir_for


_DEFAULT_URI = "qemu:///system"
_ONEOFF_STORAGE_ROOT = Path("/var/lib/libvirt/images/oneoff")


app = typer.Typer(
    help="Destroy and undefine a one-off libvirt VM created by createvm.",
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
    vm_name: str = typer.Argument(..., help="Short name of the one-off VM."),
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
    """Destroy and undefine the one-off VM named ``VM_NAME``.

    Looks up the libvirt domain ``oneoff-<VM_NAME>``. If the prefix
    isn't on the domain list, this exits with a clear error rather than
    falling through to any other lookup — keeping the manifest workflow
    fully invisible.
    """
    domain_name = domain_name_for(vm_name)
    vm_dir = storage_dir_for(vm_name, root=storage_root)

    try:
        present_domains = virsh_list_all_names(uri)
    except VirshError as exc:
        raise _fail(
            f"Could not list libvirt domains at {uri}: {exc.stderr or exc}"
        ) from exc

    if domain_name not in present_domains:
        # Important: the bare vm_name is NEVER looked up. A manifest VM
        # that happened to share the short name stays invisible.
        raise _fail(
            f"One-off VM '{domain_name}' is not defined at {uri}. "
            "(If you expected to manage a manifest VM, use 'lvlab destroy' instead.)"
        )

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
