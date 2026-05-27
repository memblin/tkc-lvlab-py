"""Unit tests for :mod:`tkc_lvlab.utils.requirements`.

Locked-in contracts:

- ``check_createvm_tooling`` raises ``DependencyError`` when ANY required
    binary is missing; success returns ``None``.
- The required-binary set is exactly virsh, qemu-img, virt-install, openssl
    (the lvscripts-port adaptations from Phase 5 — no genisoimage, no cp).
- ``_detect_package_manager`` reads ``_OS_RELEASE_PATH`` and classifies
    apt/dnf/zypper/pacman from ``ID=`` and ``ID_LIKE=`` lines.
- The install-hint message names every missing binary and emits the
    correct ``sudo <pm> install ...`` command for the detected manager.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest import mock

import pytest

from tkc_lvlab.utils import requirements as req_mod
from tkc_lvlab.utils.requirements import (
    DependencyError,
    check_createvm_tooling,
)

# ---------------------------------------------------------------------------
# check_createvm_tooling — happy path + missing-binary path
# ---------------------------------------------------------------------------


def test_check_succeeds_when_all_binaries_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When shutil.which returns a path for every required binary, the check returns None."""
    monkeypatch.setattr(shutil, "which", lambda binary: f"/usr/bin/{binary}")
    # Must not raise.
    assert check_createvm_tooling() is None


def test_check_raises_when_virsh_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single missing binary trips the gate."""

    def which(binary: str) -> str | None:
        return None if binary == "virsh" else f"/usr/bin/{binary}"

    monkeypatch.setattr(shutil, "which", which)
    # Redirect os-release to a non-existent path so package-manager detection
    # falls through to 'unknown' and we don't depend on the host OS.
    monkeypatch.setattr(req_mod, "_OS_RELEASE_PATH", tmp_path / "absent")

    with pytest.raises(DependencyError, match="virsh"):
        check_createvm_tooling()


def test_check_lists_every_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """All missing binaries appear in the error message, not just the first one detected.

    Real-bug surface: if the check ever stops collecting and short-circuits
    on the first miss, the operator would fix one binary, re-run, hit the
    next miss, fix it, repeat. The whole point of one-shot validation is
    to surface every gap at once.
    """

    def which(binary: str) -> str | None:
        # Both virsh and qemu-img absent.
        return None if binary in ("virsh", "qemu-img") else f"/usr/bin/{binary}"

    monkeypatch.setattr(shutil, "which", which)

    with pytest.raises(DependencyError) as excinfo:
        check_createvm_tooling()

    msg = str(excinfo.value)
    assert "virsh" in msg
    assert "qemu-img" in msg


def test_check_does_not_require_genisoimage_or_cp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """genisoimage/mkisofs/cp are NOT in the required set (Phase 6 lvlab uses pycdlib + qemu-img -b).

    Regression guard: if some future refactor accidentally re-introduces
    the lvscripts check for genisoimage or cp, this fails. The two
    skipped lvscripts dependencies are documented in the module
    docstring and the Phase 5 survey — they should never come back
    without intentional re-evaluation.
    """

    def which(binary: str) -> str | None:
        if binary in ("genisoimage", "mkisofs", "cp"):
            return None
        return f"/usr/bin/{binary}"

    monkeypatch.setattr(shutil, "which", which)

    # The check must NOT raise — those three binaries aren't required.
    assert check_createvm_tooling() is None


# ---------------------------------------------------------------------------
# _detect_package_manager — classification from /etc/os-release
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("os_release_content", "expected"),
    [
        (
            "ID=ubuntu\nID_LIKE=debian\n",
            "apt",
        ),
        (
            "ID=debian\n",
            "apt",
        ),
        (
            "ID=fedora\n",
            "dnf",
        ),
        (
            # Rocky Linux: ID_LIKE carries the rhel hint, ID alone wouldn't.
            'ID=rocky\nID_LIKE="rhel centos fedora"\n',
            "dnf",
        ),
        (
            'ID=opensuse-tumbleweed\nID_LIKE="suse opensuse"\n',
            "zypper",
        ),
        (
            "ID=arch\n",
            "pacman",
        ),
        (
            "ID=manjaro\nID_LIKE=arch\n",
            "pacman",
        ),
        (
            # An obscure distro with no recognized tags falls through to unknown.
            "ID=somedistro\n",
            "unknown",
        ),
    ],
)
def test_detect_package_manager_from_os_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    os_release_content: str,
    expected: str,
) -> None:
    """Each major Linux family maps to its expected package manager.

    The right behavior here matters because the install hint shown to the
    operator depends on this classification. A misclassification means
    pasting a wrong ``sudo`` command into the wrong package manager.
    """
    fake_os_release = tmp_path / "os-release"
    fake_os_release.write_text(os_release_content)
    monkeypatch.setattr(req_mod, "_OS_RELEASE_PATH", fake_os_release)

    assert req_mod._detect_package_manager() == expected


def test_detect_package_manager_when_os_release_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-existent /etc/os-release returns 'unknown' (no crash)."""
    missing = tmp_path / "definitely-not-here"
    monkeypatch.setattr(req_mod, "_OS_RELEASE_PATH", missing)

    assert req_mod._detect_package_manager() == "unknown"


# ---------------------------------------------------------------------------
# Install-hint message shape
# ---------------------------------------------------------------------------


def test_install_hint_emits_apt_command_on_debian_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Debian/Ubuntu host gets a ``sudo apt update && sudo apt install -y ...`` hint."""
    fake_os_release = tmp_path / "os-release"
    fake_os_release.write_text("ID=ubuntu\nID_LIKE=debian\n")
    monkeypatch.setattr(req_mod, "_OS_RELEASE_PATH", fake_os_release)
    monkeypatch.setattr(
        shutil, "which", lambda binary: None if binary == "virsh" else "/x"
    )

    with pytest.raises(DependencyError) as excinfo:
        check_createvm_tooling()

    msg = str(excinfo.value)
    assert "sudo apt update && sudo apt install -y" in msg
    # The package that ships virsh on apt is libvirt-clients.
    assert "libvirt-clients" in msg


def test_install_hint_emits_dnf_command_on_fedora_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Fedora/RHEL host gets a ``sudo dnf install -y ...`` hint with libvirt-client (no -s)."""
    fake_os_release = tmp_path / "os-release"
    fake_os_release.write_text("ID=fedora\n")
    monkeypatch.setattr(req_mod, "_OS_RELEASE_PATH", fake_os_release)
    monkeypatch.setattr(
        shutil, "which", lambda binary: None if binary == "virsh" else "/x"
    )

    with pytest.raises(DependencyError) as excinfo:
        check_createvm_tooling()

    msg = str(excinfo.value)
    assert "sudo dnf install -y" in msg
    # Fedora's package name is libvirt-client (singular), NOT libvirt-clients.
    assert "libvirt-client" in msg
    assert "libvirt-clients" not in msg


def test_install_hint_falls_back_for_unknown_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrecognized OS still gets a usable message with package names listed."""
    fake_os_release = tmp_path / "os-release"
    fake_os_release.write_text("ID=somedistro\n")
    monkeypatch.setattr(req_mod, "_OS_RELEASE_PATH", fake_os_release)
    monkeypatch.setattr(
        shutil, "which", lambda binary: None if binary == "virsh" else "/x"
    )

    with pytest.raises(DependencyError) as excinfo:
        check_createvm_tooling()

    msg = str(excinfo.value)
    # The fallback hint mentions "your system package manager".
    assert "your system package manager" in msg
    # And still lists the candidate package names.
    assert "libvirt-client" in msg
