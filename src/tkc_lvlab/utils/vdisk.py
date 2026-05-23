"""qcow2 virtual disk creation and lifecycle helpers.

Used by the manifest workflow's :class:`tkc_lvlab.utils.libvirt.Machine`
to provision per-VM backing-file qcow2 disks under
``<disk_image_basedir>/<environment_name>/<vm_name>/diskN.qcow2``. Every
disk references the verified cloud image as its qcow2 backing file
(``qemu-img create -b ...``), so on-disk size stays low across many
manifest VMs that share an OS.

The standalone ``createvm`` script does NOT use this class — see
:mod:`tkc_lvlab.scripts.createvm` for the standalone path which can opt
into a copy-strategy disk instead of backing-file.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from .._logging import get_logger


logger = get_logger(__name__)


class VirtualDisk:
    """A per-VM qcow2 disk with a cloud-image backing file.

    Attributes:
        name: Friendly name from the manifest's ``disks[*].name`` entry.
        index: Zero-based disk index used to compose the filename
            (``disk0.qcow2``, ``disk1.qcow2``, ...).
        size: qemu-img size string (e.g. ``25G``) — used at create time
            and ignored thereafter.
        fpath: Absolute path on disk where the qcow2 lives.
        backing_image_fpath: Absolute path to the verified cloud image
            this disk uses as its backing file.
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
        """Resolve the per-VM disk path and capture the backing image reference.

        Args:
            machine_vm_name: The manifest-side VM name (``vm_name``,
                not the libvirt domain name). Used to compose the
                per-VM subdirectory.
            disk: One element of the manifest's ``disks`` list. Honors
                ``name`` and ``size`` keys.
            disk_id: Zero-based position in the manifest's ``disks``
                list. Drives the ``diskN.qcow2`` filename.
            cloud_image: The :class:`tkc_lvlab.utils.images.CloudImage`
                whose ``image_fpath`` becomes this disk's backing file.
                Typed as ``Any`` here to avoid a circular import.
            environment: The manifest's ``environment[0]`` dict — its
                ``name`` is the per-environment subdirectory.
            config_defaults: The manifest's ``config_defaults`` dict.
                Honors ``disk_image_basedir``; defaults to
                ``/var/lib/libvirt/images/lvlab``.
        """
        self.name = disk.get("name", None)
        self.index = disk_id
        self.size = disk.get("size", None)
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

    def exists(self) -> bool:
        """Return True if the qcow2 file is already on disk.

        Returns:
            ``True`` when ``self.fpath`` is a regular file; ``False``
            otherwise.
        """
        return os.path.isfile(self.fpath)

    def create(self) -> bool:
        """Create the backing-file qcow2 via ``qemu-img create -b ...``.

        Ensures the parent directory exists, then shells out to
        ``qemu-img create -b <backing> -f qcow2 -F qcow2 <fpath> <size>``.
        On any failure (mkdir or qemu-img), the error is logged and
        ``False`` is returned — callers should check the return value.

        Returns:
            ``True`` on success, ``False`` if directory creation or
            ``qemu-img`` failed.
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

        if not os.path.exists(os.path.dirname(self.fpath)):
            try:
                os.makedirs(os.path.dirname(self.fpath))
            except Exception as e:  # pylint: disable=broad-except
                logger.error(
                    "Exception creating %s: %s", os.path.dirname(self.fpath), e
                )
                return False

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
