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

        if image.checksum_url_gpg and image.exists_locally(
            file_type=("checksum_gpg")
        ):
            if image.gpg_verify_checksum_file():
                click.echo(f"CloudImage {image.name} checksum file GPG validation OK")
            else:
                click.echo(f"CloudImage {image.name} checksum file GPG validation BAD")

        if image.checksum_url and image.exists_locally(
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

    machine = Machine(get_machine_by_vm_name(machines, vm_name), config_defaults)

    if machine:
        conn = connect_to_libvirt()
        current_vms = [dom.name() for dom in conn.listAllDomains()]

        if vm_name in current_vms:
            click.echo(f"The virtual machine {vm_name} already exists.")
            vm = conn.lookupByName(machine.vm_name)
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
                vdisk = VirtualDisk(
                    machine.hostname,
                    disk,
                    index,
                    cloud_image,
                    environment,
                    config_defaults,
                )

                if vdisk.exists():
                    click.echo(f"Virtual Disk: {vdisk.name} exists at {vdisk.fpath}")
                else:
                    click.echo(f"Creating Virtual Disk: {vdisk.fpath} at {vdisk.size}")
                    if vdisk.create():
                        if vdisk.exists():
                            click.echo(f"Virtual Disk Created Successfully")
                    else:
                        click.echo(f"Failed to create Virtual Disk: {vdisk.fpath}")

            config_fpath = os.path.join(
                config_defaults.get("disk_image_basedir", "~/.local/lvlab"),
                environment.get("name", "LvLabEnvironment"),
                machine.hostname,
            )

            # Render and write cloud-init: network-config
            network_config = NetworkConfig(
                cloud_image.network_version, machine.interfaces
            )
            rendered_network_config = network_config.render_config()
            network_config_fpath = os.path.join(config_fpath, "network-config")
            click.echo(f"Writing cloud-init network config file {network_config_fpath}")
            with open(
                network_config_fpath, "w", encoding="utf-8"
            ) as network_config_file:
                network_config_file.write(rendered_network_config)

            # Render and write cloud-init: meta-data
            metadata_config = MetaData(machine.hostname)
            rendered_metadata_config = metadata_config.render_config()
            metadata_config_fpath = os.path.join(config_fpath, "meta-data")
            click.echo(f"Writing cloud-init meta-data file {metadata_config_fpath}")
            with open(
                metadata_config_fpath, "w", encoding="utf-8"
            ) as metadata_config_file:
                metadata_config_file.write(rendered_metadata_config)

            # Render and write cloud-init: user-data
            userdata_config = UserData(
                config_defaults.get("cloud_init", {}), machine.hostname, machine.domain
            )
            rendered_userdata_config = userdata_config.render_config()
            userdata_config_fpath = os.path.join(config_fpath, "user-data")
            click.echo(f"Writing cloud-init user-data file {userdata_config_fpath}")
            with open(
                userdata_config_fpath, "w", encoding="utf-8"
            ) as userdata_config_file:
                userdata_config_file.write(rendered_userdata_config)

            # Write cloud-init config files to ISO to mount during launch
            iso = CloudInitIso(metadata_config_fpath, userdata_config_fpath, network_config_fpath)
            click.echo(f"Writing cloud-init config ISO file {os.path.join(config_fpath, 'cidata.iso')}")
            if iso.write(config_fpath):
                click.echo(f'Writing cloud-init config ISO successful')
            else:
                click.echo(f'Writing cloud-init config ISO failed.')
                sys.exit(1)

            # TODO: virt-install the VM and check status
            click.echo(f"Attempting to start virtual maching: {machine.hostname}.{machine.domain}")
            if machine.deploy(config_fpath, environment.get('libvirt_uri', 'qemu:///session')):
                click.echo(f"Virtual machine deployment complete.")
            else:
                click.echo(f"Virtual machine installation failed.")

        conn.close()

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

    machine = Machine(get_machine_by_vm_name(machines, vm_name), config_defaults)

    if machine:
        click.echo(f"Destroying virtual machine: {vm_name}")
        if machine.destroy(environment.get('libvirt_uri', 'qemu:///session')):
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

    machine = Machine(get_machine_by_vm_name(machines, vm_name), config_defaults)

    if machine:
        click.echo(f"Shutting down virtual machine: {vm_name}")
        if machine.shutdown(environment.get('libvirt_uri', 'qemu:///session')):
            click.echo(f"Shutdown appears successful. The virtual machine may take a short time to complete shutdown.")
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
