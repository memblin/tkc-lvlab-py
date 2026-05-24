"""Typer-based ``lvlab`` CLI entry points.

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

Phase 9 ported this surface from Click to Typer. The UX contract is
preserved: ``-h``/``--help`` both work at every level, ``-v`` is a
count flag (``-v`` = INFO, ``-vv`` = DEBUG), command + option names
and defaults match the Click implementation. Typer's underlying Click
runtime keeps the test surface (``click.testing.CliRunner`` on the
``app`` Typer instance) working unchanged.

The standalone one-off workflow (``createvm`` / ``destroyvm`` console
scripts in :mod:`tkc_lvlab.scripts`) still uses Click directly — this
module does not flow through them.
"""

from __future__ import annotations

import os
import sys

import typer

from ._logging import configure_logging, get_logger
from .config import (
    parse_config,
    generate_hosts,
    generate_hosts_entries,
    parse_hosts_file,
)
from .utils.cloud_init import CloudInitIso
from .utils.libvirt import (
    get_machine_by_vm_name,
    Machine,
)
from .utils.images import CloudImage
from .utils.virsh import (
    VirshError,
    humanize_state,
    virsh_capabilities,
    virsh_domstate,
    virsh_list_all_names,
)


logger = get_logger(__name__)


app = typer.Typer(
    help="A command-line tool for managing VMs.",
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
)
snapshot_app = typer.Typer(
    help="Snapshot management commands.",
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
)
app.add_typer(snapshot_app, name="snapshot")


@app.callback()
def _root(
    verbose: int = typer.Option(
        0,
        "-v",
        "--verbose",
        count=True,
        help="Increase log verbosity (-v for INFO, -vv for DEBUG).",
    ),
    quiet: bool = typer.Option(
        False,
        "-q",
        "--quiet",
        help="Suppress informational logs (ERROR only). Overrides -v.",
    ),
) -> None:
    """Top-level callback — configures logging before any subcommand runs."""
    configure_logging(verbosity=verbose, quiet=quiet)


def _resolve_image_config(images: dict, machine_os: str, vm_name: str) -> dict:
    """Look up an image config by ``machine.os``; exit with a clear error if absent.

    Replaces a ``None`` from ``images.get(machine.os)`` — which used to
    crash later inside ``CloudImage.__init__`` with an opaque
    ``AttributeError: 'NoneType' object has no attribute 'get'`` — with
    an operator-readable message naming the missing key and listing the
    image keys actually defined in the manifest.

    Args:
        images: The ``images`` dict from ``parse_config()``.
        machine_os: The ``os`` value resolved for this machine
            (merging machine entry + ``config_defaults``).
        vm_name: The machine's ``vm_name``, used in the error message
            so the operator knows which manifest entry is wrong.

    Returns:
        The image config dict from ``images[machine_os]``.

    Raises:
        typer.Exit: With code 1 if ``machine_os`` is not a key in
            ``images``.
    """
    image_config = images.get(machine_os)
    if image_config is None:
        available = ", ".join(sorted(images.keys())) or "(no images defined)"
        logger.error(
            "Machine %r has os=%r, which is not defined in the manifest's "
            "images section. Available image keys: %s",
            vm_name,
            machine_os,
            available,
        )
        raise typer.Exit(code=1)
    return image_config


@app.command()
def capabilities() -> None:
    """Print the raw hypervisor capabilities XML for qemu:///session."""
    try:
        caps = virsh_capabilities("qemu:///session")
    except VirshError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo("Capabilities:\n" + caps)


@app.command()
def cloudinit(vm_name: str) -> None:
    """Render cloud-init files for a manifest VM without starting it."""
    try:
        environment, images, config_defaults, machines = parse_config()
    except TypeError:
        logger.error("Could not parse config file.")
        raise typer.Exit(code=1)

    machine = Machine(
        get_machine_by_vm_name(machines, vm_name), environment, config_defaults
    )

    if machine:
        image_config = _resolve_image_config(images, machine.os, machine.vm_name)
        cloud_image = CloudImage(machine.os, image_config, environment, config_defaults)
        # Render and write cloud-init config
        _, _, _ = machine.cloud_init(cloud_image, config_defaults)


@app.command()
def destroy(
    vm_name: str,
    force: bool = typer.Option(
        False, "--force", help="Force destruction without confirmation."
    ),
) -> None:
    """Destroy a manifest VM: force-off, undefine, remove files."""
    try:
        environment, _, config_defaults, machines = parse_config()
    except TypeError:
        logger.error("Could not parse config file.")
        raise typer.Exit(code=1)

    machine_config = get_machine_by_vm_name(machines, vm_name)
    if machine_config:

        machine = Machine(
            get_machine_by_vm_name(machines, vm_name), environment, config_defaults
        )
        libvirt_endpoint = environment.get("libvirt_uri", "qemu:///session")

        if machine:
            exists, _, _ = machine.exists_in_libvirt(libvirt_endpoint)
            if exists:
                if force or typer.confirm(
                    f"Are you sure you want to destroy {machine.vm_name}?"
                ):
                    if machine.destroy(libvirt_endpoint):
                        typer.echo(
                            f"Destruction appears successful for {machine.vm_name}."
                        )
                    else:
                        logger.error(
                            "Destruction appears to have failed for %s.",
                            machine.vm_name,
                        )

                else:
                    typer.echo(f"Destruction aborted for {machine.vm_name}.")
            else:
                logger.warning(
                    "Machine %s is not deployed to the configured in %s.",
                    machine.vm_name,
                    libvirt_endpoint,
                )
    else:
        logger.error("Machine not found in manifest: %s", vm_name)


@app.command()
def down(vm_name: str) -> None:
    """Gracefully shut down a manifest VM."""
    try:
        environment, _, config_defaults, machines = parse_config()
    except TypeError:
        logger.error("Could not parse config file.")
        raise typer.Exit(code=1)

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
                typer.echo(f"Shutting down virtual machine {vm_name}.")
                if (
                    machine.shutdown(environment.get("libvirt_uri", "qemu:///session"))
                    > 0
                ):
                    logger.error("Shutdown appears to have failed.")
                else:
                    typer.echo(
                        "Shutdown appears successful. The virtual machine may take a short time to complete shutdown."
                    )
            elif state in ["VIR_DOMAIN_SHUTOFF"]:
                typer.echo(
                    f"The virtual machine {machine.vm_name} is shutdown already."
                )

    else:
        logger.error("Machine %s not found in manifest.", vm_name)


@app.command()
def hosts(
    append: bool = typer.Option(
        False, "--append", help="Attempt to append hosts snippet to /etc/hosts."
    ),
    heredoc: bool = typer.Option(
        False,
        "--heredoc",
        help="Render hosts snippet as a heredoc to append to /etc/hosts.",
    ),
) -> None:
    """Render a /etc/hosts snippet for the manifest's machines.

    Default mode prints the snippet to stdout. --append writes new
    entries directly into /etc/hosts (skipping conflicts); typically
    needs `sudo $(which lvlab) hosts --append`. --heredoc wraps the
    output in a `cat <<EOF` heredoc.
    """
    try:
        environment, _, config_defaults, machines = parse_config()
    except TypeError:
        logger.error("Could not parse config file.")
        sys.exit()

    hosts_snippet = generate_hosts(environment, config_defaults, machines)

    if append:
        etc_hosts = "/etc/hosts"
        try:
            existing_ips, existing_names = parse_hosts_file(etc_hosts)
        except OSError as e:
            logger.error("Unable to read %s: %s", etc_hosts, e)
            raise typer.Exit(code=1)

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
                typer.echo(
                    f"Skipping {entry['ip4']} {entry['fqdn']} {entry['hostname']}: "
                    + "; ".join(conflict_reasons)
                )
            else:
                to_append.append(entry)

        if not to_append:
            typer.echo("No new entries to append to /etc/hosts.")
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
                        typer.echo(f"Appended: {line.rstrip()}")
            except OSError as e:
                logger.error("Unable to write %s: %s", etc_hosts, e)
                raise typer.Exit(code=1)
        else:
            logger.error("No write access available for /etc/hosts")

    if heredoc:
        hosts_snippet = generate_hosts(
            environment, config_defaults, machines, heredoc="/etc/hosts"
        )

    typer.echo(f"{hosts_snippet}")


@app.command("ssh-config")
def ssh_config(vm_name: str = typer.Argument(None)) -> None:
    """Print ~/.ssh/config snippet(s) for machines in the manifest.

    With no VM_NAME, a snippet is emitted for every machine. With a
    VM_NAME, only that machine's snippet is emitted. Output goes to
    stdout; redirect or append it to ~/.ssh/config yourself.
    """
    try:
        environment, _, config_defaults, machines = parse_config()
    except TypeError:
        typer.echo("Could not parse config file.")
        raise typer.Exit(code=1)

    if vm_name:
        machine_config = get_machine_by_vm_name(machines, vm_name)
        if not machine_config:
            typer.echo(f"Machine {vm_name} not found in manifest.")
            raise typer.Exit(code=1)
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

    typer.echo("\n\n".join(snippets))


@app.command()
def init() -> None:
    """Initialize the environment: download and verify cloud images."""
    try:
        environment, images, config_defaults, _ = parse_config()
    except TypeError:
        logger.error("Could not parse config file.")
        sys.exit()

    typer.echo()
    typer.echo(f'Initializing Libvirt Lab Environment: {environment["name"]}\n')

    # TODO: Would this classify well into an environment object with
    # a list of CloudImages?
    for image_name, image_config in images.items():
        image = CloudImage(image_name, image_config, environment, config_defaults)

        if image.exists_locally("image"):
            typer.echo(f"CloudImage {image.name} exists locally: {image.image_fpath}")
        else:
            logger.info("Attempting to download image: %s", image.image_url)
            if image.download_image():
                typer.echo(f"CloudImage downloaded to {image.image_fpath}")
            else:
                logger.error("CloudImage download failed")

        if image.checksum_url_gpg:
            if image.exists_locally(file_type=("checksum_gpg")):
                typer.echo(
                    f"CloudImage {image.name} checksum GPG file exists locally: {image.checksum_gpg_fpath}"
                )
            else:
                if image.download_checksum_gpg():
                    typer.echo(
                        f"CloudImage checksum GPG file downloaded to {image.checksum_gpg_fpath}"
                    )
                else:
                    logger.error("CloudImage checksum GPG file download failed")

        if image.checksum_url:
            if image.exists_locally(file_type="checksum"):
                typer.echo(
                    f"CloudImage {image.name} checksum file exists locally: {image.checksum_fpath}"
                )
            else:
                logger.info(
                    "Attempting to download checksum file URL: %s", image.checksum_url
                )
                if image.download_checksum():
                    typer.echo(
                        f"CloudImage {image.name} checksum file downloaded to {image.checksum_fpath}"
                    )
                else:
                    logger.error(
                        "CloudImage %s checksum file download failed", image.name
                    )

        if image.checksum_url_gpg and image.exists_locally(file_type=("checksum_gpg")):
            if image.gpg_verify_checksum_file():
                typer.echo(f"CloudImage {image.name} checksum file GPG validation OK")
            else:
                logger.error(
                    "CloudImage %s checksum file GPG validation BAD", image.name
                )

        if image.checksum_url and image.exists_locally(file_type=("checksum")):
            if image.checksum_verify_image():
                typer.echo(f"CloudImage {image.name} checksum verification OK")
            else:
                logger.error("CloudImage %s checksum verification BAD", image.name)

        typer.echo()


@snapshot_app.command("list")
def snapshot_list(vm_name: str) -> None:
    """List snapshots for a given VM."""
    try:
        environment, _, config_defaults, machines = parse_config()
    except TypeError:
        logger.error("Could not parse config file.")
        raise typer.Exit(code=1)

    machine_config = get_machine_by_vm_name(machines, vm_name)
    if machine_config:

        machine = Machine(
            get_machine_by_vm_name(machines, vm_name), environment, config_defaults
        )
        libvirt_endpoint = environment.get("libvirt_uri", "qemu:///session")

        if machine:
            exists, _, _ = machine.exists_in_libvirt(libvirt_endpoint)
            if exists:
                typer.echo(f"Listing snapshots for {machine.vm_name}")
                snapshots = machine.list_snapshots(libvirt_endpoint)
                if snapshots:
                    for snap in snapshots:
                        typer.echo(f"  - {snap}")
                else:
                    typer.echo(f"No snapshots found for {machine.vm_name}")
            else:
                logger.warning(
                    "Machine %s is not deployed to the configured in %s.",
                    machine.vm_name,
                    libvirt_endpoint,
                )
    else:
        logger.error("Machine not found in manifest: %s", vm_name)


@snapshot_app.command("create")
def snapshot_create(
    vm_name: str,
    snapshot_name: str,
    snapshot_description: str = typer.Argument(None),
) -> None:
    """Create a snapshot for a given VM."""
    try:
        environment, _, config_defaults, machines = parse_config()
    except TypeError:
        logger.error("Could not parse config file.")
        raise typer.Exit(code=1)

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
                    typer.echo(
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


@snapshot_app.command("delete")
def snapshot_delete(
    vm_name: str,
    snapshot_name: str,
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompt."),
) -> None:
    """Delete a snapshot for a given VM."""
    try:
        environment, _, config_defaults, machines = parse_config()
    except TypeError:
        logger.error("Could not parse config file.")
        raise typer.Exit(code=1)

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
                    or typer.confirm(
                        f"Delete snapshot {snapshot_name} from {machine.vm_name}?"
                    )
                ):
                    typer.echo(f"Snapshot deletion aborted for {machine.vm_name}.")
                    return

                try:
                    machine.delete_snapshot(libvirt_endpoint, snapshot_name)
                    typer.echo(
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


@app.command()
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
        raise typer.Exit(code=1)

    uri = environment.get("libvirt_uri", "qemu:///session")
    env_name = environment.get("name", "no-name-lvlab")

    typer.echo()
    typer.echo(f"LvLab Environment Name: {env_name}\n")

    try:
        current_vms = virsh_list_all_names(uri)
    except VirshError as exc:
        logger.error("Failed to list domains at %s: %s", uri, exc)
        raise typer.Exit(code=1)

    typer.echo("Machines Defined:\n")
    for machine in machines:
        libvirt_vm_name = f"{machine['vm_name']}_{env_name}"
        if libvirt_vm_name in current_vms:
            try:
                state = virsh_domstate(uri, libvirt_vm_name)
            except VirshError as exc:
                logger.error("Failed to query state for %s: %s", libvirt_vm_name, exc)
                typer.echo(f"  - {machine['vm_name']} is unknown (virsh error)")
                continue
            human_state, _ = humanize_state(state, "")
            typer.echo(f"  - {machine['vm_name']} is {human_state}")
        else:
            typer.echo(f"  - {machine['vm_name']} is undeployed")

    typer.echo()
    typer.echo("Images Used:\n")
    for image_name, image_data in images.items():
        typer.echo(
            f"  - {image_name} from {image_data.get('image_url', 'Missing Image URL.')}"
        )

    typer.echo()


@app.command()
def up(vm_name: str) -> None:
    """Start a machine defined in the Lvlab.yml manifest.

    Creates the VM on first run (qcow2 disks -> cloud-init render ->
    ISO pack -> virt-install) or powers it on if it's shut off.
    Already-running VMs are a no-op.
    """
    try:
        environment, images, config_defaults, machines = parse_config()
    except TypeError:
        logger.error("Could not parse config file.")
        raise typer.Exit(code=1)

    machine_config = get_machine_by_vm_name(machines, vm_name)
    if machine_config:
        machine = Machine(machine_config, environment, config_defaults)

        exists, status_state, _ = machine.exists_in_libvirt(
            environment.get("libvirt_uri", "qemu:///session")
        )

        if exists:
            if status_state in {"shut off", "crashed"}:
                typer.echo(f"Starting virtual machine {machine.vm_name}")
                if machine.poweron(environment.get("libvirt_uri", None)) > 0:
                    logger.error("Problem powering on VM %s", machine.vm_name)
            elif status_state == "running":
                typer.echo(f"The virtual machine {machine.vm_name} is running already")

        else:
            typer.echo(f"Creating virtual machine: {machine.vm_name}")

            image_config = _resolve_image_config(images, machine.os, machine.vm_name)
            cloud_image = CloudImage(
                machine.os, image_config, environment, config_defaults
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
                raise typer.Exit(code=1)

            # virt-install the VM and check status
            typer.echo(f"Attempting to start virtual maching: {machine.vm_name}")
            if machine.deploy(
                machine.config_fpath,
                config_defaults,
                environment.get("libvirt_uri", "qemu:///session"),
            ):
                typer.echo("Virtual machine deployment complete.")
            else:
                logger.error("Virtual machine installation failed.")
                raise typer.Exit(code=1)

    else:
        logger.error("Machine %s not found in manifest.", vm_name)


# Backwards-compatible aliases. ``pyproject.toml`` entry-point references
# ``run``; tests reference ``snapshot`` for the snapshot subcommand group.
# Typer instances are callable, so both aliases work the same way the original
# Click group objects did.
run = app
snapshot = snapshot_app


if __name__ == "__main__":
    app()
