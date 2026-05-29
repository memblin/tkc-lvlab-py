"""Per-run configuration shared by every scenario handler.

A single :class:`RunContext` is built once in :mod:`__main__` and threaded
through the scheduler into each handler, so handlers never reach for global
state (which would make them untestable and order-dependent).
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunContext:
    """Immutable settings for one harness run.

    Attributes:
        uri: libvirt connection URI the harness targets (``qemu:///system``).
        bin_dir: Directory to resolve the ``lvlab``/``createvm``/``deletevm``
            binaries from (the built artifact under test). Falls back to PATH.
        workdir: Scratch directory for generated manifests and per-scenario cwd.
        ssh_key: Path to a private key for in-guest checks, or ``None`` to skip
            them (lease + domain-state verification still run).
        dhcp_poll_retries: Times to poll ``virsh domifaddr`` for a lease.
        dhcp_poll_interval_s: Seconds between lease polls.
        cmd_timeout_s: Hard timeout for a single binary invocation.
        boot_timeout_s: Hard timeout for a provisioning (``createvm``) call.
        dry_run: When True, no VM is provisioned; only the cheap lane executes.
    """

    uri: str = "qemu:///system"
    bin_dir: Path | None = None
    workdir: Path = Path("/tmp/lvlab-validate")
    ssh_key: Path | None = None
    dhcp_poll_retries: int = 30
    dhcp_poll_interval_s: float = 3.0
    cmd_timeout_s: float = 120.0
    boot_timeout_s: float = 600.0
    dry_run: bool = False

    def binary(self, name: str) -> str:
        """Resolve a CLI binary path for ``name``.

        Prefers ``bin_dir`` (defaulting to the running interpreter's bin dir —
        the venv), then PATH. The returned path is what the harness executes,
        so a run always exercises a known build.

        Args:
            name: ``"lvlab"``, ``"createvm"``, or ``"deletevm"``.

        Returns:
            An absolute path to the binary, or bare ``name`` as a last resort
            (let the OS raise a clear ``FileNotFoundError`` at exec time).
        """
        search = self.bin_dir or Path(sys.executable).parent
        candidate = search / name
        if candidate.is_file():
            return str(candidate)
        found = shutil.which(name)
        return found or name
