"""qcow2 virtual disk creation and lifecycle helpers.

Used by the manifest workflow's :class:`tkc_lvlab.utils.libvirt.Machine`
to provision per-VM qcow2 disks under
``<disk_image_basedir>/<environment_name>/<vm_name>/diskN.qcow2``.

Two disk strategies (issue #99):

- ``copy`` (**default**) — a standalone copy of the verified cloud image
    (``cp`` + a best-effort ``qemu-img resize``). The disk has **no**
    dependency on the shared ``cloud-images/`` cache, so
    ``lvlab images clean`` can never break a running VM by pruning a base
    image. This matches what the standalone ``createvm`` script already
    does.
- ``backing`` (opt-in) — the disk references the cloud image as its qcow2
    backing file (``qemu-img create -b``), so on-disk size stays low across
    many VMs that share an OS, at the cost of a hard dependency on the
    cached base image. Selecting it warns that the cache must not be
    cleaned while the VM exists.

Select per environment with ``config_defaults.disk_strategy: copy|backing``,
overridable per disk with ``disks[*].strategy``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from .._logging import get_logger

logger = get_logger(__name__)


#: The supported disk strategies. ``copy`` is the default (cache-safe);
#: ``backing`` is the storage-efficient opt-in.
DISK_STRATEGIES = ("copy", "backing")
DEFAULT_DISK_STRATEGY = "copy"


class VirtualDisk:
    """A per-VM qcow2 disk, created as a standalone copy or a backing-file overlay.

    Attributes:
        name: Friendly name from the manifest's ``disks[*].name`` entry.
        index: Zero-based disk index used to compose the filename
            (``disk0.qcow2``, ``disk1.qcow2``, ...).
        size: qemu-img size string (e.g. ``25G``) — used at create time
            and ignored thereafter. Optional for ``copy`` (the base image
            size is kept when absent); required for ``backing``.
        strategy: ``copy`` (standalone copy, default) or ``backing``
            (cloud-image backing file).
        fpath: Absolute path on disk where the qcow2 lives.
        backing_image_fpath: Absolute path to the verified cloud image —
            the backing file (``backing`` strategy) or the copy source
            (``copy`` strategy).
    """

    def __init__(
        self,
        machine_vm_name: str,
        disk: dict[str, Any],
        disk_id: int,
        cloud_image: Any,
        environment: dict[str, Any],
        config_defaults: dict[str, Any],
    ) -> None:
        """Resolve the per-VM disk path, strategy, and base-image reference.

        Args:
            machine_vm_name: The manifest-side VM name (``vm_name``,
                not the libvirt domain name). Used to compose the
                per-VM subdirectory.
            disk: One element of the manifest's ``disks`` list. Honors
                ``name``, ``size``, and ``strategy`` keys.
            disk_id: Zero-based position in the manifest's ``disks``
                list. Drives the ``diskN.qcow2`` filename.
            cloud_image: The :class:`tkc_lvlab.utils.images.CloudImage`
                whose ``image_fpath`` is the backing file / copy source.
                Typed as ``Any`` here to avoid a circular import.
            environment: The manifest's ``environment[0]`` dict — its
                ``name`` is the per-environment subdirectory.
            config_defaults: The manifest's ``config_defaults`` dict.
                Honors ``disk_image_basedir`` (defaults to
                ``/var/lib/libvirt/images/lvlab``) and ``disk_strategy``
                (defaults to ``copy``).
        """
        self.name = disk.get("name", None)
        self.index = disk_id
        self.size = disk.get("size", None)
        self.strategy = self._resolve_strategy(disk, config_defaults)
        self.fpath = os.path.join(
            os.path.expanduser(
                config_defaults.get(
                    "disk_image_basedir", "/var/lib/libvirt/images/lvlab"
                )
            ),
            environment.get("name", "LvLabEnvironment"),
            machine_vm_name,
            "disk" + f"{disk_id}" + ".qcow2",
        )
        self.backing_image_fpath = cloud_image.image_fpath

    @staticmethod
    def _resolve_strategy(disk: dict[str, Any], config_defaults: dict[str, Any]) -> str:
        """Resolve the disk strategy: per-disk override, else default, else ``copy``.

        An unrecognized value falls back to ``copy`` with a warning rather
        than failing the whole provision.
        """
        raw = disk.get("strategy") or config_defaults.get("disk_strategy")
        if raw is None:
            return DEFAULT_DISK_STRATEGY
        strategy = str(raw).strip().lower()
        if strategy not in DISK_STRATEGIES:
            logger.warning(
                "Unknown disk strategy %r; valid: %s. Defaulting to %r.",
                raw,
                ", ".join(DISK_STRATEGIES),
                DEFAULT_DISK_STRATEGY,
            )
            return DEFAULT_DISK_STRATEGY
        return strategy

    def exists(self) -> bool:
        """Return True if the qcow2 file is already on disk.

        Returns:
            ``True`` when ``self.fpath`` is a regular file; ``False``
            otherwise.
        """
        return os.path.isfile(self.fpath)

    def create(self) -> bool:
        """Create the qcow2 using the resolved strategy.

        Ensures the parent directory exists, then dispatches to the
        ``copy`` (standalone) or ``backing`` (overlay) builder.

        Returns:
            ``True`` on success, ``False`` if directory creation or the
            underlying ``cp`` / ``qemu-img`` step failed.
        """
        if not self._ensure_parent_dir():
            return False
        if self.strategy == "backing":
            return self._create_backing()
        return self._create_copy()

    def _ensure_parent_dir(self) -> bool:
        """Create the disk's parent directory if absent. Returns success."""
        parent = os.path.dirname(self.fpath)
        if not os.path.exists(parent):
            try:
                os.makedirs(parent)
            except Exception as e:  # pylint: disable=broad-except
                logger.error("Exception creating %s: %s", parent, e)
                return False
        return True

    def _create_copy(self) -> bool:
        """Create a standalone qcow2 by copying the base image, then resizing.

        ``cp`` of the verified cloud image, followed by a best-effort
        ``qemu-img resize`` to :attr:`size`. qcow2 cannot shrink, so a
        ``size`` at or below the base image's virtual size makes ``resize``
        fail — that's tolerated (warn, keep the base size) rather than
        failing the provision (issue #88 / #99). With no ``size`` the base
        image size is kept as-is.

        Returns:
            ``True`` on a successful copy (resize failures are non-fatal),
            ``False`` if the copy itself failed.
        """
        try:
            shutil.copyfile(self.backing_image_fpath, self.fpath)
        except OSError as e:
            logger.error("Error copying base image to %s: %s", self.fpath, e)
            return False

        if self.size:
            try:
                subprocess.run(
                    ["qemu-img", "resize", self.fpath, self.size],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except subprocess.CalledProcessError as e:
                stderr = (e.stderr or b"").decode(errors="replace").strip()
                logger.warning(
                    "Could not resize %s to %s; keeping the base image size "
                    "(qcow2 cannot shrink): %s",
                    self.fpath,
                    self.size,
                    stderr,
                )
        return True

    def _create_backing(self) -> bool:
        """Create a backing-file qcow2 via ``qemu-img create -b ...``.

        Returns:
            ``True`` on success, ``False`` if ``qemu-img`` failed.
        """
        command = [
            "qemu-img",
            "create",
            "-b",
            self.backing_image_fpath,
            "-f",
            "qcow2",
            "-F",
            "qcow2",
            self.fpath,
            self.size,
        ]
        try:
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            return True
        except subprocess.CalledProcessError as e:
            logger.error("Error in qemu-img call: %s", e)
            return False

    def delete(self) -> None:
        """Remove the qcow2 file from disk.

        A missing file is treated as a no-op (no exception). Removal
        failures are logged but not raised — the caller can verify via
        :meth:`exists` if removal mattered.
        """
        if os.path.exists(self.fpath):
            try:
                os.remove(self.fpath)
            except Exception as e:  # pylint: disable=broad-except
                logger.error("Exception removing vdisk: %s", e)
