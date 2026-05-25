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

The standalone one-off workflow (``createvm`` / ``deletevm`` console
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
    DEAD_STATES,
    DOMSTATE_RUNNING,
    VirshError,
    humanize_state,
    virsh_capabilities,
    virsh_domstate,
    virsh_list_all_names,
)


logger = get_logger(__name__)


DEFAULT_LIBVIRT_URI = "qemu:///session"
CONFIG_PARSE_ERROR_MSG = "Could not parse config file."
MACHINE_NOT_DEPLOYED_MSG = "Machine %s is not deployed to the configured in %s."
MACHINE_NOT_IN_MANIFEST_MSG = "Machine not found in manifest: %s"


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


def _resolve_existing_machine(vm_name: str) -> tuple[Machine | None, str | None]:
    """Resolve a manifest entry into a :class:`Machine` that exists in libvirt.

    Shared boilerplate for the commands that operate on an already-defined
    domain (``destroy``, ``snapshot list``, ``snapshot delete``, etc.). All
    failure paths log the same way the inline-bodied commands did:

    - ``parse_config()`` failing → ``logger.error`` then ``typer.Exit(1)``.
    - ``vm_name`` not in the manifest → ``logger.error`` with the
      ``MACHINE_NOT_IN_MANIFEST_MSG`` template, returns ``(None, None)``.
    - Machine resolved but not present at the libvirt URI →
      ``logger.warning`` with the ``MACHINE_NOT_DEPLOYED_MSG`` template,
      returns ``(None, None)``.

    Args:
        vm_name: The ``vm_name`` from the user-supplied CLI argument.

    Returns:
        ``(machine, libvirt_uri)`` on success. ``(None, None)`` on any
        non-fatal failure (caller should just return early).

    Raises:
        typer.Exit: With code 1 if ``parse_config()`` raises ``TypeError``.
            Matches the long-standing parse-failure behaviour.
    """
    try:
        environment, _, config_defaults, machines = parse_config()
    except TypeError:
        logger.error(CONFIG_PARSE_ERROR_MSG)
        raise typer.Exit(code=1)

    machine_config = get_machine_by_vm_name(machines, vm_name)
    if not machine_config:
        logger.error(MACHINE_NOT_IN_MANIFEST_MSG, vm_name)
        return None, None

    machine = Machine(machine_config, environment, config_defaults)
    libvirt_uri = environment.get("libvirt_uri", DEFAULT_LIBVIRT_URI)
    exists, _, _ = machine.exists_in_libvirt(libvirt_uri)
    if not exists:
        logger.warning(MACHINE_NOT_DEPLOYED_MSG, machine.vm_name, libvirt_uri)
        return None, None
    return machine, libvirt_uri


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
        caps = virsh_capabilities(DEFAULT_LIBVIRT_URI)
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
        logger.error(CONFIG_PARSE_ERROR_MSG)
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
    machine, libvirt_uri = _resolve_existing_machine(vm_name)
    if machine is None:
        return

    if not (
        force or typer.confirm(f"Are you sure you want to destroy {machine.vm_name}?")
    ):
        typer.echo(f"Destruction aborted for {machine.vm_name}.")
        return

    if machine.destroy(libvirt_uri):
        typer.echo(f"Destruction appears successful for {machine.vm_name}.")
    else:
        logger.error("Destruction appears to have failed for %s.", machine.vm_name)


@app.command()
def down(vm_name: str) -> None:
    """Gracefully shut down a manifest VM."""
    try:
        environment, _, config_defaults, machines = parse_config()
    except TypeError:
        logger.error(CONFIG_PARSE_ERROR_MSG)
        raise typer.Exit(code=1)

    machine_config = get_machine_by_vm_name(machines, vm_name)
    if machine_config:

        machine = Machine(
            get_machine_by_vm_name(machines, vm_name), environment, config_defaults
        )

        exists, state, _ = machine.exists_in_libvirt(
            environment.get("libvirt_uri", DEFAULT_LIBVIRT_URI)
        )

        if exists:
            if state in {"running", "paused"}:
                typer.echo(f"Shutting down virtual machine {vm_name}.")
                if (
                    machine.shutdown(
                        environment.get("libvirt_uri", DEFAULT_LIBVIRT_URI)
                    )
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


def _hosts_classify_entries(
    candidates: list[dict],
    existing_ips: set[str],
    existing_names: set[str],
) -> tuple[list[dict], list[str]]:
    """Split candidate host entries into appendable + skip-message lists."""
    to_append: list[dict] = []
    skips: list[str] = []
    for entry in candidates:
        reasons: list[str] = []
        if entry["ip4"] in existing_ips:
            reasons.append(f"IP {entry['ip4']} already present")
        if entry["hostname"] and entry["hostname"].lower() in existing_names:
            reasons.append(f"hostname {entry['hostname']} already present")
        if entry["fqdn"] and entry["fqdn"].lower() in existing_names:
            reasons.append(f"fqdn {entry['fqdn']} already present")
        if reasons:
            skips.append(
                f"Skipping {entry['ip4']} {entry['fqdn']} {entry['hostname']}: "
                + "; ".join(reasons)
            )
        else:
            to_append.append(entry)
    return to_append, skips


def _hosts_etc_writable(etc_hosts: str) -> bool:
    """True iff the process can write to (or create) ``etc_hosts``."""
    if os.access(etc_hosts, os.W_OK):
        return True
    parent = os.path.dirname(etc_hosts) or "."
    return not os.path.exists(etc_hosts) and os.access(parent, os.W_OK)


def _hosts_write_entries(
    etc_hosts: str, environment: dict, to_append: list[dict]
) -> None:
    """Append a labelled header + entries to ``etc_hosts``; echo each line."""
    logger.info("Appending hosts file snippet to %s", etc_hosts)
    header = (
        "\n# Libvirt Labs /etc/hosts snippet\n"
        f"# Environment: {environment.get('name', '')}\n"
    )
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


def _hosts_run_append(environment: dict, config_defaults: dict, machines: list) -> None:
    """Compute, report, and apply the ``--append`` branch of ``lvlab hosts``."""
    etc_hosts = "/etc/hosts"
    try:
        existing_ips, existing_names = parse_hosts_file(etc_hosts)
    except OSError as e:
        logger.error("Unable to read %s: %s", etc_hosts, e)
        raise typer.Exit(code=1)

    candidates = generate_hosts_entries(config_defaults, machines)
    to_append, skips = _hosts_classify_entries(candidates, existing_ips, existing_names)
    for skip_msg in skips:
        typer.echo(skip_msg)

    if not to_append:
        typer.echo("No new entries to append to /etc/hosts.")
    elif _hosts_etc_writable(etc_hosts):
        _hosts_write_entries(etc_hosts, environment, to_append)
    else:
        logger.error("No write access available for /etc/hosts")


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
        logger.error(CONFIG_PARSE_ERROR_MSG)
        sys.exit()

    if append:
        _hosts_run_append(environment, config_defaults, machines)

    hosts_snippet = generate_hosts(
        environment,
        config_defaults,
        machines,
        heredoc="/etc/hosts" if heredoc else None,
    )
    typer.echo(f"{hosts_snippet}")


def _ssh_config_select_machines(
    machines: list[dict], vm_name: str | None
) -> list[dict]:
    """Resolve the machine subset to render: all, or just one by name."""
    if not vm_name:
        return machines
    machine_config = get_machine_by_vm_name(machines, vm_name)
    if not machine_config:
        typer.echo(f"Machine {vm_name} not found in manifest.")
        raise typer.Exit(code=1)
    return [machine_config]


def _ssh_config_primary_ip(machine: dict) -> str | None:
    """Return the first interface's ``ip4`` with any CIDR suffix stripped."""
    interfaces = machine.get("interfaces", []) or []
    if interfaces and interfaces[0].get("ip4"):
        return interfaces[0]["ip4"].split("/")[0]
    return None


def _ssh_config_identity_file(pubkey: str | None) -> str | None:
    """If ``pubkey`` looks like a path, return the matching private key path.

    Reuses :class:`tkc_lvlab.utils.cloud_init.UserData`'s heuristic:
    a pubkey containing ``~`` or ``/`` is treated as a filesystem path
    and the private key path is derived by stripping a ``.pub`` suffix.
    Literal SSH key strings return ``None``.
    """
    if not pubkey or ("~" not in pubkey and "/" not in pubkey):
        return None
    return pubkey[:-4] if pubkey.endswith(".pub") else pubkey


def _ssh_config_render_machine(machine: dict, cloud_init_defaults: dict) -> str:
    """Render the ``~/.ssh/config`` snippet for one manifest machine."""
    machine_cloud_init = {**cloud_init_defaults, **machine.get("cloud_init", {})}
    user = machine_cloud_init.get("user")
    identity_file = _ssh_config_identity_file(machine_cloud_init.get("pubkey"))
    host_ip = _ssh_config_primary_ip(machine)

    lines = [f"Host {machine.get('vm_name')}"]
    if host_ip:
        lines.append(f"  HostName {host_ip}")
    else:
        lines.append(
            "  # HostName not resolvable from manifest "
            "(no static ip4; VM may use DHCP or not be up yet)"
        )
    if user:
        lines.append(f"  User {user}")
    if identity_file:
        lines.append(f"  IdentityFile {identity_file}")
    return "\n".join(lines)


@app.command("ssh-config")
def ssh_config(vm_name: str = typer.Argument(None)) -> None:
    """Print ~/.ssh/config snippet(s) for machines in the manifest.

    With no VM_NAME, a snippet is emitted for every machine. With a
    VM_NAME, only that machine's snippet is emitted. Output goes to
    stdout; redirect or append it to ~/.ssh/config yourself.
    """
    try:
        _, _, config_defaults, machines = parse_config()
    except TypeError:
        typer.echo(CONFIG_PARSE_ERROR_MSG)
        raise typer.Exit(code=1)

    selected_machines = _ssh_config_select_machines(machines, vm_name)
    cloud_init_defaults = config_defaults.get("cloud_init", {})

    snippets = [
        _ssh_config_render_machine(machine, cloud_init_defaults)
        for machine in selected_machines
    ]
    typer.echo("\n\n".join(snippets))


def _init_ensure_image(image: CloudImage) -> None:
    """Ensure the cloud image qcow2 is on disk; emit status."""
    if image.exists_locally("image"):
        typer.echo(f"CloudImage {image.name} exists locally: {image.image_fpath}")
        return
    logger.info("Attempting to download image: %s", image.image_url)
    if image.download_image():
        typer.echo(f"CloudImage downloaded to {image.image_fpath}")
    else:
        logger.error("CloudImage download failed")


def _init_ensure_checksum_gpg(image: CloudImage) -> None:
    """Ensure the checksum-file GPG keyring is on disk; emit status."""
    if image.exists_locally(file_type="checksum_gpg"):
        typer.echo(
            f"CloudImage {image.name} checksum GPG file exists locally: "
            f"{image.checksum_gpg_fpath}"
        )
        return
    if image.download_checksum_gpg():
        typer.echo(
            f"CloudImage checksum GPG file downloaded to {image.checksum_gpg_fpath}"
        )
    else:
        logger.error("CloudImage checksum GPG file download failed")


def _init_ensure_checksum(image: CloudImage) -> None:
    """Ensure the checksum manifest is on disk; emit status."""
    if image.exists_locally(file_type="checksum"):
        typer.echo(
            f"CloudImage {image.name} checksum file exists locally: "
            f"{image.checksum_fpath}"
        )
        return
    logger.info("Attempting to download checksum file URL: %s", image.checksum_url)
    if image.download_checksum():
        typer.echo(
            f"CloudImage {image.name} checksum file downloaded to "
            f"{image.checksum_fpath}"
        )
    else:
        logger.error("CloudImage %s checksum file download failed", image.name)


def _init_verify_gpg(image: CloudImage) -> None:
    """Run GPG verification on the checksum file; emit OK/BAD status."""
    if image.gpg_verify_checksum_file():
        typer.echo(f"CloudImage {image.name} checksum file GPG validation OK")
    else:
        logger.error("CloudImage %s checksum file GPG validation BAD", image.name)


def _init_verify_checksum(image: CloudImage) -> None:
    """Hash-verify the image against the manifest; emit OK/BAD status."""
    if image.checksum_verify_image():
        typer.echo(f"CloudImage {image.name} checksum verification OK")
    else:
        logger.error("CloudImage %s checksum verification BAD", image.name)


def _init_process_image(image: CloudImage) -> None:
    """Download and verify one CloudImage's artefacts in order."""
    _init_ensure_image(image)
    if image.checksum_url_gpg:
        _init_ensure_checksum_gpg(image)
    if image.checksum_url:
        _init_ensure_checksum(image)
    if image.checksum_url_gpg and image.exists_locally(file_type="checksum_gpg"):
        _init_verify_gpg(image)
    if image.checksum_url and image.exists_locally(file_type="checksum"):
        _init_verify_checksum(image)


@app.command()
def init() -> None:
    """Initialize the environment: download and verify cloud images."""
    try:
        environment, images, config_defaults, _ = parse_config()
    except TypeError:
        logger.error(CONFIG_PARSE_ERROR_MSG)
        sys.exit()

    typer.echo()
    typer.echo(f'Initializing Libvirt Lab Environment: {environment["name"]}\n')

    # TODO: Would this classify well into an environment object with
    # a list of CloudImages?
    for image_name, image_config in images.items():
        image = CloudImage(image_name, image_config, environment, config_defaults)
        _init_process_image(image)
        typer.echo()


@snapshot_app.command("list")
def snapshot_list(vm_name: str) -> None:
    """List snapshots for a given VM."""
    machine, libvirt_uri = _resolve_existing_machine(vm_name)
    if machine is None:
        return

    typer.echo(f"Listing snapshots for {machine.vm_name}")
    snapshots = machine.list_snapshots(libvirt_uri)
    if snapshots:
        for snap in snapshots:
            typer.echo(f"  - {snap}")
    else:
        typer.echo(f"No snapshots found for {machine.vm_name}")


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
        logger.error(CONFIG_PARSE_ERROR_MSG)
        raise typer.Exit(code=1)

    machine_config = get_machine_by_vm_name(machines, vm_name)
    if machine_config:

        machine = Machine(
            get_machine_by_vm_name(machines, vm_name), environment, config_defaults
        )
        libvirt_endpoint = environment.get("libvirt_uri", DEFAULT_LIBVIRT_URI)

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
                    MACHINE_NOT_DEPLOYED_MSG,
                    machine.vm_name,
                    libvirt_endpoint,
                )
    else:
        logger.error(MACHINE_NOT_IN_MANIFEST_MSG, vm_name)


@snapshot_app.command("delete")
def snapshot_delete(
    vm_name: str,
    snapshot_name: str,
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompt."),
) -> None:
    """Delete a snapshot for a given VM."""
    machine, libvirt_uri = _resolve_existing_machine(vm_name)
    if machine is None:
        return

    if not (
        force
        or typer.confirm(f"Delete snapshot {snapshot_name} from {machine.vm_name}?")
    ):
        typer.echo(f"Snapshot deletion aborted for {machine.vm_name}.")
        return

    try:
        machine.delete_snapshot(libvirt_uri, snapshot_name)
        typer.echo(f"Snapshot {snapshot_name} deleted from {machine.vm_name}")
    except VirshError as e:
        logger.error(
            "Failed to delete snapshot %s from %s: %s",
            snapshot_name,
            machine.vm_name,
            e,
        )


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
        logger.error(CONFIG_PARSE_ERROR_MSG)
        raise typer.Exit(code=1)

    uri = environment.get("libvirt_uri", DEFAULT_LIBVIRT_URI)
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


def _up_start_existing(
    machine: Machine, status_state: str | None, environment: dict
) -> None:
    """Power on or no-op a machine that already exists in libvirt."""
    if status_state in DEAD_STATES:
        typer.echo(f"Starting virtual machine {machine.vm_name}")
        # Preserve the original ``None`` fallback used by the powering path
        # (the existence check above uses DEFAULT_LIBVIRT_URI; keeping the
        # asymmetry behaviour-preserving for this refactor).
        if machine.poweron(environment.get("libvirt_uri", None)) > 0:
            logger.error("Problem powering on VM %s", machine.vm_name)
    elif status_state == DOMSTATE_RUNNING:
        typer.echo(f"The virtual machine {machine.vm_name} is running already")


def _up_build_cloud_init_iso(
    machine: Machine, cloud_image: CloudImage, config_defaults: dict
) -> None:
    """Render cloud-init files, pack them into cidata.iso, exit on failure."""
    metadata_config_fpath, userdata_config_fpath, network_config_fpath = (
        machine.cloud_init(cloud_image, config_defaults)
    )
    iso = CloudInitIso(
        metadata_config_fpath,
        userdata_config_fpath,
        network_config_fpath,
        os.path.join(machine.config_fpath, "cidata.iso"),
    )
    logger.info("Writing cloud-init config ISO file %s", iso.fpath)
    if iso.write():
        logger.info("Writing cloud-init config ISO successful")
    else:
        logger.error("Writing cloud-init config ISO failed.")
        raise typer.Exit(code=1)


def _up_first_time_create(
    machine: Machine, environment: dict, images: dict, config_defaults: dict
) -> None:
    """First-time create: vdisks → cloud-init ISO → virt-install."""
    typer.echo(f"Creating virtual machine: {machine.vm_name}")

    image_config = _resolve_image_config(images, machine.os, machine.vm_name)
    cloud_image = CloudImage(machine.os, image_config, environment, config_defaults)

    machine.create_vdisks(environment, config_defaults, cloud_image)
    _up_build_cloud_init_iso(machine, cloud_image, config_defaults)

    typer.echo(f"Attempting to start virtual maching: {machine.vm_name}")
    if machine.deploy(
        machine.config_fpath,
        config_defaults,
        environment.get("libvirt_uri", DEFAULT_LIBVIRT_URI),
        os_variant=cloud_image.os_variant,
    ):
        typer.echo("Virtual machine deployment complete.")
    else:
        logger.error("Virtual machine installation failed.")
        raise typer.Exit(code=1)


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
        logger.error(CONFIG_PARSE_ERROR_MSG)
        raise typer.Exit(code=1)

    machine_config = get_machine_by_vm_name(machines, vm_name)
    if not machine_config:
        logger.error("Machine %s not found in manifest.", vm_name)
        return

    machine = Machine(machine_config, environment, config_defaults)
    libvirt_uri = environment.get("libvirt_uri", DEFAULT_LIBVIRT_URI)
    exists, status_state, _ = machine.exists_in_libvirt(libvirt_uri)

    if exists:
        _up_start_existing(machine, status_state, environment)
    else:
        _up_first_time_create(machine, environment, images, config_defaults)


# Backwards-compatible aliases. ``pyproject.toml`` entry-point references
# ``run``; tests reference ``snapshot`` for the snapshot subcommand group.
# Typer instances are callable, so both aliases work the same way the original
# Click group objects did.
run = app
snapshot = snapshot_app


if __name__ == "__main__":
    app()
