"""Unit tests for the shared per-distro footprint floors.

:mod:`tkc_lvlab.footprints` is the single source of truth that both the smoke
scheduler and ``tests/test_integration_createvm.py`` consume. These tests pin
the documented floors and confirm the integration test actually imports the
shared values rather than carrying its own drifting copy.
"""

from __future__ import annotations

from tkc_lvlab.footprints import (
    DEFAULT_MEMORY_FLOOR_MIB,
    DEFAULT_VCPUS_PER_VM,
    MEMORY_FLOOR_MIB_BY_FAMILY,
    memory_floor_for_os,
    overhead_mib_for_os,
)


def test_documented_floors_match_plan():
    # The floors agreed in the #90 plan: debian 512 / ubuntu 1024 /
    # alma 1536 / fedora 2048, single vCPU.
    assert MEMORY_FLOOR_MIB_BY_FAMILY["debian"] == 512
    assert MEMORY_FLOOR_MIB_BY_FAMILY["ubuntu"] == 1024
    assert MEMORY_FLOOR_MIB_BY_FAMILY["almalinux"] == 1536
    assert MEMORY_FLOOR_MIB_BY_FAMILY["fedora"] == 2048
    assert DEFAULT_VCPUS_PER_VM == 1


def test_no_floor_below_512():
    for mib in MEMORY_FLOOR_MIB_BY_FAMILY.values():
        assert mib >= 512
    assert DEFAULT_MEMORY_FLOOR_MIB >= 512


def test_memory_floor_resolves_by_family_token():
    # A dated build / point release inherits its family's floor without a new
    # dict entry: debian13, debian11, fedora44, almalinux9 all map.
    assert memory_floor_for_os("debian13") == 512
    assert memory_floor_for_os("debian11") == 512
    assert memory_floor_for_os("fedora44") == 2048
    assert memory_floor_for_os("almalinux9") == 1536
    assert memory_floor_for_os("ubuntu2204") == 1024


def test_memory_floor_unknown_family_falls_back():
    assert memory_floor_for_os("plan9") == DEFAULT_MEMORY_FLOOR_MIB


def test_overhead_is_positive():
    assert overhead_mib_for_os("debian12") > 0


def test_createvm_integration_uses_shared_floors():
    """The createvm integration test imports the shared floor, not a local copy.

    Guards against the duplication this module was created to eliminate: if a
    future edit reintroduces a private ``_TEST_MEMORY_BY_DISTRO`` dict, this
    fails. The module imports cleanly even though its tests are gated by the
    ``integration`` marker — importing does not run them.
    """
    import tests.test_integration_createvm as itc

    # The shared helper is present and returns the documented floor as a string
    # (createvm's --memory takes a string).
    assert itc._test_memory_for("fedora44") == "2048"
    assert itc._test_memory_for("debian12") == "512"
    # The old duplicated dict is gone.
    assert not hasattr(itc, "_TEST_MEMORY_BY_DISTRO")
