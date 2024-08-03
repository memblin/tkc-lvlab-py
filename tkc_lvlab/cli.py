"""A CLI for deploying lab VMs on Libvirt"""

import click
import libvirt
import os
import sys

from tkc_lvlab.config import parse_config, parse_file_from_url
from tkc_lvlab.logging import get_logger
from tkc_lvlab.utils.libvirt import get_domain_state_string
from tkc_lvlab.utils.images import CloudImage
from tkc_lvlab.utils.vdisk import create_vdisk


logger = get_logger(__name__)


def connect_to_libvirt(uri=None):
    """Connect to Hypervisor"""
    if uri == None:
        uri = "qemu:///system"

    conn = libvirt.open(uri)
    if not conn:
        raise SystemExit(f"Failed to open connection to {uri}")

    return conn


def get_machine_by_hostname(machines, hostname):
    """Get a machine by hostname from the machines list"""
    for machine in machines:
        if machine.get("hostname", None) == hostname:
            return machine
    return None


@click.group()
def run():
    """A command-line tool for managing VMs."""
    pass


@click.command()
def init():
    """Initialize the environment."""
    environment, images, config_defaults, machines = parse_config()

    logger.info(f'Initializing Libvirt Lab Environment: {environment["name"]}')
    logger.info("")

    for image_config in images:
        image = CloudImage(image_config, environment, config_defaults)

        if image.exists_locally("image"):
            logger.info(f"CloudImage {image.name} exists locally: {image.image_fpath}")
        else:
            logger.info(f"Attempting to download image: {image.image_url}")
            if image.download_image():
                logger.info(f"CloudImage downloaded to {image.image_fpath}")
            else:
                logger.error("CloudImage download failed")

        if image.checksum_url_gpg is not None:
            if image.exists_locally(file_type=("checksum_gpg")):
                logger.info(
                    f"CloudImage {image.name} checksum GPG file exists locally: {image.checksum_gpg_fpath}"
                )
            else:
                if image.download_checksum_gpg():
                    logger.info(
                        f"CloudImage checksum GPG file downloaded to {image.checksum_gpg_fpath}"
                    )
                else:
                    logger.error(f"CloudImage checksum GPG file download failed")

        if image.checksum_url is not None:
            if image.exists_locally(file_type="checksum"):
                logger.info(
                    f"CloudImage {image.name} checksum file exists locally: {image.checksum_fpath}"
                )
            else:
                logger.info(
                    f"Attempting to download checksum file URL: {image.checksum_url}"
                )
                if image.download_checksum():
                    logger.info(
                        f"CloudImage checksum file downloaded to {image.checksum_fpath}"
                    )
                else:
                    logger.error("CloudImage checksum file download failed")

        if image.checksum_url_gpg is not None and image.exists_locally(
            file_type=("checksum_gpg")
        ):
            if image.gpg_verify_checksum_file():
                logger.info(f"CloudImage checksum file GPG validation OK")
            else:
                logger.error(f"CloudImage checksum file GPG validation BAD")

        if image.checksum_url is not None and image.exists_locally(
            file_type=("checksum")
        ):
            if image.checksum_verify_image():
                logger.info(f"CloudImage checksum verification OK")
            else:
                logger.error(f"CloudImage checksum verification BAD")


@click.command()
@click.argument("vm_name")
def up(vm_name):
    """Start a machine defined in the Lvlab.yml manifest."""
    environment, images, config_defaults, machines = parse_config()

    cloud_image_dir = config_defaults.get("cloud_image_base_dir", "/var/lib/libvirt")
    cloud_image_dir += "/cloud-images"

    disk_image_dir = config_defaults.get("disk_image_base_dir", "/var/lib/libvirt")
    disk_image_dir += f"/{environment.get("name", "lvlab_noname")}"

    # Lookup our machine config from the Lvlab.yml manifest
    machine = get_machine_by_hostname(machines, vm_name)
    if machine:
        # Connect to Libvirt
        conn = connect_to_libvirt()

        # Get a list of current VMs
        current_vms = [dom.name() for dom in conn.listAllDomains()]

        if vm_name in current_vms:
            print(f"The VM, {vm_name}, already exists.")

            vm = conn.lookupByName(machine["hostname"])
            vm_status, vm_status_reason = get_domain_state_string(vm.state())
            print(f"Status: {vm_status}, {vm_status_reason}")

            # If VM is shutdown, start it up
            if vm_status in ["Shut Off", "Crashed"]:
                print(f"Trying to start {vm_name}...")
                if vm.create() > 0:
                    raise SystemExit(f"Cannot boot VM {vm_name}")

                cur_vm_status, cur_vm_status_reason = get_domain_state_string(
                    vm.state()
                )
                print(f"Status: {cur_vm_status}, {cur_vm_status_reason}")

            elif vm_status in ["Running"]:
                print(f"The VM {vm_name} is running already.")

        else:
            print(f"The VM {vm_name}, doesn't exist yet.")
            print(f"Creating VM: {vm_name}.")

            vdisk_fpath = os.path.join(
                disk_image_dir, machine.get("hostname"), "disk0.qcow2"
            )

            backing_image = [
                img
                for img in images
                if img["name"]
                == environment.get("os", config_defaults.get("os", "fedora40"))
            ][0]
            backing_image_name = parse_file_from_url(backing_image["image_url"])
            vdisk_backing_fpath = cloud_image_dir + "/" + backing_image_name

            if os.path.isfile(vdisk_fpath):
                raise SystemExit(
                    f"The vDisk {vdisk_fpath} already exists. May need to clean-up a previous deployment.\n"
                )

            if not os.path.exists(os.path.dirname(vdisk_fpath)):
                os.makedirs(os.path.dirname(vdisk_fpath))

            create_vdisk(
                vdisk_fpath,
                machine.get("disk", config_defaults.get("disk", "15GB")),
                vdisk_backing_fpath,
            )

            # TODO: Create cloud-init data and iso
            # TODO: virt-install the VM and check status

        conn.close()

    else:
        click.echo(f"Machine not found: {vm_name}")

    print()


@click.command()
@click.argument("vm_name")
def destroy(vm_name):
    """Destroy a VM."""
    click.echo(f"Destroying VM: {vm_name}")


@click.command()
@click.argument("vm_name")
def down(vm_name):
    """Shutdown a machine defined in the Lvlab.yml manifest."""
    click.echo(f"Shutting down VM: {vm_name}")
    environment, images, config_defaults, machines = parse_config()

    # Lookup our machine config from the Lvlab.yml manifest
    machine = get_machine_by_hostname(machines, vm_name)
    if machine:
        # Connect to Libvirt
        conn = connect_to_libvirt()

        # Get a list of current VMs
        current_vms = [dom.name() for dom in conn.listAllDomains()]

        if vm_name in current_vms:
            print(f"The VM {vm_name} exists.")

            vm = conn.lookupByName(machine["hostname"])
            vm_status, vm_status_reason = get_domain_state_string(vm.state())
            print(f"Status: {vm_status}, {vm_status_reason}")

            # If VM is shutdown, start it up
            if vm_status in ["Running"]:
                print(f"Trying to Shutdown {vm_name}...")
                if vm.shutdown() > 0:
                    raise SystemExit(f"Cannot shutdown VM {vm_name}")

            elif vm_status in ["Shut Off", "Crashed"]:
                print(f"The VM {vm_name} is not Running.")

        else:
            print(f"The VM {vm_name}, doesn't exist")

        conn.close()

    else:
        click.echo(f"Machine not found:  {vm_name}")

    print()


@click.command()
def capabilities():
    """Hypervisor Capabilities"""
    conn = connect_to_libvirt()

    caps = conn.getCapabilities()
    print("Capabilities:\n" + caps)

    conn.close()


@click.command()
def status():
    """Show the status of the environment."""
    environment, images, config_defaults, machines = parse_config()

    # conn = connect_to_libvirt()

    # # Get a list of current VMs
    # current_vms = [dom.name() for dom in conn.listAllDomains()]

    # print("Machines Defined:\n")
    # for machine in machines:
    #     if machine["hostname"] in current_vms:
    #         vm = conn.lookupByName(machine["hostname"])
    #         vm_status, vm_status_reason = get_domain_state_string(vm.state())
    #         print(f"  - { machine['hostname'] } is {vm_status} ({vm_status_reason})")
    #     else:
    #         print(f"  - { machine['hostname'] } is undeployed")

    # print("Images Used:\n")
    # for img in images:
    #     print(f"  - { img['name'] }")


# Bulid the CLI
run.add_command(up)
run.add_command(down)
run.add_command(destroy)
run.add_command(init)
run.add_command(status)
run.add_command(capabilities)


if __name__ == "__main__":
    run()
