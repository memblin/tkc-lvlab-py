"""Per-run resource-naming prefix and prefix-scoped reaping for the harness.

The harness boots **real** VMs on a host that may also hold developer VMs the
maintainer cares about. The single guarantee that makes that safe is the same
one the integration suite relies on (``tests/conftest.py``): every libvirt
domain, qcow2 path, and network the harness creates starts with a
session-unique prefix, and **teardown only ever touches prefixed resources** —
it never lists or iterates over all domains/files/networks.

This module is the harness's own copy of that model (own prefix, own reaper)
rather than an import of test-only code, so the harness stays independent of
``conftest`` and of pytest's import machinery.
"""

from __future__ import annotations

import secrets
import shutil
import time
from pathlib import Path

# Import the same virsh wrappers the application uses, so the reaper speaks the
# exact dialect (retry-on-transient, FileNotFoundError -> VirshError) as the code
# under test.
from tkc_lvlab.utils.virsh import VirshError, run_virsh, virsh_list_all_names

LVLAB_VALIDATE_PREFIX: str = (
    f"lvlab-validate-{int(time.time() * 1000)}-{secrets.token_hex(2)}-"
)
"""Session-unique prefix every harness-owned resource must start with.

Of the form ``lvlab-validate-<epoch_ms>-<rand4>-``. Epoch milliseconds plus a
short random suffix avoid collisions across concurrent runs on one host.
"""

# Dedicated storage root for harness-owned disks/ISOs, mirroring the integration
# suite's ``lvlab-test`` root. Kept distinct so a stray reap can be scoped to it.
VALIDATE_STORAGE_ROOT: Path = Path("/var/lib/libvirt/images/lvlab-validate")


def make_name(base: str) -> str:
    """Return a harness-owned resource name carrying :data:`LVLAB_VALIDATE_PREFIX`.

    The only sanctioned way to name a domain, network, or storage path the
    harness will later reap.

    Args:
        base: A short, human-meaningful suffix (e.g. ``"deb13-dhcp"``).

    Returns:
        ``f"{LVLAB_VALIDATE_PREFIX}{base}"``.
    """
    return f"{LVLAB_VALIDATE_PREFIX}{base}"


def is_owned(name: str) -> bool:
    """Return True iff ``name`` is a harness-owned resource.

    Args:
        name: A libvirt domain, network, or storage-directory name.

    Returns:
        True when ``name`` starts with :data:`LVLAB_VALIDATE_PREFIX`.
    """
    return name.startswith(LVLAB_VALIDATE_PREFIX)


def assert_owned(name: str) -> None:
    """Guard called before every destructive operation in the harness.

    Args:
        name: The resource name about to be destroyed/undefined/removed.

    Raises:
        AssertionError: ``name`` does not carry :data:`LVLAB_VALIDATE_PREFIX`.
            This guard is what prevents a runaway teardown from touching a
            developer VM.
    """
    if not is_owned(name):
        raise AssertionError(
            f"Refusing destructive op on {name!r}: it does not carry the harness "
            f"prefix {LVLAB_VALIDATE_PREFIX!r}. This guard prevents teardown from "
            f"touching resources the harness did not create."
        )


def list_prefixed_domains(uri: str) -> list[str]:
    """Return the harness-owned domains currently defined on ``uri``.

    Walks :func:`tkc_lvlab.utils.virsh.virsh_list_all_names` and keeps only
    names matching :data:`LVLAB_VALIDATE_PREFIX` — it never returns names the
    harness does not own.

    Args:
        uri: libvirt connection URI (typically ``qemu:///system``).

    Returns:
        Sorted list of prefixed domain names (possibly empty).
    """
    return sorted(name for name in virsh_list_all_names(uri) if is_owned(name))


def reap_domain(uri: str, name: str) -> None:
    """Destroy (if running) and undefine a single harness-owned domain.

    Args:
        uri: libvirt connection URI.
        name: Domain name — **must** be harness-owned.

    Raises:
        AssertionError: ``name`` is not harness-owned (via :func:`assert_owned`).
    """
    assert_owned(name)
    # Best-effort destroy; a shut-off domain makes this fail, which is fine.
    try:
        run_virsh(uri, ["destroy", name], check=False)
    except VirshError:
        pass
    try:
        # Remove managed-save state + NVRAM so undefine can't be blocked by them.
        run_virsh(uri, ["undefine", name, "--nvram", "--managed-save"], check=False)
    except VirshError:
        # Retry without the extra flags for hosts/domains that reject them.
        run_virsh(uri, ["undefine", name], check=False)


def reap_prefixed_domains(uri: str) -> list[str]:
    """Reap every harness-owned domain on ``uri``. Prefix-scoped, never blanket.

    Args:
        uri: libvirt connection URI.

    Returns:
        The list of domain names that were reaped (for the run log).
    """
    reaped = list_prefixed_domains(uri)
    for name in reaped:
        reap_domain(uri, name)
    return reaped


def reap_prefixed_storage(
    roots: tuple[Path, ...] = (VALIDATE_STORAGE_ROOT,)
) -> list[Path]:
    """Remove harness-owned directories under the given storage roots.

    Only directories whose **name** carries :data:`LVLAB_VALIDATE_PREFIX` are
    removed; the roots themselves and any non-prefixed sibling are left alone.

    Args:
        roots: Storage roots to sweep (defaults to the harness storage root).

    Returns:
        The list of directories that were removed.
    """
    removed: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.is_dir() and is_owned(child.name):
                assert_owned(child.name)
                shutil.rmtree(child, ignore_errors=True)
                removed.append(child)
    return removed
