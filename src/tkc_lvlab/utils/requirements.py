"""Runtime host-binary dependency checks for the standalone ``createvm`` script.

Ported from the sibling `lvscripts-py` project (`src/lvscripts/requirements.py`)
as part of Phase 6 — see ``docs-extra/lvscripts-survey.md`` §5 "PORT + adapt: dependency
precheck". Adapted from the original in two ways per the survey's disposition
decisions:

- **No ``genisoimage`` / ``mkisofs`` check.** lvlab builds cloud-init ISOs
    in-process with ``pycdlib`` (see :mod:`tkc_lvlab.utils.cloud_init`). The
    external ISO builder lvscripts requires is never invoked here.
- **No ``cp`` check.** lvlab creates per-VM qcow2 disks with
    ``qemu-img create -b <cloud_image>`` (backing-file mode) rather than
    duplicating the base image with ``cp``. See :mod:`tkc_lvlab.utils.vdisk`.

Required binaries reduce to ``virsh``, ``qemu-img``, ``virt-install``, and
``openssl``. The function surfaces a single :class:`DependencyError` with a
package-manager-aware install hint when any are missing, so the operator
sees one actionable message rather than a deep traceback from the first
shellout failure.

Nothing here reads ``Lvlab.yml`` or talks to libvirt directly.
"""

from __future__ import annotations

from pathlib import Path
import shlex
import shutil

# Re-export so existing imports and isinstance checks keep working after the
# class definition moved to :mod:`tkc_lvlab.exceptions`.
from ..exceptions import DependencyError

_OS_RELEASE_PATH: Path = Path("/etc/os-release")
"""Filesystem path read to classify the local package manager.

Module-level so tests can monkeypatch it to a fixture file. Do not inline
this into :func:`_detect_package_manager` — the indirection is the test
seam.
"""


_REQUIRED_BINARIES: tuple[str, ...] = (
    "virsh",
    "qemu-img",
    "virt-install",
    "openssl",
)
"""Host binaries the standalone ``createvm`` script shells out to.

Reduced from lvscripts' set (which also required ``cp`` and an ISO builder
like ``genisoimage``/``mkisofs``). See the module docstring for why those
two are dropped.
"""


def check_createvm_tooling() -> None:
    """Verify every binary in :data:`_REQUIRED_BINARIES` is on ``PATH``.

    Intended to run once at ``createvm`` startup so a missing binary
    produces an actionable error before any provisioning state is
    written to disk.

    Returns:
        ``None`` on success.

    Raises:
        DependencyError: One or more required binaries are missing.
            The exception message lists each missing binary and the
            ``sudo <package-manager> install ...`` command for the
            local OS family (when recognized).
    """
    missing: list[str] = [
        binary for binary in _REQUIRED_BINARIES if shutil.which(binary) is None
    ]
    if missing:
        raise DependencyError(_build_dependency_message(missing))


def _build_dependency_message(missing_binaries: list[str]) -> str:
    """Compose the human-readable error message for missing binaries.

    Detects the local package manager and emits the exact
    ``sudo <pm> install ...`` command. Falls back to a manual hint
    when the OS family isn't recognized.

    Args:
        missing_binaries: Names of binaries not found on ``PATH``.

    Returns:
        Multi-line message suitable for an end-user error display.
    """
    manager = _detect_package_manager()
    package_map = _package_map_by_manager(manager)

    packages: list[str] = []
    for binary in missing_binaries:
        packages.extend(package_map.get(binary, [binary]))
    packages = sorted(set(packages))

    lines = [
        "Missing required system binaries for createvm:",
        *[f"- {binary}" for binary in missing_binaries],
        "",
    ]

    if manager == "apt":
        cmd = f"sudo apt update && sudo apt install -y {' '.join(packages)}"
        lines.append(f"Install them with apt: {cmd}")
    elif manager == "dnf":
        cmd = f"sudo dnf install -y {' '.join(packages)}"
        lines.append(f"Install them with dnf: {cmd}")
    elif manager == "zypper":
        cmd = f"sudo zypper install -y {' '.join(packages)}"
        lines.append(f"Install them with zypper: {cmd}")
    elif manager == "pacman":
        cmd = f"sudo pacman -S --needed {' '.join(packages)}"
        lines.append(f"Install them with pacman: {cmd}")
    else:
        package_hint = " ".join(shlex.quote(item) for item in packages)
        lines.append(
            "Install the corresponding packages with your system package manager. "
            f"Suggested package names: {package_hint}"
        )

    return "\n".join(lines)


def _detect_package_manager() -> str:
    """Classify the local OS family by reading :data:`_OS_RELEASE_PATH`.

    Returns one of ``"apt"``, ``"dnf"``, ``"zypper"``, ``"pacman"``, or
    ``"unknown"``. Classification looks at the union of ``ID=`` and
    ``ID_LIKE=`` lines so Rocky/Alma/CentOS map to ``dnf`` via their
    ``ID_LIKE=rhel`` even when the bare ``ID`` doesn't match a known tag.

    Returns:
        Package-manager identifier, or ``"unknown"`` when the file is
        absent or the OS family is unrecognized.
    """
    if not _OS_RELEASE_PATH.exists():
        return "unknown"

    content = _OS_RELEASE_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
    values: dict[str, str] = {}
    for line in content:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"')

    joined = " ".join([values.get("ID", "").lower(), values.get("ID_LIKE", "").lower()])

    if any(tag in joined for tag in ("debian", "ubuntu")):
        return "apt"
    if any(tag in joined for tag in ("rhel", "fedora", "centos", "rocky", "almalinux")):
        return "dnf"
    if any(tag in joined for tag in ("suse", "opensuse", "sles")):
        return "zypper"
    if any(tag in joined for tag in ("arch", "manjaro")):
        return "pacman"
    return "unknown"


def _package_map_by_manager(manager: str) -> dict[str, list[str]]:
    """Return the binary→package mapping for the given package manager.

    Args:
        manager: One of the strings returned by :func:`_detect_package_manager`.

    Returns:
        Dict mapping binary name to a list of package names that provide
        it on the named manager. Unknown managers fall back to a
        Debian-style table since most package names match across
        distros for these four binaries.
    """
    if manager == "apt":
        return {
            "openssl": ["openssl"],
            "qemu-img": ["qemu-utils"],
            "virsh": ["libvirt-clients"],
            "virt-install": ["virtinst"],
        }
    if manager == "dnf":
        return {
            "openssl": ["openssl"],
            "qemu-img": ["qemu-img"],
            "virsh": ["libvirt-client"],
            "virt-install": ["virt-install"],
        }
    if manager == "zypper":
        return {
            "openssl": ["openssl"],
            "qemu-img": ["qemu-tools"],
            "virsh": ["libvirt-client"],
            "virt-install": ["virt-install"],
        }
    if manager == "pacman":
        return {
            "openssl": ["openssl"],
            "qemu-img": ["qemu-base"],
            "virsh": ["libvirt"],
            "virt-install": ["virt-install"],
        }
    return {
        "openssl": ["openssl"],
        "qemu-img": ["qemu-img"],
        "virsh": ["libvirt-client"],
        "virt-install": ["virt-install"],
    }
