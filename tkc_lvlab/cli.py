"""A CLI for deploying lab VMs on Libvirt"""

import os
import sys

import click
from .config import parse_config
from .utils.cloud_init import NetworkConfig
from .utils.libvirt import (
    connect_to_libvirt,
    get_domain_state_string,
    get_machine_by_hostname,
    Machine,
)
from .utils.vdisk import VirtualDisk
from .utils.images import CloudImage


@click.group()
def run():
    """A command-line tool for managing VMs."""
    pass


@click.command()
def init():
    """Initialize the environment."""
    try:
        environment, images, config_defaults, _ = parse_config()
    except TypeError as e:
        click.echo("Could not parse config file.")
        sys.exit()

    click.echo()
    click.echo(f'Initializing Libvirt Lab Environment: {environment["name"]}\n')

    for image_name, image_config in images.items():
        image = CloudImage(image_name, image_config, environment, config_defaults)

        if image.exists_locally("image"):
            click.echo(f"CloudImage {image.name} exists locally: {image.image_fpath}")
        else:
            click.echo(f"Attempting to download image: {image.image_url}")
            if image.download_image():
                click.echo(f"CloudImage downloaded to {image.image_fpath}")
            else:
                click.echo("CloudImage download failed")

        if image.checksum_url_gpg is not None:
            if image.exists_locally(file_type=("checksum_gpg")):
                click.echo(
                    f"CloudImage {image.name} checksum GPG file exists locally: {image.checksum_gpg_fpath}"
                )
            else:
                if image.download_checksum_gpg():
                    click.echo(
                        f"CloudImage checksum GPG file downloaded to {image.checksum_gpg_fpath}"
                    )
                else:
                    click.echo(f"CloudImage checksum GPG file download failed")

        if image.checksum_url is not None:
            if image.exists_locally(file_type="checksum"):
                click.echo(
                    f"CloudImage {image.name} checksum file exists locally: {image.checksum_fpath}"
                )
            else:
                click.echo(
                    f"Attempting to download checksum file URL: {image.checksum_url}"
                )
                if image.download_checksum():
                    click.echo(
                        f"CloudImage {image.name} checksum file downloaded to {image.checksum_fpath}"
                    )
                else:
                    click.echo("CloudImage {image.name} checksum file download failed")

        if image.checksum_url_gpg is not None and image.exists_locally(
            file_type=("checksum_gpg")
        ):
            if image.gpg_verify_checksum_file():
                click.echo(f"CloudImage {image.name} checksum file GPG validation OK")
            else:
                click.echo(f"CloudImage {image.name} checksum file GPG validation BAD")

        if image.checksum_url is not None and image.exists_locally(
            file_type=("checksum")
        ):
            if image.checksum_verify_image():
                click.echo(f"CloudImage {image.name} checksum verification OK")
            else:
                click.echo(f"CloudImage {image.name} checksum verification BAD")

        click.echo()


@click.command()
@click.argument("vm_name")
def up(vm_name):
    """Start a machine defined in the Lvlab.yml manifest."""
    try:
        environment, images, config_defaults, machines = parse_config()
    except TypeError as e:
        click.echo("Could not parse config file.")
        sys.exit(1)

    machine = Machine(get_machine_by_hostname(machines, vm_name), config_defaults)

    if machine:
        conn = connect_to_libvirt()
        current_vms = [dom.name() for dom in conn.listAllDomains()]

        if vm_name in current_vms:
            click.echo(f"The virtual machine {vm_name} already exists.")
            vm = conn.lookupByName(machine.hostname)
            vm_status, vm_status_reason = get_domain_state_string(vm.state())
            click.echo(f"Status: {vm_status}, {vm_status_reason}")

            # If VM is shutdown, start it up
            if vm_status in ["Shut Off", "Crashed"]:
                click.echo(f"Trying to start {vm_name}...")
                if vm.create() > 0:
                    raise SystemExit(f"Cannot boot VM {vm_name}")

                cur_vm_status, cur_vm_status_reason = get_domain_state_string(
                    vm.state()
                )
                click.echo(f"Status: {cur_vm_status}, {cur_vm_status_reason}")

            elif vm_status in ["Running"]:
                click.echo(f"The virtual machine {vm_name} is running already")

        else:
            click.echo(f"The virtual machine {vm_name} doesn't exist yet")
            click.echo(f"Creating virtual machine: {vm_name}")

            cloud_image = CloudImage(
                machine.os, images.get(machine.os), environment, config_defaults
            )

            # Create Virtual Disks
            for index, disk in enumerate(machine.disks):
                vdisk = VirtualDisk(machine.hostname, disk, index, cloud_image, environment, config_defaults)

                if vdisk.exists():
                    click.echo(f"Virtual Disk: {vdisk.name} exists at {vdisk.fpath}")
                else:
                    click.echo(f"Creating Virtual Disk: {vdisk.fpath} at {vdisk.size}")
                    if vdisk.create():
                        if vdisk.exists():
                            click.echo(f"Virtual Disk Created Successfully")
                    else:
                        click.echo(f"Failed to create Virtual Disk: {vdisk.fpath}")

            # TODO: Create cloud-init data and iso
            network_config = NetworkConfig(cloud_image.network_version, machine.interfaces)
            rendered_network_config = network_config.render_network_config()

            network_config_fpath = os.path.join(
                config_defaults.get("disk_image_basedir", "/var/lib/libvirt/images"),
                environment.get("name", "LvLabEnvironment"),
                machine.hostname,
                "network-config"
            )

            with open(network_config_fpath, "w", encoding="utf-8") as network_config_file:
                network_config_file.write(rendered_network_config)

            #  - meta-data
            #  - user-data
             
             
            # TODO: virt-install the VM and check status

        conn.close()

    else:
        click.echo(f"Machine not found: {vm_name}")


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


@click.command()
def capabilities():
    """Hypervisor Capabilities"""
    conn = connect_to_libvirt()

    caps = conn.getCapabilities()
    click.echo("Capabilities:\n" + caps)

    conn.close()


@click.command()
def status():
    """Show the status of the environment."""
    try:
        environment, images, config_defaults, machines = parse_config()
    except TypeError as e:
        click.echo("Could not parse config file.")
        sys.exit()

    click.echo()
    click.echo(f'LvLab Environment Name: {environment.get("name", "no-name-lvlab")}\n')
    conn = connect_to_libvirt()

    # Get a list of current VMs
    current_vms = [dom.name() for dom in conn.listAllDomains()]

    click.echo("Machines Defined:\n")
    for machine in machines:
        if machine["hostname"] in current_vms:
            vm = conn.lookupByName(machine["hostname"])
            vm_status, vm_status_reason = get_domain_state_string(vm.state())
            click.echo(
                f"  - { machine['hostname'] } is {vm_status} ({vm_status_reason})"
            )
        else:
            click.echo(f"  - { machine['hostname'] } is undeployed")

    click.echo()
    click.echo("Images Used:\n")
    for image_name, image_date in images.items():
        click.echo(
            f"  - {image_name} from {image_date.get("image_url", "Missing Image URL.")}"
        )

    click.echo()


# Bulid the CLI
run.add_command(up)
run.add_command(down)
run.add_command(destroy)
run.add_command(init)
run.add_command(status)
run.add_command(capabilities)


if __name__ == "__main__":
    run()
