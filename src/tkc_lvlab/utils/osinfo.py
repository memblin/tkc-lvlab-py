"""Fallback resolution for ``virt-install --os-variant`` values.

``virt-install`` consults libosinfo's database (``osinfo-db``) to map
``--os-variant=<id>`` to tuning hints. When the requested variant
isn't in the host's osinfo-db, virt-install hard-fails with
``Unknown OS name 'X'``. That's surprising on long-supported hosts:
Debian 12 in 2026 still ships osinfo-db 0.20221130 from main, which
predates Debian 13's release — so requesting ``--distro debian13``
fails even though the same flag works fine on Debian 13 hosts.

This module sniffs the host's available variants via
``virt-install --osinfo list`` and picks the best match for a
requested name. The match preference, in order:

1. Exact match.
2. For families like ``debianNN`` / ``fedoraNN`` with an integer
   version: walk down to the highest available older version in
   the same family.
3. ``{family}-current`` (osinfo always tracks a per-family current
   alias even when specific versions lag).
4. ``linux-current``.
5. ``generic`` (kernel-only hints, no OS specifics).

When a fallback is selected the caller receives both the resolved
variant and a short, human-readable reason so it can be logged at
the call site.
"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache

# Re-export so existing imports and isinstance checks keep working after the
# class definition moved to :mod:`tkc_lvlab.exceptions`.
from ..exceptions import OsInfoLookupError
from .subprocess_env import system_first_env


_FAMILY_PATTERN = re.compile(r"^([a-zA-Z]+)(\d+(?:\.\d+)?)$")
"""Splits an osinfo id like ``debian13`` or ``fedora40`` into family and version."""


_GENERIC_FALLBACKS = ("linux-current", "generic")
"""Last-resort os-variants tried after family-specific options are exhausted."""


@lru_cache(maxsize=1)
def list_available_os_variants() -> frozenset[str]:
    """Return the set of OS variants known to the host's ``osinfo-db``.

    Shells out to ``virt-install --osinfo list`` (which is the same
    source virt-install itself consults). The result is cached for
    the lifetime of the process via :func:`functools.lru_cache` —
    osinfo-db doesn't change during one lvlab invocation, and a
    single ``up`` run can deploy multiple VMs.

    Tests that need to exercise different sets of available variants
    can call ``list_available_os_variants.cache_clear()`` between
    cases.

    Returns:
        Every os-variant alias virt-install recognizes (including
        every comma-separated alias on each line of the listing).

    Raises:
        OsInfoLookupError: ``virt-install`` is missing, fails, or
            produces an unparseable listing.
    """
    try:
        result = subprocess.run(
            ["virt-install", "--osinfo", "list"],
            check=True,
            capture_output=True,
            text=True,
            env=system_first_env(),
        )
    except FileNotFoundError as exc:
        raise OsInfoLookupError(
            "virt-install not found on PATH; cannot enumerate osinfo variants"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise OsInfoLookupError(
            f"virt-install --osinfo list failed (exit {exc.returncode}): "
            f"{(exc.stderr or '').strip()}"
        ) from exc

    variants: set[str] = set()
    for line in result.stdout.splitlines():
        for alias in line.split(","):
            alias = alias.strip()
            if alias:
                variants.add(alias)
    if not variants:
        raise OsInfoLookupError(
            "virt-install --osinfo list returned no parseable entries"
        )
    return frozenset(variants)


def resolve_os_variant(
    requested: str,
    available: frozenset[str] | None = None,
) -> tuple[str, str | None]:
    """Pick the best available os-variant for ``requested``.

    See the module docstring for the match-preference order.

    Args:
        requested: The os-variant id the caller wanted (e.g.
            ``debian13``, ``fedora40``).
        available: Override the available set — tests pass this
            directly so they don't shell out. Production callers
            leave it as ``None`` and the function queries the
            cached :func:`list_available_os_variants`.

    Returns:
        ``(resolved_variant, fallback_reason)``. ``fallback_reason``
        is ``None`` when the exact requested variant was used; a
        short human-readable string otherwise (suitable for logging
        next to the requested name).

    Raises:
        ValueError: No matching variant — not even the generic
            fallbacks — is present in the available set. Indicates
            an unusually broken osinfo-db install.
    """
    avail = available if available is not None else list_available_os_variants()

    if requested in avail:
        return requested, None

    match = _FAMILY_PATTERN.match(requested)
    if match:
        family = match.group(1).lower()
        version_str = match.group(2)

        # Walk down integer versions in the same family. The first hit
        # is the highest available version <= requested.
        try:
            n = int(version_str)
            for candidate_n in range(n - 1, 0, -1):
                candidate = f"{family}{candidate_n}"
                if candidate in avail:
                    return (
                        candidate,
                        f"{requested!r} unknown to osinfo-db; using older {candidate!r}",
                    )
        except ValueError:
            # Non-integer version (e.g. ``ubuntu24.04``). Skip the
            # version-walk and fall through to family-current below.
            pass

        family_current = f"{family}-current"
        if family_current in avail:
            return (
                family_current,
                f"{requested!r} unknown to osinfo-db; using {family_current!r}",
            )

    for fallback in _GENERIC_FALLBACKS:
        if fallback in avail:
            return (
                fallback,
                f"{requested!r} unknown to osinfo-db; using generic {fallback!r}",
            )

    raise ValueError(
        f"virt-install knows no os-variant matching {requested!r} and no "
        f"generic fallback ({', '.join(_GENERIC_FALLBACKS)}) is available. "
        f"Install a newer osinfo-db."
    )
