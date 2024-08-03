import click
import libvirt
import os
import requests
import yaml

from urllib.parse import urlparse
from tkc_lvlab.utils.libvirt import get_domain_state_string
from tkc_lvlab.utils.images import download_file


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


def parse_config(fpath=None):
    """Read config file"""

    if fpath == None:
        fpath = "Lvlab.yml"

    if os.path.isfile(fpath):
        print(f"Loading {fpath} config...\n")
        with open(fpath, "r") as f:
            config = yaml.safe_load(f)

        environment = config["environment"][0]
        images = config["images"]
        config_defaults = environment.get("config_defaults", {})
        machines = environment.get("machines", {})

        return (environment, images, config_defaults, machines)

    else:
        print(f"{fpath} not found. Please create enviornment definition.")

    return (None, None, None, None)


def parse_file_from_url(url):
    """Return the filename from the end of a URL"""
    parsed_url = urlparse(url)
    filename = os.path.basename(parsed_url.path)

    return filename


@click.group()
def run():
    """A command-line tool for managing VMs."""
    print()
    pass


@click.command()
@click.argument("vm_name")
def up(vm_name):
    """Start a machine defined in the Lvlab.yml manifest."""
    environment, images, config_defaults, machines = parse_config()

    cloud_image_dir = config_defaults.get(
        "cloud_image_base_dir", "/var/lib/libvirt/cloud-images"
    )

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

        #     # TODO: Create disk images backed by cloud image
        #     # TODO: Create cloud-init data and iso
        #     # TODO: virt-install the VM and check status

        conn.close()

    else:
        click.echo(f"Machine not found:  {vm_name}")

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

        #     # TODO: Create disk images backed by cloud image
        #     # TODO: Create cloud-init data and iso
        #     # TODO: virt-install the VM and check status

        conn.close()

    else:
        click.echo(f"Machine not found:  {vm_name}")

    print()


@click.command()
def init():
    """Initialize the environment."""
    environment, images, config_defaults, _ = parse_config()
    print(f'Initializing Libvirt Lab Environment: {environment["name"]}\n')

    cloud_image_dir = config_defaults.get(
        "cloud_image_base_dir", "/var/lib/libvirt/cloud-images"
    )

    for image in images:
        image_fname = parse_file_from_url(image["image_url"])
        image_fpath = os.path.join(cloud_image_dir, image_fname)

        if os.path.isfile(image_fpath):
            print(f"The image {image_fpath} already exists.")
        else:
            print(f"The image {image_fpath} does not exist, attempting to download.")
            download_file(image["image_url"], image_fpath)

        if image["checksum_url"]:
            print("Checksum URL is set, validating checksum of existing cloud image")
            checksum_url_fname = parse_file_from_url(image["checksum_url"])
            checksum_url_fpath = os.path.join(cloud_image_dir, checksum_url_fname)

            if os.path.isfile(checksum_url_fpath):
                print(f"The image checksum file already exists.")
            else:
                print(
                    f"The image checksum file {checksum_url_fpath} does not exist, attempting to download."
                )
                download_file(image["checksum_url"], checksum_url_fpath)

        if image.get("checksum_url_gpg", None):
            print(
                "Checksum URL GPG is set, this is normally to validate the checksum_url content."
            )
            checksum_url_gpg_fname = parse_file_from_url(image["checksum_url_gpg"])
            checksum_url_gpg_fpath = os.path.join(
                cloud_image_dir, checksum_url_gpg_fname
            )

            if os.path.isfile(checksum_url_gpg_fpath):
                print(
                    f"The image checksum GPG file {checksum_url_gpg_fpath} already exists."
                )
            else:
                print(
                    f"The image checksum GPG file {checksum_url_gpg_fpath} does not exist, attempting to download."
                )
                download_file(image["checksum_url_gpg"], checksum_url_gpg_fpath)

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
    print()

    environment, images, config_defaults, machines = parse_config()

    conn = connect_to_libvirt()

    # Get a list of current VMs
    current_vms = [dom.name() for dom in conn.listAllDomains()]

    print("Machines Defined:\n")
    for machine in machines:
        if machine["hostname"] in current_vms:
            vm = conn.lookupByName(machine["hostname"])
            vm_status, vm_status_reason = get_domain_state_string(vm.state())
            print(f"  - { machine['hostname'] } is {vm_status} ({vm_status_reason})")
        else:
            print(f"  - { machine['hostname'] } is undeployed")

    print()

    print("Images Used:\n")
    for img in images:
        print(f"  - { img['name'] }")

    print()


# Bulid the CLI
run.add_command(up)
run.add_command(down)
run.add_command(destroy)
run.add_command(init)
run.add_command(status)
run.add_command(capabilities)


if __name__ == "__main__":
    run()
