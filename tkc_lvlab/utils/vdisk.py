"""Module to contain vDisk operations"""

import os
import subprocess

import click


def create_vdisk(vdisk_fpath, vdisk_size, vdisk_backing_image):
    """Create a virtual disk image with qemu-img"""
    command = [
        "qemu-img",
        "create",
        "-b",
        vdisk_backing_image,
        "-f",
        "qcow2",
        "-F",
        "qcow2",
        vdisk_fpath,
        vdisk_size,
    ]

    if os.path.isfile(vdisk_fpath):
        click.echo(
            f"vDisk {vdisk_fpath} already exists, May need to clean-up previous deployment."
        )
        return

    if not os.path.exists(os.path.dirname(vdisk_fpath)):
        click.echo("vDisk path doesn't exist, attempting to create.")
        try:
            os.makedirs(os.path.exists(os.path.dirname(vdisk_fpath)))
        except Exception as e:  # pylint: disable=broad-except
            click.echo(e)
            return
    else:
        try:
            subprocess.run(command, check=True)
            click.echo("vDisk image created successfully.")
        except subprocess.CalledProcessError as e:
            click.echo(f"An error occurred creating vDisk image: {e}")
