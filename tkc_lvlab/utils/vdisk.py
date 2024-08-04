"""Module to contain vDisk operations"""

import os
import subprocess

import click


class VirtualDisk:
    """A virtual disk definition"""

    def __init__(
        self, machine_hostname, disk, disk_id, cloud_image, environment, config_defaults
    ):
        """VirtualDisk"""
        self.name = disk.get("name", None)
        self.index = disk_id
        self.size = disk.get("size", None)
        self.fpath = os.path.join(
            config_defaults.get("disk_image_basedir", "/var/lib/libvirt/images"),
            environment.get("name", "LvLabEnvironment"),
            machine_hostname,
            "disk" + f"{disk_id}" + ".qcow2",
        )
        self.backing_image_fpath = cloud_image.image_fpath

    def exists(self):
        """Report if the virtual disk exists"""
        if os.path.isfile(self.fpath):
            return True

        return False

    def create(self):
        """Create a virtual disk"""

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
                click.echo(f"Failed to create image path: {e}")
                return
        else:
            try:
                subprocess.run(
                    command,
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                return True
            except subprocess.CalledProcessError as e:
                click.echo(f"Error in qemu-img call: {e}")
                return False
