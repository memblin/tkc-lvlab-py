"""Unit tests for :mod:`tkc_lvlab.utils.osinfo`.

Covers the fallback chain that lets ``--os-variant`` requests keep
working on hosts whose ``osinfo-db`` package predates the requested
variant. Without this resolution, ``virt-install`` hard-fails with
``Unknown OS name 'debian13'`` on long-stable Debian 12 hosts (their
osinfo-db is from 2022-11-30, before Debian 13 existed).
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from tkc_lvlab.utils import osinfo
from tkc_lvlab.utils.osinfo import (
    OsInfoLookupError,
    list_available_os_variants,
    resolve_os_variant,
)

# A representative slice of what ``virt-install --osinfo list`` emits on
# a current-ish host. Each line is one or more comma-separated aliases.
_SAMPLE_OSINFO_LIST_OUTPUT = """\
debian13, debiantrixie
debian12, debianbookworm
debian11, debianbullseye
debian10, debianbuster
debian-current
fedora44
fedora43
fedora42
fedora-current
ubuntu24.04, ubuntunoble
ubuntu22.04, ubuntujammy
ubuntu-current
almalinux10.1
almalinux10.0
almalinux10-unknown
linux-current
generic
"""


@pytest.fixture(autouse=True)
def _clear_osinfo_cache():
    """Make every test see a fresh lookup — the module caches via ``lru_cache``."""
    list_available_os_variants.cache_clear()
    yield
    list_available_os_variants.cache_clear()


# ---------------------------------------------------------------------------
# list_available_os_variants — parsing + error translation
# ---------------------------------------------------------------------------


def test_list_available_parses_comma_separated_aliases() -> None:
    """Every alias on each line lands in the set, not just the first one."""
    with mock.patch.object(
        osinfo.subprocess,
        "run",
        return_value=subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_SAMPLE_OSINFO_LIST_OUTPUT,
            stderr="",
        ),
    ):
        variants = list_available_os_variants()

    assert "debian13" in variants
    assert "debiantrixie" in variants
    assert "ubuntu24.04" in variants
    assert "ubuntunoble" in variants
    assert "generic" in variants


def test_list_available_translates_missing_virt_install() -> None:
    """A missing virt-install binary surfaces as OsInfoLookupError, not FileNotFoundError."""
    with mock.patch.object(
        osinfo.subprocess, "run", side_effect=FileNotFoundError("virt-install")
    ):
        with pytest.raises(OsInfoLookupError, match="virt-install not found"):
            list_available_os_variants()


def test_list_available_translates_nonzero_exit() -> None:
    """A non-zero virt-install exit also surfaces as OsInfoLookupError with stderr."""
    err = subprocess.CalledProcessError(
        returncode=2,
        cmd=["virt-install", "--osinfo", "list"],
        output="",
        stderr="osinfo: lookup failed\n",
    )
    with mock.patch.object(osinfo.subprocess, "run", side_effect=err):
        with pytest.raises(OsInfoLookupError, match="exit 2"):
            list_available_os_variants()


def test_list_available_translates_empty_output() -> None:
    """An empty listing is treated as a broken osinfo-db, not a silent no-op."""
    with mock.patch.object(
        osinfo.subprocess,
        "run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        ),
    ):
        with pytest.raises(OsInfoLookupError, match="no parseable entries"):
            list_available_os_variants()


# ---------------------------------------------------------------------------
# resolve_os_variant — fallback preference
# ---------------------------------------------------------------------------


def test_resolve_exact_match_returns_requested() -> None:
    """When the requested variant is available, no fallback message."""
    available = frozenset({"debian13", "debian12", "debian-current"})
    resolved, reason = resolve_os_variant("debian13", available=available)
    assert resolved == "debian13"
    assert reason is None


def test_resolve_falls_back_to_highest_lower_version_in_family() -> None:
    """``debian13`` on a host that only knows debian10..12 picks debian12."""
    available = frozenset({"debian12", "debian11", "debian10", "linux-current"})
    resolved, reason = resolve_os_variant("debian13", available=available)
    assert resolved == "debian12"
    assert reason is not None
    assert "debian13" in reason
    assert "debian12" in reason


def test_resolve_falls_back_skipping_gaps() -> None:
    """If debian12 is missing but debian11 exists, walk down to debian11."""
    available = frozenset({"debian11", "debian10", "linux-current"})
    resolved, _ = resolve_os_variant("debian13", available=available)
    assert resolved == "debian11"


def test_resolve_falls_back_to_family_current_when_no_versioned() -> None:
    """When no versioned same-family entries exist, prefer ``{family}-current``."""
    available = frozenset({"debian-current", "linux-current", "generic"})
    resolved, reason = resolve_os_variant("debian13", available=available)
    assert resolved == "debian-current"
    assert "debian-current" in reason


def test_resolve_falls_back_to_linux_current_for_unknown_family() -> None:
    """An unknown family slug (e.g. ``thisisnotreal42``) lands on linux-current."""
    available = frozenset({"linux-current", "generic", "debian13"})
    resolved, reason = resolve_os_variant("thisisnotreal42", available=available)
    assert resolved == "linux-current"
    assert "linux-current" in reason


def test_resolve_falls_back_to_generic_as_last_resort() -> None:
    """When even linux-current is gone, fall through to ``generic``."""
    available = frozenset({"generic"})
    resolved, reason = resolve_os_variant("debian13", available=available)
    assert resolved == "generic"
    assert "generic" in reason


def test_resolve_raises_when_truly_nothing_available() -> None:
    """An osinfo-db with no recognized fallback is an unrecoverable env."""
    available = frozenset({"some-weird-variant-only"})
    with pytest.raises(ValueError, match="Install a newer osinfo-db"):
        resolve_os_variant("debian13", available=available)


def test_resolve_does_not_walk_below_one() -> None:
    """The version walk stops at 1; it doesn't try ``debian0`` or negative numbers."""
    available = frozenset({"linux-current"})
    resolved, _ = resolve_os_variant("debian13", available=available)
    # Confirms we fell through to linux-current — i.e. the version walk
    # didn't somehow produce a match by accident.
    assert resolved == "linux-current"


def test_resolve_handles_non_integer_version_via_family_current() -> None:
    """Non-integer versions (e.g. ``ubuntu24.04``) skip the version walk."""
    available = frozenset({"ubuntu-current", "linux-current"})
    resolved, reason = resolve_os_variant("ubuntu24.04", available=available)
    assert resolved == "ubuntu-current"
    assert "ubuntu-current" in reason
