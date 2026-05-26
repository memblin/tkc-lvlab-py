"""Single source of truth for per-distro VM footprint floors.

The smoke runner's resource-aware scheduler (:mod:`tkc_lvlab.smoke`) and the
createvm integration matrix (``tests/test_integration_createvm.py``) both need
the smallest reliable per-distro guest memory. Before this module those floors
were duplicated — in ``docs-extra/smoke/Lvlab.yml`` and in the integration
test's local ``_TEST_MEMORY_BY_DISTRO`` — and drifted independently. This
module is the one place they live in code; the test imports
:data:`MEMORY_FLOOR_MIB_BY_FAMILY` instead of its own copy.

The floors are documented distro minimums, never below 512 MiB: Debian boots
happily at 512, Ubuntu's cloud images want ~1 GiB, AlmaLinux's docs floor is
1.5 GiB, and Fedora Cloud's is 2 GiB. All run on a single vCPU.

``docs-extra/smoke/Lvlab.yml`` is hand-maintained YAML kept in lockstep with
these values — YAML can't import Python, so its per-machine ``memory:`` is
matched by hand (a comment there points back here).
"""

from __future__ import annotations

import re

# Smallest reliable guest memory (MiB) per distro family. Keyed by the leading
# alphabetic family token of a catalog key / ``machine.os`` (``debian13`` ->
# ``debian``), so a new dated build or point release inherits its family's
# floor without a new entry.
MEMORY_FLOOR_MIB_BY_FAMILY: dict[str, int] = {
    "debian": 512,
    "ubuntu": 1024,
    "almalinux": 1536,
    "rocky": 1536,
    "centos": 1536,
    "rhel": 1536,
    "fedora": 2048,
}

# Fallback floor for a family not yet tuned above. Matches the integration
# matrix's historical default and never drops below the 512 MiB hard minimum.
DEFAULT_MEMORY_FLOOR_MIB = 1024

# Per-VM scheduling allowances. ``OVERHEAD_MIB`` is the qemu/firmware slack the
# scheduler budgets on top of guest RAM so a batch's real host footprint is not
# undercounted. Every smoke guest runs on a single vCPU.
OVERHEAD_MIB = 256
DEFAULT_VCPUS_PER_VM = 1


def _family_token(key: str) -> str:
    """Return the leading alphabetic family token of a catalog/``os`` key.

    ``debian12`` -> ``debian``, ``fedora44`` -> ``fedora``. Falls back to the
    whole lower-cased key when it has no leading letters.

    Args:
        key: A catalog key, ``VM_DISTRO``, or ``machine.os`` value.

    Returns:
        The lower-cased family token.
    """
    match = re.match(r"[a-z]+", key.lower())
    return match.group(0) if match else key.lower()


def memory_floor_for_os(os_key: str) -> int:
    """Return the documented memory floor (MiB) for a distro key.

    Args:
        os_key: A catalog key / ``machine.os`` value (e.g. ``almalinux9``).

    Returns:
        The family floor from :data:`MEMORY_FLOOR_MIB_BY_FAMILY`, or
        :data:`DEFAULT_MEMORY_FLOOR_MIB` for an untuned family.
    """
    return MEMORY_FLOOR_MIB_BY_FAMILY.get(
        _family_token(os_key), DEFAULT_MEMORY_FLOOR_MIB
    )


def overhead_mib_for_os(os_key: str) -> int:
    """Return the per-VM qemu overhead allowance (MiB) for a distro key.

    Currently a flat :data:`OVERHEAD_MIB` for every family; the per-key
    signature leaves room for a heavier allowance on a specific family later
    without touching call sites.

    Args:
        os_key: A catalog key / ``machine.os`` value.

    Returns:
        The overhead allowance in MiB.
    """
    return OVERHEAD_MIB
