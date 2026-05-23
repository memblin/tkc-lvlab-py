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

Wired up as a console script via
``[project.scripts] destroyvm = "tkc_lvlab.scripts.destroyvm:run"``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import click

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


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("vm_name")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt.",
)
@click.option(
    "--uri",
    default=_DEFAULT_URI,
    show_default=True,
    help="libvirt connection URI.",
)
@click.option(
    "--storage-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=_ONEOFF_STORAGE_ROOT,
    show_default=True,
    help="Override the per-VM storage root (test seam).",
)
def run(vm_name: str, force: bool, uri: str, storage_root: Path) -> None:
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
        raise click.ClickException(
            f"Could not list libvirt domains at {uri}: {exc.stderr or exc}"
        ) from exc

    if domain_name not in present_domains:
        # Important: the bare vm_name is NEVER looked up. A manifest VM
        # that happened to share the short name stays invisible.
        raise click.ClickException(
            f"One-off VM '{domain_name}' is not defined at {uri}. "
            "(If you expected to manage a manifest VM, use 'lvlab destroy' instead.)"
        )

    if not force:
        click.echo(
            f"This will destroy, undefine, and remove all data for VM "
            f"'{domain_name}' at {uri}.",
            err=True,
        )
        if not click.confirm("Are you sure?", err=True):
            click.echo("Aborted.", err=True)
            return

    # Step 1: force-off if not already shut off.
    try:
        state = virsh_domstate(uri, domain_name)
    except VirshError as exc:
        raise click.ClickException(
            f"Could not query state of '{domain_name}': {exc.stderr or exc}"
        ) from exc

    if state not in DEAD_STATES:
        try:
            run_virsh(uri, ["destroy", domain_name])
        except VirshError as exc:
            raise click.ClickException(
                f"Could not force-off '{domain_name}': {exc.stderr or exc}"
            ) from exc

    # Step 2: undefine (with snapshot cleanup fallback).
    try:
        undefine_with_snapshot_cleanup(uri, domain_name)
    except VirshError as exc:
        raise click.ClickException(
            f"Could not undefine '{domain_name}': {exc.stderr or exc}"
        ) from exc

    # Step 3: storage cleanup. Only after undefine succeeds; if undefine
    # failed the files are useful evidence and should NOT be wiped.
    if vm_dir.exists():
        shutil.rmtree(vm_dir, ignore_errors=False)

    click.echo(f"VM '{domain_name}' destroyed and removed.", err=False)


if __name__ == "__main__":  # pragma: no cover
    run()
