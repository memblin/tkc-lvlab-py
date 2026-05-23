"""Click-based ``lvlab`` CLI entry points.

This module wires the ``lvlab`` console script (``[project.scripts] lvlab``)
to its subcommands. Each subcommand:

1. Calls :func:`tkc_lvlab.config.parse_config` to load ``Lvlab.yml``.
2. Resolves a machine by name via
   :func:`tkc_lvlab.utils.libvirt.get_machine_by_vm_name`.
3. Constructs a :class:`tkc_lvlab.utils.libvirt.Machine` from the
   resolved manifest entry + environment + config_defaults.
4. Dispatches the requested operation (start, stop, snapshot, etc.)
   against the libvirt URI from ``environment.libvirt_uri``.

The hypervisor side is invoked via :mod:`tkc_lvlab.utils.virsh` — a
``subprocess.run`` wrapper around ``virsh``. No ``libvirt-python``
C extension is imported (Phase 2 removed that dependency).

The standalone one-off workflow (``createvm`` / ``destroyvm`` console
scripts in :mod:`tkc_lvlab.scripts`) does not flow through this module
— they have their own Click entry points that talk to virsh directly.
"""

from __future__ import annotations

import os
import sys

import click

from ._logging import configure_logging, get_logger
from .config import (
    parse_config,
    generate_hosts,
    generate_hosts_entries,
    parse_hosts_file,
)
from .utils.cloud_init import CloudInitIso, MetaData, NetworkConfig, UserData
from .utils.libvirt import (
    get_machine_by_vm_name,
    Machine,
)
from .utils.vdisk import VirtualDisk
from .utils.images import CloudImage
from .utils.virsh import (
    VirshError,
    humanize_state,
    virsh_capabilities,
    virsh_domstate,
    virsh_list_all_names,
)


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
def run(verbose: int, quiet: bool) -> None:
    """A command-line tool for managing VMs."""
    configure_logging(verbosity=verbose, quiet=quiet)


@click.command()
def capabilities() -> None:
    """Print the raw hypervisor capabilities XML for qemu:///session."""
    try:
        caps = virsh_capabilities("qemu:///session")
    except VirshError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    click.echo("Capabilities:\n" + caps)


@click.command()
@click.argument("vm_name")
def cloudinit(vm_name: str) -> None:
    """Render cloud-init files for a manifest VM without starting it."""
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
def destroy(vm_name: str, force: bool = False) -> None:
    """Destroy a manifest VM: force-off, undefine, remove files."""
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
def down(vm_name: str) -> None:
    """Gracefully shut down a manifest VM."""
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
            if state in {"running", "paused"}:
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
def hosts(append: bool = False, heredoc: bool = False) -> None:
    """Render a /etc/hosts snippet for the manifest's machines.

    Default mode prints the snippet to stdout. ``--append`` writes
    new entries directly into ``/etc/hosts`` (skipping conflicts);
    typically needs ``sudo $(which lvlab) hosts --append``.
    ``--heredoc`` wraps the output in a ``cat <<EOF`` heredoc.
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


@click.command(name="ssh-config")
@click.argument("vm_name", required=False)
def ssh_config(vm_name: str | None = None) -> None:
    """Print ~/.ssh/config snippet(s) for machines in the manifest.

    With no VM_NAME, a snippet is emitted for every machine. With a
    VM_NAME, only that machine's snippet is emitted. Output goes to
    stdout; redirect or append it to ~/.ssh/config yourself.
    """
    try:
        environment, _, config_defaults, machines = parse_config()
    except TypeError as e:
        click.echo("Could not parse config file.")
        sys.exit(1)

    if vm_name:
        machine_config = get_machine_by_vm_name(machines, vm_name)
        if not machine_config:
            click.echo(f"Machine {vm_name} not found in manifest.")
            sys.exit(1)
        selected_machines = [machine_config]
    else:
        selected_machines = machines

    cloud_init_defaults = config_defaults.get("cloud_init", {})

    snippets = []
    for machine in selected_machines:
        machine_vm_name = machine.get("vm_name")

        # Merge cloud_init defaults with per-machine overrides for user/pubkey lookup.
        machine_cloud_init = {**cloud_init_defaults, **machine.get("cloud_init", {})}
        user = machine_cloud_init.get("user")
        pubkey = machine_cloud_init.get("pubkey")

        # Pull the primary interface IP (first interface), stripping any CIDR suffix.
        interfaces = machine.get("interfaces", []) or []
        host_ip = None
        if interfaces and interfaces[0].get("ip4"):
            host_ip = interfaces[0]["ip4"].split("/")[0]

        lines = [f"Host {machine_vm_name}"]
        if host_ip:
            lines.append(f"  HostName {host_ip}")
        else:
            lines.append(
                "  # HostName not resolvable from manifest (no static ip4; VM may use DHCP or not be up yet)"
            )
        if user:
            lines.append(f"  User {user}")

        # Reuse UserData.__post_init__'s heuristic: if pubkey contains "~" or "/",
        # treat it as a path on disk and derive the private key by stripping ".pub".
        if pubkey and ("~" in pubkey or "/" in pubkey):
            identity_file = pubkey[:-4] if pubkey.endswith(".pub") else pubkey
            lines.append(f"  IdentityFile {identity_file}")

        snippets.append("\n".join(lines))

    click.echo("\n\n".join(snippets))


@click.command()
def init() -> None:
    """Initialize the environment: download and verify cloud images."""
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
def snapshot() -> None:
    """Snapshot management commands."""


@snapshot.command()
@click.argument("vm_name")
def list(vm_name: str) -> None:  # pylint: disable=redefined-builtin
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
                        click.echo(f"  - {snapshot}")
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
def create(
    vm_name: str,
    snapshot_name: str,
    snapshot_description: str | None = None,
) -> None:
    """Create a snapshot for a given VM."""
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
                try:
                    machine.create_snapshot(
                        libvirt_endpoint, snapshot_name, snapshot_description
                    )
                    click.echo(
                        f"Snapshot {snapshot_name} created for {machine.vm_name}"
                    )
                except VirshError as e:
                    logger.error(
                        "Failed to create snapshot %s for %s: %s",
                        snapshot_name,
                        machine.vm_name,
                        e,
                    )
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
def delete(vm_name: str, snapshot_name: str, force: bool = False) -> None:
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

                try:
                    machine.delete_snapshot(libvirt_endpoint, snapshot_name)
                    click.echo(
                        f"Snapshot {snapshot_name} deleted from {machine.vm_name}"
                    )
                except VirshError as e:
                    logger.error(
                        "Failed to delete snapshot %s from %s: %s",
                        snapshot_name,
                        machine.vm_name,
                        e,
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
def status() -> None:
    """Show the status of the environment.

    Lists the configured environment, every machine in the manifest with
    its current libvirt state, and the cloud images referenced by the
    manifest. Machines that are not present on the hypervisor are
    reported as ``undeployed``.

    Phase 2 note: this command now uses ``virsh`` exclusively. The
    parenthesized state-reason suffix that previous releases printed
    (e.g. ``the machine is running (normal startup from boot)``) has
    been dropped to avoid an N+1 ``virsh domstate --reason`` call per
    machine.
    """
    try:
        environment, images, _, machines = parse_config()
    except TypeError:
        logger.error("Could not parse config file.")
        sys.exit(1)

    uri = environment.get("libvirt_uri", "qemu:///session")
    env_name = environment.get("name", "no-name-lvlab")

    click.echo()
    click.echo(f"LvLab Environment Name: {env_name}\n")

    try:
        current_vms = virsh_list_all_names(uri)
    except VirshError as exc:
        logger.error("Failed to list domains at %s: %s", uri, exc)
        sys.exit(1)

    click.echo("Machines Defined:\n")
    for machine in machines:
        libvirt_vm_name = f"{machine['vm_name']}_{env_name}"
        if libvirt_vm_name in current_vms:
            try:
                state = virsh_domstate(uri, libvirt_vm_name)
            except VirshError as exc:
                logger.error("Failed to query state for %s: %s", libvirt_vm_name, exc)
                click.echo(f"  - {machine['vm_name']} is unknown (virsh error)")
                continue
            human_state, _ = humanize_state(state, "")
            click.echo(f"  - {machine['vm_name']} is {human_state}")
        else:
            click.echo(f"  - {machine['vm_name']} is undeployed")

    click.echo()
    click.echo("Images Used:\n")
    for image_name, image_data in images.items():
        click.echo(
            f"  - {image_name} from {image_data.get('image_url', 'Missing Image URL.')}"
        )

    click.echo()


@click.command()
@click.argument("vm_name")
def up(vm_name: str) -> None:
    """Start a machine defined in the Lvlab.yml manifest.

    Creates the VM on first run (qcow2 disks → cloud-init render → ISO
    pack → ``virt-install``) or powers it on if it's shut off. Already-
    running VMs are a no-op.
    """
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
            if status in {"shut off", "crashed"}:
                click.echo(f"Starting virtual machine {machine.vm_name}")
                if machine.poweron(environment.get("libvirt_uri", None)) > 0:
                    logger.error("Problem powering on VM %s", machine.vm_name)
            elif status == "running":
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
run.add_command(ssh_config)

if __name__ == "__main__":
    run()
