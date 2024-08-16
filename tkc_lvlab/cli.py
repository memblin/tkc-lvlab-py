"""A CLI for deploying lab VMs on Libvirt"""

import os
import sys

import click
from .config import parse_config
from .utils.cloud_init import CloudInitIso, MetaData, NetworkConfig, UserData
from .utils.libvirt import (
    connect_to_libvirt,
    get_domain_state_string,
    get_machine_by_vm_name,
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

    # TODO: Would this classify well into an environment object with
    # a list of CloudImages?
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

        if image.checksum_url_gpg:
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

        if image.checksum_url:
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

        if image.checksum_url_gpg and image.exists_locally(file_type=("checksum_gpg")):
            if image.gpg_verify_checksum_file():
                click.echo(f"CloudImage {image.name} checksum file GPG validation OK")
            else:
                click.echo(f"CloudImage {image.name} checksum file GPG validation BAD")

        if image.checksum_url and image.exists_locally(file_type=("checksum")):
            if image.checksum_verify_image():
                click.echo(f"CloudImage {image.name} checksum verification OK")
            else:
                click.echo(f"CloudImage {image.name} checksum verification BAD")

        click.echo()


@click.command()
@click.argument("vm_name")
def cloudinit(vm_name):
    """Render the cloud-init template for a machine defined in the Lvlab.yml manifest."""
    try:
        environment, images, config_defaults, machines = parse_config()
    except TypeError as e:
        click.echo("Could not parse config file.")
        sys.exit(1)

    machine = Machine(
        get_machine_by_vm_name(machines, vm_name), environment, config_defaults
    )

    if machine:
        cloud_image = CloudImage(
            machine.os, images.get(machine.os), environment, config_defaults
        )
        # Render and write cloud-init config
        _, _, _ = machine.cloud_init(cloud_image, config_defaults)


@click.command()
@click.argument("vm_name")
def up(vm_name):
    """Start a machine defined in the Lvlab.yml manifest."""
    try:
        environment, images, config_defaults, machines = parse_config()
    except TypeError as e:
        click.echo("Could not parse config file.")
        sys.exit(1)

    machine = Machine(
        get_machine_by_vm_name(machines, vm_name), environment, config_defaults
    )

    if machine:

        exists, status, status_reason = machine.exists_in_libvirt(
            environment.get("libvirt_uri", None)
        )

        if exists:
            if status in ["Shut Off", "Crashed"]:
                click.echo(f"Virtual machine exists, trying to start {machine.vm_name}")
                if machine.poweron(environment.get("libvirt_uri", None)) > 0:
                    click.echo(f"Problem powering on VM {machine.vm_name}")
            elif status in ["Running"]:
                click.echo(f"The virtual machine {machine.vm_name} is running already")

        else:
            click.echo(f"Creating virtual machine: {machine.vm_name}")

            cloud_image = CloudImage(
                machine.os, images.get(machine.os), environment, config_defaults
            )

            # TODO: Check if vdisks exist before trying to create
            machine.create_vdisks(environment, config_defaults, cloud_image)

            # Render and write cloud-init config
            metadata_config_fpath, userdata_config_fpath, network_config_fpath = (
                machine.cloud_init(cloud_image, config_defaults)
            )

            # Write cloud-init config files to ISO to mount during launch
            iso = CloudInitIso(
                metadata_config_fpath,
                userdata_config_fpath,
                network_config_fpath,
                os.path.join(machine.config_fpath, "cidata.iso"),
            )
            click.echo(f"Writing cloud-init config ISO file {iso.fpath}")
            if iso.write(iso.fpath):
                click.echo(f"Writing cloud-init config ISO successful")
            else:
                click.echo(f"Writing cloud-init config ISO failed.")
                sys.exit(1)

            # virt-install the VM and check status
            click.echo(f"Attempting to start virtual maching: {machine.vm_name}")
            if machine.deploy(
                machine.config_fpath,
                config_defaults,
                environment.get("libvirt_uri", "qemu:///session"),
            ):
                click.echo(f"Virtual machine deployment complete.")
            else:
                click.echo(f"Virtual machine installation failed.")

    else:
        click.echo(f"Machine not found: {vm_name}")


@click.command()
@click.argument("vm_name")
def destroy(vm_name):
    """Destroy a Virtual machine listed in the LvLab manifest"""
    try:
        environment, images, config_defaults, machines = parse_config()
    except TypeError as e:
        click.echo("Could not parse config file.")
        sys.exit(1)

    machine = Machine(
        get_machine_by_vm_name(machines, vm_name), environment, config_defaults
    )

    if machine:
        # TODO: Ask for confirmation before destroying, control with feature flag?
        if machine.destroy(
            machine.config_fpath, environment.get("libvirt_uri", "qemu:///session")
        ):
            click.echo(f"Destruction appears successful.")
    else:
        click.echo(f"Machine not found:  {vm_name}")


@click.command()
@click.argument("vm_name")
def down(vm_name):
    """Shutdown a machine defined in the Lvlab.yml manifest."""
    try:
        environment, images, config_defaults, machines = parse_config()
    except TypeError as e:
        click.echo("Could not parse config file.")
        sys.exit(1)

    machine = Machine(
        get_machine_by_vm_name(machines, vm_name), environment, config_defaults
    )

    if machine:
        click.echo(f"Shutting down virtual machine: {vm_name}")
        if machine.shutdown(environment.get("libvirt_uri", "qemu:///session")) > 0:
            click.echo(f"Shutdown appears to have failed.")

        else:
            click.echo(
                f"Shutdown appears successful. The virtual machine may take a short time to complete shutdown."
            )
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
        sys.exit(1)

    click.echo()
    click.echo(f'LvLab Environment Name: {environment.get("name", "no-name-lvlab")}\n')
    conn = connect_to_libvirt(environment.get("libvirt_uri", None))

    # Get a list of current VMs
    current_vms = [dom.name() for dom in conn.listAllDomains()]

    click.echo("Machines Defined:\n")
    for machine in machines:
        if machine["vm_name"] in current_vms:
            vm = conn.lookupByName(machine["vm_name"])
            vm_status, vm_status_reason = get_domain_state_string(vm.state())
            click.echo(
                f"  - { machine['vm_name'] } is {vm_status} ({vm_status_reason})"
            )
        else:
            click.echo(f"  - { machine['vm_name'] } is undeployed")

    click.echo()
    click.echo("Images Used:\n")
    for image_name, image_date in images.items():
        click.echo(
            f'  - {image_name} from {image_date.get("image_url", "Missing Image URL.")}'
        )

    click.echo()


# Bulid the CLI
run.add_command(cloudinit)
run.add_command(up)
run.add_command(down)
run.add_command(destroy)
run.add_command(init)
run.add_command(status)
run.add_command(capabilities)


if __name__ == "__main__":
    run()
