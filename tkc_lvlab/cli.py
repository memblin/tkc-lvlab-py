"""A CLI for deploying lab VMs on Libvirt"""

import os
import sys

import click
import libvirt

from ._logging import configure_logging, get_logger
from .config import (
    parse_config,
    generate_hosts,
    generate_hosts_entries,
    parse_hosts_file,
)
from .utils.cloud_init import CloudInitIso, MetaData, NetworkConfig, UserData
from .utils.libvirt import (
    connect_to_libvirt,
    get_machine_state,
    get_machine_by_vm_name,
    Machine,
)
from .utils.vdisk import VirtualDisk
from .utils.images import CloudImage


logger = get_logger(__name__)


@click.group(context_settings=dict(help_option_names=["-h", "--help"]))
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="Increase log verbosity (-v for INFO, -vv for DEBUG).",
)
@click.option(
    "-q",
    "--quiet",
    is_flag=True,
    default=False,
    help="Suppress informational logs (ERROR only). Overrides -v.",
)
def run(verbose, quiet):
    """A command-line tool for managing VMs."""
    configure_logging(verbosity=verbose, quiet=quiet)


@click.command()
def capabilities():
    """Hypervisor Capabilities"""
    conn = connect_to_libvirt()

    caps = conn.getCapabilities()
    click.echo("Capabilities:\n" + caps)

    conn.close()


@click.command()
@click.argument("vm_name")
def cloudinit(vm_name):
    """Render the cloud-init template for a machine defined in the Lvlab.yml manifest."""
    try:
        environment, images, config_defaults, machines = parse_config()
    except TypeError as e:
        logger.error("Could not parse config file.")
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
@click.option("--force", is_flag=True, help="Force destruction without confirmation.")
def destroy(vm_name, force=False):
    """Destroy a Virtual machine listed in the LvLab manifest"""
    try:
        environment, _, config_defaults, machines = parse_config()
    except TypeError as e:
        logger.error("Could not parse config file.")
        sys.exit(1)

    machine_config = get_machine_by_vm_name(machines, vm_name)
    if machine_config:

        machine = Machine(
            get_machine_by_vm_name(machines, vm_name), environment, config_defaults
        )
        libvirt_endpoint = environment.get("libvirt_uri", "qemu:///session")

        if machine:
            exists, _, _ = machine.exists_in_libvirt(libvirt_endpoint)
            if exists:
                if force or click.confirm(
                    f"Are you sure you want to destroy {machine.vm_name}?"
                ):
                    if machine.destroy(libvirt_endpoint):
                        click.echo(
                            f"Destruction appears successful for {machine.vm_name}."
                        )
                    else:
                        logger.error(
                            "Destruction appears to have failed for %s.",
                            machine.vm_name,
                        )

                else:
                    click.echo(f"Destruction aborted for {machine.vm_name}.")
            else:
                logger.warning(
                    "Machine %s is not deployed to the configured in %s.",
                    machine.vm_name,
                    libvirt_endpoint,
                )
    else:
        logger.error("Machine not found in manifest: %s", vm_name)


@click.command()
@click.argument("vm_name")
def down(vm_name):
    """Shutdown a machine defined in the Lvlab.yml manifest."""
    try:
        environment, _, config_defaults, machines = parse_config()
    except TypeError as e:
        logger.error("Could not parse config file.")
        sys.exit(1)

    machine_config = get_machine_by_vm_name(machines, vm_name)
    if machine_config:

        machine = Machine(
            get_machine_by_vm_name(machines, vm_name), environment, config_defaults
        )

        exists, state, _ = machine.exists_in_libvirt(
            environment.get("libvirt_uri", "qemu:///session")
        )

        if exists:
            if state in ["VIR_DOMAIN_RUNNING", "VIR_DOMAIN_PAUSED"]:
                click.echo(f"Shutting down virtual machine {vm_name}.")
                if (
                    machine.shutdown(environment.get("libvirt_uri", "qemu:///session"))
                    > 0
                ):
                    logger.error("Shutdown appears to have failed.")
                else:
                    click.echo(
                        f"Shutdown appears successful. The virtual machine may take a short time to complete shutdown."
                    )
            elif state in ["VIR_DOMAIN_SHUTOFF"]:
                click.echo(
                    f"The virtual machine {machine.vm_name} is shutdown already."
                )

    else:
        logger.error("Machine %s not found in manifest.", vm_name)


@click.command()
@click.option(
    "--append", is_flag=True, help="Attempt to append hosts snippet to /etc/hosts."
)
@click.option(
    "--heredoc",
    is_flag=True,
    help="Render hosts snippet as a heredoc to append to /etc/hosts.",
)
def hosts(append=False, heredoc=False):
    """Provide /etc/hosts support

    This command by default will only print recommended
    /etc/hosts snippets to the screen.

    Flags can augment the output.

    --append : Attempt to append hosts snippet to /etc/hosts.

      Needs privs like; sudo $(which lvlab) hosts --append

    --heredoc : Render hosts snippet as a heredoc to append to /etc/hosts.

    """
    try:
        environment, _, config_defaults, machines = parse_config()
    except TypeError as e:
        logger.error("Could not parse config file.")
        sys.exit()

    hosts_snippet = generate_hosts(environment, config_defaults, machines)

    if append:
        etc_hosts = "/etc/hosts"
        try:
            existing_ips, existing_names = parse_hosts_file(etc_hosts)
        except OSError as e:
            logger.error("Unable to read %s: %s", etc_hosts, e)
            sys.exit(1)

        candidates = generate_hosts_entries(config_defaults, machines)
        to_append = []
        for entry in candidates:
            conflict_reasons = []
            if entry["ip4"] in existing_ips:
                conflict_reasons.append(f"IP {entry['ip4']} already present")
            if entry["hostname"] and entry["hostname"].lower() in existing_names:
                conflict_reasons.append(f"hostname {entry['hostname']} already present")
            if entry["fqdn"] and entry["fqdn"].lower() in existing_names:
                conflict_reasons.append(f"fqdn {entry['fqdn']} already present")

            if conflict_reasons:
                click.echo(
                    f"Skipping {entry['ip4']} {entry['fqdn']} {entry['hostname']}: "
                    + "; ".join(conflict_reasons)
                )
            else:
                to_append.append(entry)

        if not to_append:
            click.echo("No new entries to append to /etc/hosts.")
        elif os.access(etc_hosts, os.W_OK) or (
            not os.path.exists(etc_hosts)
            and os.access(os.path.dirname(etc_hosts) or ".", os.W_OK)
        ):
            logger.info("Appending hosts file snippet to %s", etc_hosts)
            header = f"\n# Libvirt Labs /etc/hosts snippet\n# Environment: {environment.get('name', '')}\n"
            try:
                with open(etc_hosts, "a", encoding="utf-8") as hosts_file:
                    hosts_file.write(header)
                    for entry in to_append:
                        line = f"{entry['ip4']} {entry['fqdn']} {entry['hostname']}\n"
                        hosts_file.write(line)
                        click.echo(f"Appended: {line.rstrip()}")
            except OSError as e:
                logger.error("Unable to write %s: %s", etc_hosts, e)
                sys.exit(1)
        else:
            logger.error("No write access available for /etc/hosts")

    if heredoc:
        hosts_snippet = generate_hosts(
            environment, config_defaults, machines, heredoc="/etc/hosts"
        )

    click.echo(f"{hosts_snippet}")


@click.command()
def init():
    """Initialize the environment."""
    try:
        environment, images, config_defaults, _ = parse_config()
    except TypeError as e:
        logger.error("Could not parse config file.")
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
            logger.info("Attempting to download image: %s", image.image_url)
            if image.download_image():
                click.echo(f"CloudImage downloaded to {image.image_fpath}")
            else:
                logger.error("CloudImage download failed")

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
                    logger.error("CloudImage checksum GPG file download failed")

        if image.checksum_url:
            if image.exists_locally(file_type="checksum"):
                click.echo(
                    f"CloudImage {image.name} checksum file exists locally: {image.checksum_fpath}"
                )
            else:
                logger.info(
                    "Attempting to download checksum file URL: %s", image.checksum_url
                )
                if image.download_checksum():
                    click.echo(
                        f"CloudImage {image.name} checksum file downloaded to {image.checksum_fpath}"
                    )
                else:
                    logger.error(
                        "CloudImage %s checksum file download failed", image.name
                    )

        if image.checksum_url_gpg and image.exists_locally(file_type=("checksum_gpg")):
            if image.gpg_verify_checksum_file():
                click.echo(f"CloudImage {image.name} checksum file GPG validation OK")
            else:
                logger.error(
                    "CloudImage %s checksum file GPG validation BAD", image.name
                )

        if image.checksum_url and image.exists_locally(file_type=("checksum")):
            if image.checksum_verify_image():
                click.echo(f"CloudImage {image.name} checksum verification OK")
            else:
                logger.error("CloudImage %s checksum verification BAD", image.name)

        click.echo()


@click.group()
def snapshot():
    """Snapshot management commands."""
    pass


@snapshot.command()
@click.argument("vm_name")
def list(vm_name):
    """List snapshots for a given VM."""
    try:
        environment, _, config_defaults, machines = parse_config()
    except TypeError as e:
        logger.error("Could not parse config file.")
        sys.exit(1)

    machine_config = get_machine_by_vm_name(machines, vm_name)
    if machine_config:

        machine = Machine(
            get_machine_by_vm_name(machines, vm_name), environment, config_defaults
        )
        libvirt_endpoint = environment.get("libvirt_uri", "qemu:///session")

        if machine:
            exists, _, _ = machine.exists_in_libvirt(libvirt_endpoint)
            if exists:
                click.echo(f"Listing snapshots for {machine.vm_name}")
                snapshots = machine.list_snapshots(libvirt_endpoint)
                if snapshots:
                    for snapshot in snapshots:
                        click.echo(f"  - {snapshot.getName()}")
                else:
                    click.echo(f"No snapshots found for {machine.vm_name}")
            else:
                logger.warning(
                    "Machine %s is not deployed to the configured in %s.",
                    machine.vm_name,
                    libvirt_endpoint,
                )
    else:
        logger.error("Machine not found in manifest: %s", vm_name)


@snapshot.command()
@click.argument("vm_name")
@click.argument("snapshot_name")
@click.argument("snapshot_description", default=None, required=False)
def create(vm_name, snapshot_name, snapshot_description=None):
    """List snapshots for a given VM."""
    try:
        environment, _, config_defaults, machines = parse_config()
    except TypeError as e:
        logger.error("Could not parse config file.")
        sys.exit(1)

    machine_config = get_machine_by_vm_name(machines, vm_name)
    if machine_config:

        machine = Machine(
            get_machine_by_vm_name(machines, vm_name), environment, config_defaults
        )
        libvirt_endpoint = environment.get("libvirt_uri", "qemu:///session")

        if machine:
            exists, _, _ = machine.exists_in_libvirt(libvirt_endpoint)
            if exists:
                snapshot_status = machine.create_snapshot(
                    libvirt_endpoint, snapshot_name, snapshot_description
                )
                if type(snapshot_status) == libvirt.virDomainSnapshot:
                    click.echo(
                        f"Snapshot {snapshot_status.getName()} created for {machine.vm_name}"
                    )
                else:
                    logger.error("Snapshot creation failed for %s", machine.vm_name)
            else:
                logger.warning(
                    "Machine %s is not deployed to the configured in %s.",
                    machine.vm_name,
                    libvirt_endpoint,
                )
    else:
        logger.error("Machine not found in manifest: %s", vm_name)


@snapshot.command()
@click.argument("vm_name")
@click.argument("snapshot_name")
@click.option("--force", is_flag=True, help="Skip confirmation prompt.")
def delete(vm_name, snapshot_name, force=False):
    """Delete a snapshot for a given VM."""
    try:
        environment, _, config_defaults, machines = parse_config()
    except TypeError as e:
        logger.error("Could not parse config file.")
        sys.exit(1)

    machine_config = get_machine_by_vm_name(machines, vm_name)
    if machine_config:

        machine = Machine(
            get_machine_by_vm_name(machines, vm_name), environment, config_defaults
        )
        libvirt_endpoint = environment.get("libvirt_uri", "qemu:///session")

        if machine:
            exists, _, _ = machine.exists_in_libvirt(libvirt_endpoint)
            if exists:
                if not (
                    force
                    or click.confirm(
                        f"Delete snapshot {snapshot_name} from {machine.vm_name}?"
                    )
                ):
                    click.echo(f"Snapshot deletion aborted for {machine.vm_name}.")
                    return

                snapshot_status = machine.delete_snapshot(
                    libvirt_endpoint, snapshot_name
                )
                if snapshot_status == 0:
                    click.echo(f"Snapshot deleted for {machine.vm_name}")
                else:
                    logger.error(
                        "Snapshot deletion failed for %s: %s",
                        machine.vm_name,
                        snapshot_status,
                    )
            else:
                logger.warning(
                    "Machine %s is not deployed to the configured in %s.",
                    machine.vm_name,
                    libvirt_endpoint,
                )
    else:
        logger.error("Machine not found in manifest: %s", vm_name)


@click.command()
def status():
    """Show the status of the environment."""
    try:
        environment, images, _, machines = parse_config()
    except TypeError as e:
        logger.error("Could not parse config file.")
        sys.exit(1)

    click.echo()
    click.echo(f'LvLab Environment Name: {environment.get("name", "no-name-lvlab")}\n')
    conn = connect_to_libvirt(environment.get("libvirt_uri", None))

    # Get a list of current VMs
    current_vms = [dom.name() for dom in conn.listAllDomains()]

    click.echo("Machines Defined:\n")
    for machine in machines:
        libvirt_vm_name = (
            machine["vm_name"] + "_" + environment.get("name", "LvLabEnvironment")
        )
        if libvirt_vm_name in current_vms:
            vm = conn.lookupByName(libvirt_vm_name)
            _, _, vm_status, vm_status_reason = get_machine_state(vm.state())
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


@click.command()
@click.argument("vm_name")
def up(vm_name):
    """Start a machine defined in the Lvlab.yml manifest."""
    try:
        environment, images, config_defaults, machines = parse_config()
    except TypeError as e:
        logger.error("Could not parse config file.")
        sys.exit(1)

    machine_config = get_machine_by_vm_name(machines, vm_name)
    if machine_config:
        machine = Machine(machine_config, environment, config_defaults)

        exists, status, _ = machine.exists_in_libvirt(
            environment.get("libvirt_uri", "qemu:///session")
        )

        if exists:
            if status in ["VIR_DOMAIN_SHUTOFF", "VIR_DOMAIN_CRASHED"]:
                click.echo(f"Starting virtual machine {machine.vm_name}")
                if machine.poweron(environment.get("libvirt_uri", None)) > 0:
                    logger.error("Problem powering on VM %s", machine.vm_name)
            elif status in ["VIR_DOMAIN_RUNNING"]:
                click.echo(f"The virtual machine {machine.vm_name} is running already")

        else:
            click.echo(f"Creating virtual machine: {machine.vm_name}")

            cloud_image = CloudImage(
                machine.os, images.get(machine.os), environment, config_defaults
            )

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
            logger.info("Writing cloud-init config ISO file %s", iso.fpath)
            if iso.write(iso.fpath):
                logger.info("Writing cloud-init config ISO successful")
            else:
                logger.error("Writing cloud-init config ISO failed.")
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
                logger.error("Virtual machine installation failed.")

    else:
        logger.error("Machine %s not found in manifest.", vm_name)


# Bulid the CLI
run.add_command(cloudinit)
run.add_command(down)
run.add_command(destroy)
run.add_command(init)
run.add_command(snapshot)
run.add_command(status)
run.add_command(capabilities)
run.add_command(up)
run.add_command(hosts)

if __name__ == "__main__":
    run()
