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

import concurrent.futures
import dataclasses
import os
import threading
from typing import Any

import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table

from . import __version__
from ._logging import configure_logging, get_logger
from .utils.catalog import BUILTIN_IMAGES, image_version, resolve_catalog
from .utils.output import (
    get_console,
    is_tty,
    render_one_time_password,
    render_ssh_hint,
    secho,
    set_no_color,
    styled_table,
)
from .utils.passwords import generate_one_time_password
from .config import (
    ConfigManager,
    NetworkDefaults,
    load_host_config,
    parse_config,
    generate_hosts,
    generate_hosts_entries,
    parse_hosts_file,
)
from .exceptions import ConfigError, LvlabError, PasswordHashError
from .smoke import OutputFormat, SmokeError, build_cases, run_smoke
from .utils.cloud_init import CloudInitIso
from .utils.libvirt import (
    get_machine_by_vm_name,
    Machine,
)
from .utils.images import (
    CleanupCandidate,
    CloudImage,
    backing_files_in_use,
    comment_referenced_files,
    enumerate_protected_files,
    find_cleanup_candidates,
    resolve_cloud_image_dir,
)
from .utils.virsh import (
    DEAD_STATES,
    DOMSTATE_RUNNING,
    DomInfo,
    VirshError,
    run_virsh,
    virsh_dominfo,
    virsh_domstate,
    virsh_list_all_names,
)

logger = get_logger(__name__)


DEFAULT_LIBVIRT_URI = "qemu:///session"
CONFIG_PARSE_ERROR_MSG = "Could not parse config file."
MACHINE_NOT_DEPLOYED_MSG = "Machine %s is not deployed to the configured in %s."
MACHINE_NOT_IN_MANIFEST_MSG = "Machine not found in manifest: %s"


# Global flags that live on the root callback (:func:`_root`) and are accepted
# in *either* position — ``lvlab --no-color smoke`` and ``lvlab smoke
# --no-color`` are equivalent (issue #133). These are the single source of
# truth :class:`GlobalFlagGroup` hoists; keep them in sync with ``_root``'s
# option names. All are boolean/count flags (no value-taking globals), which is
# what makes the lexical hoist below safe — none consume a following token.
GLOBAL_LONG_FLAGS = frozenset({"--no-color", "--verbose", "--quiet"})
GLOBAL_SHORT_FLAG_CHARS = frozenset("vq")  # -v (count), -q; stack as -vv, -vq.


def _is_global_flag(token: str) -> bool:
    """Return ``True`` if ``token`` is one of the position-independent globals.

    Matches a long flag verbatim (``--no-color``) or a short-flag cluster made
    up only of global short chars (``-v``, ``-vv``, ``-vq``). A token that
    merely *starts* with a global short char but carries other characters
    (e.g. the value ``-q-thing``, or a mixed cluster ``-vx``) is **not** a
    global — it stays with the subcommand.

    Args:
        token: A single ``argv`` token.

    Returns:
        ``True`` when the token should be hoisted to the root parser.
    """
    if token in GLOBAL_LONG_FLAGS:
        return True
    if len(token) >= 2 and token[0] == "-" and token[1] != "-":
        return all(char in GLOBAL_SHORT_FLAG_CHARS for char in token[1:])
    return False


class GlobalFlagGroup(typer.core.TyperGroup):
    """Root command group that accepts global flags in either position.

    Click parses a group's options only up to the first non-option token (the
    subcommand name); anything after is handed to the subcommand. That makes
    ``lvlab smoke --no-color`` fail even though ``lvlab --no-color smoke``
    works. This group reorders ``argv`` *before* dispatch so the root-owned
    globals (:data:`GLOBAL_LONG_FLAGS` / :data:`GLOBAL_SHORT_FLAG_CHARS`) are
    parsed by the root callback regardless of where they appear — the
    cobra/kubectl-style "flags anywhere" feel (issue #133).

    Set once on the top-level :data:`app`; nested sub-apps (``snapshot``,
    ``global show``, ``images``) need no change, because the reorder runs on
    the full ``argv`` before the group splits off the subcommand chain, so a
    global buried in ``lvlab snapshot create --no-color`` is still hoisted to
    the root. The hoist is purely lexical and only moves boolean/count globals,
    so it never steals a value token; the end-of-options ``--`` separator is
    honored (nothing past it is hoisted).
    """

    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:
        """Hoist global flags to the front of ``args`` before normal parsing.

        Args:
            ctx: The Click context for this group invocation.
            args: The raw argument tokens for the group.

        Returns:
            The remaining args after the superclass consumes the group options
            (Click's :meth:`parse_args` contract).
        """
        try:
            sep = args.index("--")
        except ValueError:
            sep = len(args)
        head, tail = args[:sep], args[sep:]
        hoisted = [token for token in head if _is_global_flag(token)]
        rest = [token for token in head if not _is_global_flag(token)]
        # Only global flags and no subcommand (e.g. ``lvlab --no-color``): there
        # is nothing to run, so behave like a bare ``lvlab`` and show the full
        # help via ``no_args_is_help`` rather than erroring "Missing command".
        # Delegating with empty args reuses Click's own help path verbatim.
        if self.no_args_is_help and hoisted and not rest and not tail:
            return super().parse_args(ctx, [])
        return super().parse_args(ctx, hoisted + rest + tail)


app = typer.Typer(
    cls=GlobalFlagGroup,
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

global_app = typer.Typer(
    help="Hypervisor-wide commands not scoped to a single Lvlab.yml machine.",
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
)
global_show_app = typer.Typer(
    help="Read-only cross-connection overviews (instances, and later networks/pools).",
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
)
global_app.add_typer(global_show_app, name="show")
app.add_typer(global_app, name="global")

images_app = typer.Typer(
    help="Cloud-image cache management commands.",
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
)
app.add_typer(images_app, name="images")


# config_defaults flag that hard-refuses cloud-image cleanup. Read the same
# way other config_defaults flags are (``.get`` with a default), so an
# operator can pin a cache against accidental deletion from the manifest.
PREVENT_CLEANUP_FLAG = "prevent_cloud_image_cleanup"


# Connections every ``lvlab global show`` enumerates unless the user narrows or
# extends the set with ``--uri``. Both common local libvirt sockets, so a
# developer sees rootful (qemu:///system) and rootless (qemu:///session) VMs in
# one table without naming either explicitly.
DEFAULT_GLOBAL_URIS: tuple[str, ...] = ("qemu:///system", "qemu:///session")


def _version_callback(value: bool) -> None:
    """Print the installed package version and exit when ``--version`` is set."""
    if value:
        typer.echo(f"lvlab {__version__}")
        raise typer.Exit()


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
    no_color: bool = typer.Option(
        False,
        "--no-color",
        help="Disable colored/styled output (also honors the NO_COLOR env var).",
    ),
    version: bool = typer.Option(  # pylint: disable=unused-argument
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the installed tkc-lvlab package version and exit.",
    ),
) -> None:
    """Top-level callback — configures logging before any subcommand runs."""
    configure_logging(verbosity=verbose, quiet=quiet)
    if no_color:
        set_no_color(True)
        # Belt-and-suspenders: export NO_COLOR so any child process and Rich's
        # own detection see it too. Our click/typer echo path does NOT rely on
        # this — Click ignores NO_COLOR (see utils.output.secho, which forces
        # color=False); the Rich consoles key off set_no_color / color_disabled.
        os.environ["NO_COLOR"] = "1"


def _load_config() -> ConfigManager:
    """Load the manifest into a :class:`ConfigManager`, exiting on any absence/parse failure.

    Routes the read through the module-level :func:`parse_config` (the seam
    CLI tests patch) and wraps the result so the manifest is parsed exactly
    once per command path. Every manifest-absence outcome maps to the same
    exit-1 behaviour the inline ``parse_config()`` call sites had:

    - ``parse_config`` raising :class:`ConfigError` (structurally invalid
      manifest) or ``TypeError`` (the historical missing-file unpack signal
      some tests still simulate) → ``logger.error`` + ``typer.Exit(1)``.
    - ``parse_config`` returning ``None`` (a genuinely missing file) — which
      the old call sites turned into a ``TypeError`` by unpacking ``None`` —
      → the same ``logger.error`` + ``typer.Exit(1)``.

    Commands that must distinguish the soft missing-file path from a parse
    error (``images clean``) or treat it as non-fatal
    (``global show instances``) do **not** use this helper; they wrap
    :func:`parse_config` directly and inspect the result.

    Returns:
        A loaded :class:`ConfigManager`.

    Raises:
        typer.Exit: Code 1 when the manifest is missing or cannot be parsed.
    """
    try:
        parsed = parse_config()
    except (ConfigError, TypeError):
        logger.error(CONFIG_PARSE_ERROR_MSG)
        raise typer.Exit(code=1)
    if parsed is None:
        logger.error(CONFIG_PARSE_ERROR_MSG)
        raise typer.Exit(code=1)
    return ConfigManager.from_parsed(parsed)


def _host_networks() -> dict[str, NetworkDefaults]:
    """Return the layered ``networks:`` per-network defaults for the up path.

    Reads the same ``/etc`` -> ``~`` -> CWD layered config ``createvm`` uses
    (:func:`tkc_lvlab.config.load_host_config`) and returns its ``networks``
    map, so a manifest VM on a bridge interface inherits that network's
    gateway/DNS without repeating it per machine (#138 Phase 3). A
    structurally invalid layer maps to the standard config-error exit.

    Returns:
        The per-network defaults map (``{}`` when no layer declares
        ``networks:``).

    Raises:
        typer.Exit: A config layer is structurally invalid (code 1).
    """
    try:
        return load_host_config().networks
    except ValueError as exc:
        logger.error("%s", exc)
        raise typer.Exit(code=1)


class ResolvedMachine:
    """The libvirt-resolved view of a manifest machine.

    Bundles the four facts every machine-scoped command needs after the
    shared prologue (load config → resolve manifest entry → construct
    :class:`Machine` → probe libvirt): the :class:`Machine` object, the
    libvirt URI it was probed against, whether a domain exists there, and
    its current state. It is the return value of :func:`_resolve_machine`,
    the one prologue seam the machine-scoped commands call.

    Commands that only act on an already-defined domain (``destroy``,
    ``snapshot create``/``list``/``delete``) use the
    :func:`_resolve_existing_machine` wrapper, which collapses both the
    not-in-manifest and not-deployed outcomes to a single ``None``
    early-return. ``down`` branches on presence/state and consumes the
    fields directly. (``up`` keeps its own prologue: it needs the full
    parsed config tuple for the first-time-create path and pins a distinct
    not-found message, so routing it through this seam would add threading
    rather than remove duplication.)

    Attributes:
        machine: The constructed :class:`Machine`.
        libvirt_uri: The URI the machine was probed against.
        exists: ``True`` when a domain with the machine's libvirt name is
            defined at ``libvirt_uri``.
        state: The libvirt domain state string (e.g. ``"running"``,
            ``"shut off"``), or ``None`` when the domain does not exist.
    """

    __slots__ = ("machine", "libvirt_uri", "exists", "state")

    def __init__(
        self,
        machine: Machine,
        libvirt_uri: str,
        exists: bool,
        state: str | None,
    ) -> None:
        self.machine = machine
        self.libvirt_uri = libvirt_uri
        self.exists = exists
        self.state = state


def _resolve_machine(vm_name: str) -> ResolvedMachine | None:
    """Run the shared machine-scoped command prologue.

    Consolidates the load-config → resolve-manifest-entry →
    construct-:class:`Machine` → probe-libvirt sequence that the
    machine-scoped commands all repeat. The manifest-level failure
    boundary is handled here once:

    - ``parse_config()`` failing (missing file or :class:`ConfigError`) →
      ``logger.error`` then ``typer.Exit(1)`` (via :func:`_load_config`).
    - ``vm_name`` not in the manifest → ``logger.error`` with the
      ``MACHINE_NOT_IN_MANIFEST_MSG`` template, returns ``None``.

    On success the caller gets a :class:`ResolvedMachine` carrying the
    machine, URI, existence, and state, and decides what to do with the
    presence/state (``down`` shuts down a running domain or no-ops an
    absent one; :func:`_resolve_existing_machine` rejects absent ones).

    Args:
        vm_name: The ``vm_name`` from the user-supplied CLI argument.

    Returns:
        A :class:`ResolvedMachine` on success, or ``None`` when ``vm_name``
        is not in the manifest (the caller should return early).

    Raises:
        typer.Exit: Code 1 when the manifest is missing or cannot be
            parsed. Matches the long-standing parse-failure behaviour.
    """
    config = _load_config()
    environment, _, config_defaults, _ = config.as_tuple()

    machine_config = config.get_machine(vm_name)
    if not machine_config:
        logger.error(MACHINE_NOT_IN_MANIFEST_MSG, vm_name)
        return None

    machine = Machine(machine_config, environment, config_defaults)
    libvirt_uri = environment.get("libvirt_uri", DEFAULT_LIBVIRT_URI)
    try:
        exists, state, _ = machine.exists_in_libvirt(libvirt_uri)
    except VirshError as exc:
        # Single boundary for an unreachable/failing connection during the
        # existence probe — converts the leaked traceback every machine-scoped
        # command previously risked into a clean exit-1 (the #51 hierarchy's
        # stated reason for being). The narrower operation errors
        # (create/delete snapshot) stay caught in their command bodies, which
        # carry an operation-specific message and intentionally exit 0.
        logger.error("Failed to query libvirt at %s: %s", libvirt_uri, exc)
        raise typer.Exit(code=1)
    return ResolvedMachine(machine, libvirt_uri, exists, state)


def _resolve_existing_machine(vm_name: str) -> tuple[Machine | None, str | None]:
    """Resolve a manifest entry into a :class:`Machine` that exists in libvirt.

    Thin wrapper over :func:`_resolve_machine` for the commands that operate
    only on an already-defined domain (``destroy``, ``snapshot list``,
    ``snapshot delete``). It adds the not-deployed guard on top of the shared
    prologue, collapsing both non-fatal outcomes to a single ``(None, None)``:

    - ``vm_name`` not in the manifest → handled by :func:`_resolve_machine`
      (``logger.error`` with ``MACHINE_NOT_IN_MANIFEST_MSG``).
    - Machine resolved but not present at the libvirt URI →
      ``logger.warning`` with the ``MACHINE_NOT_DEPLOYED_MSG`` template.

    Args:
        vm_name: The ``vm_name`` from the user-supplied CLI argument.

    Returns:
        ``(machine, libvirt_uri)`` on success. ``(None, None)`` on any
        non-fatal failure (caller should just return early).

    Raises:
        typer.Exit: With code 1 when ``parse_config()`` cannot read the
            manifest — either a missing file or a structurally invalid one
            (:class:`ConfigError`). Matches the long-standing parse-failure
            behaviour.
    """
    resolved = _resolve_machine(vm_name)
    if resolved is None:
        return None, None
    if not resolved.exists:
        logger.warning(
            MACHINE_NOT_DEPLOYED_MSG, resolved.machine.vm_name, resolved.libvirt_uri
        )
        return None, None
    return resolved.machine, resolved.libvirt_uri


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
def cloudinit(
    vm_name: str,
    to_stdout: bool = typer.Option(
        False,
        "--stdout",
        help=(
            "Render to a tmpdir and print meta-data / user-data / "
            "network-config to stdout with separators. Does NOT touch "
            "the per-VM dir under disk_image_basedir — useful for "
            "quick inspection without root."
        ),
    ),
) -> None:
    """Render cloud-init files for a manifest VM without starting it."""
    config = _load_config()
    environment, images, config_defaults, machines = config.as_tuple()

    machine = Machine(config.get_machine(vm_name), environment, config_defaults)

    if not machine:
        return

    image_config = _resolve_image_config(images, machine.os, machine.vm_name)
    cloud_image = CloudImage(machine.os, image_config, environment, config_defaults)

    if to_stdout:
        # Redirect config_fpath to a tmpdir so the render never touches
        # the per-VM (root-owned) directory. After write, read the three
        # files back and print to stdout with scannable separators.
        import tempfile

        with tempfile.TemporaryDirectory(prefix="lvlab-cloudinit-") as tmpdir:
            machine.config_fpath = tmpdir
            try:
                meta_path, user_path, net_path = machine.cloud_init(
                    cloud_image, config_defaults, machines
                )
            except LvlabError as exc:
                logger.error("%s", exc)
                raise typer.Exit(code=1)
            for label, fpath in (
                ("meta-data", meta_path),
                ("user-data", user_path),
                ("network-config", net_path),
            ):
                typer.echo(f"--- {label} ---")
                with open(fpath, "r", encoding="utf-8") as fh:
                    typer.echo(fh.read().rstrip("\n"))
                typer.echo()  # trailing blank line between blocks
        return

    # Default: render and write under the per-VM dir as today.
    try:
        _, _, _ = machine.cloud_init(cloud_image, config_defaults, machines)
    except LvlabError as exc:
        logger.error("%s", exc)
        raise typer.Exit(code=1)


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
    resolved = _resolve_machine(vm_name)
    if resolved is None:
        return

    if not resolved.exists:
        return

    machine, state = resolved.machine, resolved.state
    if state in {"running", "paused"}:
        typer.echo(f"Shutting down virtual machine {vm_name}.")
        if machine.shutdown(resolved.libvirt_uri) > 0:
            logger.error("Shutdown appears to have failed.")
        else:
            typer.echo(
                "Shutdown appears successful. The virtual machine may take a short time to complete shutdown."
            )
    elif state in ["VIR_DOMAIN_SHUTOFF"]:
        typer.echo(f"The virtual machine {machine.vm_name} is shutdown already.")


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
    environment, _, config_defaults, machines = _load_config().as_tuple()

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


# Ephemeral lab-VM SSH options emitted by default — lab VMs recycle the NAT DHCP
# pool, so the same IP gets a different host key each lab cycle and a strict
# `ssh` would trip "REMOTE HOST IDENTIFICATION HAS CHANGED" on every recycle.
# Keep `--strict-host-keys` available for someone who wants strict checking
# (e.g. a stable manifest pinned to static IPs). See issue #127.
_EPHEMERAL_SSH_OPT_LINES: tuple[str, ...] = (
    "  StrictHostKeyChecking no",
    "  UserKnownHostsFile /dev/null",
    "  CheckHostIP no",
    "  LogLevel ERROR",
)


def _ssh_config_render_machine(
    machine: dict,
    cloud_init_defaults: dict,
    *,
    strict_host_keys: bool = False,
) -> str:
    """Render the ``~/.ssh/config`` snippet for one manifest machine.

    Args:
        machine: One ``machines[]`` entry from the parsed manifest.
        cloud_init_defaults: ``config_defaults.cloud_init`` to merge under
            the per-machine ``cloud_init`` override.
        strict_host_keys: When ``True``, suppress the default ephemeral
            host-key options (``StrictHostKeyChecking no`` /
            ``UserKnownHostsFile /dev/null`` / ``CheckHostIP no`` /
            ``LogLevel ERROR``) and emit the legacy snippet only.

    Returns:
        The multi-line ``Host`` block as one string (no trailing newline).
    """
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
    if not strict_host_keys:
        lines.extend(_EPHEMERAL_SSH_OPT_LINES)
    return "\n".join(lines)


@app.command("ssh-config")
def ssh_config(
    vm_name: str = typer.Argument(None),
    strict_host_keys: bool = typer.Option(
        False,
        "--strict-host-keys",
        help=(
            "Emit the legacy snippet only — omit the default ephemeral "
            "options (StrictHostKeyChecking no / UserKnownHostsFile /dev/null "
            "/ CheckHostIP no / LogLevel ERROR) so ssh enforces strict "
            "host-key checking for these hosts."
        ),
    ),
) -> None:
    """Print ~/.ssh/config snippet(s) for machines in the manifest.

    With no VM_NAME, a snippet is emitted for every machine. With a
    VM_NAME, only that machine's snippet is emitted. Output goes to
    stdout; redirect or append it to ~/.ssh/config yourself.

    By default each ``Host`` block carries ephemeral-lab options that
    keep recycled DHCP-pool IPs from poisoning ``~/.ssh/known_hosts``;
    pass ``--strict-host-keys`` to keep strict checking.
    """
    # ssh-config keeps its bespoke parse handling (echo to stdout, only the
    # missing-file failure caught — a structural ConfigError still
    # propagates) to preserve its observable behaviour; it just sources the
    # parsed sections through ConfigManager so the read happens once. A
    # missing file (parse_config -> None) is the same exit-1 the old
    # ``None`` unpack TypeError produced.
    try:
        config = ConfigManager.from_parsed(parse_config())
    except TypeError:
        typer.echo(CONFIG_PARSE_ERROR_MSG)
        raise typer.Exit(code=1)
    if not config.loaded:
        typer.echo(CONFIG_PARSE_ERROR_MSG)
        raise typer.Exit(code=1)
    _, _, config_defaults, machines = config.as_tuple()

    selected_machines = _ssh_config_select_machines(machines, vm_name)
    cloud_init_defaults = config_defaults.get("cloud_init", {})

    snippets = [
        _ssh_config_render_machine(
            machine, cloud_init_defaults, strict_host_keys=strict_host_keys
        )
        for machine in selected_machines
    ]
    typer.echo("\n\n".join(snippets))


# ---------------------------------------------------------------------------
# ssh: direct SSH into a manifest VM (experimental, see GitHub issue)
# ---------------------------------------------------------------------------

# Same ephemeral-lab opts ssh-config emits by default (#127). Replicated as
# explicit ``-o`` flags here so ``lvlab ssh`` works even when the operator
# hasn't appended an ssh-config snippet to ``~/.ssh/config``.
_LVLAB_SSH_EPHEMERAL_OPTS: tuple[tuple[str, str], ...] = (
    ("StrictHostKeyChecking", "no"),
    ("UserKnownHostsFile", "/dev/null"),
    ("CheckHostIP", "no"),
    ("LogLevel", "ERROR"),
)


def _ssh_command_argv(
    host_ip: str, user: str | None, identity_file: str | None
) -> list[str]:
    """Build the ``ssh`` argv for an ephemeral lab VM.

    Args:
        host_ip: The guest IP to connect to (no port — default 22).
        user: Login user, or ``None`` to omit (ssh picks ``$USER``).
        identity_file: Path to the private key, or ``None`` to omit.

    Returns:
        The fully-resolved argv ready for ``os.execvp``.
    """
    argv: list[str] = ["ssh"]
    for key, value in _LVLAB_SSH_EPHEMERAL_OPTS:
        argv.extend(["-o", f"{key}={value}"])
    if identity_file:
        argv.extend(["-i", identity_file])
    argv.append(f"{user}@{host_ip}" if user else host_ip)
    return argv


def _lvlab_ssh_resolve_dhcp_ip(libvirt_uri: str, libvirt_domain: str) -> str | None:
    """One-shot DHCP-lease lookup for ``lvlab ssh``.

    No polling — if the lease isn't visible right now (guest not up, not
    on DHCP, lease not yet observed), return ``None`` and let the caller
    report it. The smoke runner does its own polling because it just
    booted the guest; ``lvlab ssh`` is invoked by a human after they
    expect the guest to be live, so a single read is the right contract.
    """
    from .smoke import _parse_domifaddr_lease  # local import: avoid cycle

    try:
        result = run_virsh(
            libvirt_uri,
            ["domifaddr", libvirt_domain, "--source", "lease"],
            check=False,
        )
    except VirshError:
        return None
    if result.returncode != 0:
        return None
    return _parse_domifaddr_lease(result.stdout)


@app.command()
def ssh(vm_name: str) -> None:
    """SSH into a manifest VM with the right user, key, and lab-friendly opts.

    Resolves the IP (manifest static first, then ``virsh domifaddr`` for
    DHCP), the login user (``cloud_init.user`` if set, else the image's
    default username), and the identity file (``cloud_init.pubkey`` when
    it's a path on disk). Then ``exec``\\ s ``ssh`` with the ephemeral-lab
    options from #127, so the process replaces this one — stdin/stdout/
    stderr are wired directly to the SSH session.
    """
    config = _load_config()
    environment, images, config_defaults, _machines = config.as_tuple()
    machine_config = config.get_machine(vm_name)
    if not machine_config:
        typer.echo(f"Machine {vm_name} not found in manifest.")
        raise typer.Exit(code=1)

    machine = Machine(machine_config, environment, config_defaults)

    host_ip = _ssh_config_primary_ip(machine_config)
    if not host_ip:
        host_ip = _lvlab_ssh_resolve_dhcp_ip(
            environment.get("libvirt_uri", "qemu:///system"),
            machine.libvirt_vm_name,
        )
    if not host_ip:
        typer.echo(
            f"Could not resolve an IP for {vm_name} — is the VM up? "
            "Try `lvlab status` or `virsh domifaddr "
            f"{machine.libvirt_vm_name} --source lease`."
        )
        raise typer.Exit(code=1)

    cloud_init_defaults = config_defaults.get("cloud_init", {})
    merged_ci = {**cloud_init_defaults, **machine_config.get("cloud_init", {})}
    user = merged_ci.get("user")
    if not user:
        image_config = _resolve_image_config(images, machine.os, machine.vm_name)
        cloud_image = CloudImage(machine.os, image_config, environment, config_defaults)
        user = cloud_image.default_username

    identity_file = _ssh_config_identity_file(merged_ci.get("pubkey"))

    argv = _ssh_command_argv(host_ip, user, identity_file)
    # Echo the command we're about to exec so the operator can see what
    # we're doing (and copy-paste if they want to vary it). Goes to
    # stderr so stdout stays clean for any SSH transfer that follows.
    typer.echo(f"# {' '.join(argv)}", err=True)
    os.execvp(argv[0], argv)


# ---------------------------------------------------------------------------
# init: concurrent per-image download + verify with a compact progress view
# (issue #104)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _ImageInitState:
    """Mutable per-image progress for the ``lvlab init`` display (issue #104).

    ``version`` is the best-guess upstream version token shown in the init
    table column (#124) — populated from :func:`tkc_lvlab.utils.catalog.image_version`
    at construction; ``"?"`` when the heuristic doesn't recognise the format.
    """

    name: str
    phase: str = "pending"
    bytes_done: int = 0
    bytes_total: int = 0
    error: str = ""
    version: str = "?"


class _InitProgress:
    """Thread-safe per-image progress shared by init workers and the renderer.

    Worker threads mutate state through the setters (guarded by a lock); the
    main thread reads consistent snapshots to render, so every Rich call stays
    on the main thread and the workers never render.
    """

    def __init__(
        self,
        names: list[str],
        *,
        versions: dict[str, str] | None = None,
    ) -> None:
        # versions: per-image best-guess upstream version token from
        # ``image_version()``, surfaced by ``lvlab init``'s table (#124).
        # Names absent from the map fall back to "?", matching the helper.
        versions = versions or {}
        self._lock = threading.Lock()
        self._order = list(names)
        self._states = {
            n: _ImageInitState(n, version=versions.get(n, "?")) for n in names
        }

    def set_phase(self, name: str, phase: str) -> None:
        """Set an image's phase (e.g. ``downloading`` / ``verifying`` / ``done``)."""
        with self._lock:
            self._states[name].phase = phase

    def set_bytes(self, name: str, done: int, total: int) -> None:
        """Record download byte progress for an image (the download callback)."""
        with self._lock:
            state = self._states[name]
            state.phase = "downloading"
            state.bytes_done = done
            state.bytes_total = total

    def set_error(self, name: str, message: str) -> None:
        """Mark an image failed with a message (a fatal download/verify error)."""
        with self._lock:
            state = self._states[name]
            state.phase = "failed"
            state.error = message

    def snapshot(self) -> list["_ImageInitState"]:
        """Return a consistent copy of every image's state, in declared order."""
        with self._lock:
            return [dataclasses.replace(self._states[n]) for n in self._order]


def _format_bytes(num: int) -> str:
    """Compact binary-unit byte string for the progress cell."""
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{num}B"


def _init_progress_cell(state: "_ImageInitState") -> str:
    """Render the compact progress cell for one image row.

    A segmented block bar with a percentage when the total is known; the raw
    byte count for a content-encoded body (unknown total); a check / cross for
    done / failed.
    """
    if state.phase == "done":
        return "[green]✓[/green]"
    if state.phase == "failed":
        return f"[red]✗ {state.error}[/red]"
    if state.phase == "downloading" and state.bytes_total > 0:
        pct = int(state.bytes_done * 100 / state.bytes_total)
        filled = max(0, min(10, pct // 10))
        return f"{'█' * filled}{'░' * (10 - filled)} {pct:3d}%"
    if state.phase == "downloading" and state.bytes_done:
        return _format_bytes(state.bytes_done)
    return ""


def _render_init_table(
    states: list["_ImageInitState"], *, env_name: str, jobs: int
) -> Table:
    """Build the init progress table from a state snapshot."""
    table = styled_table(
        title=f"lvlab init — {env_name} · {len(states)} images · {jobs} concurrent"
    )
    table.add_column("image", style="bold")
    table.add_column("version")
    table.add_column("phase")
    table.add_column("progress")
    for state in states:
        table.add_row(
            state.name, state.version, state.phase, _init_progress_cell(state)
        )
    return table


def _init_image_worker(image: CloudImage, progress: "_InitProgress") -> bool:
    """Download + verify one image, updating ``progress``. Returns False on a fatal error.

    Mirrors the previous sequential pipeline's control flow: a transport/HTTP
    failure (``ImageError`` / ``LvlabError``) is fatal (returns False, so init
    exits 1); a content-length mismatch or a verify failure is logged but
    non-fatal (returns True), preserving the prior exit-0 behaviour.
    """
    name = image.name
    try:
        if not image.exists_locally("image"):
            progress.set_phase(name, "downloading")
            if not image.download_image(
                progress_callback=lambda done, total: progress.set_bytes(
                    name, done, total
                )
            ):
                logger.error("CloudImage download failed")
        if image.checksum_url_gpg and not image.exists_locally("checksum_gpg"):
            progress.set_phase(name, "gpg")
            if not image.download_checksum_gpg():
                logger.error("CloudImage %s checksum GPG file download failed", name)
        if image.checksum_url and not image.exists_locally("checksum"):
            progress.set_phase(name, "checksum")
            if not image.download_checksum():
                logger.error("CloudImage %s checksum file download failed", name)
        progress.set_phase(name, "verifying")
        if image.checksum_url_gpg and image.exists_locally("checksum_gpg"):
            if not image.gpg_verify_checksum_file():
                logger.error("CloudImage %s checksum file GPG validation BAD", name)
        if image.checksum_url and image.exists_locally("checksum"):
            if not image.checksum_verify_image():
                logger.error("CloudImage %s checksum verification BAD", name)
        progress.set_phase(name, "done")
        return True
    except LvlabError as exc:
        # Clean boundary (issue #98): a transport/HTTP failure surfaces the
        # ImageError's actionable message + workaround, not a traceback.
        logger.error("%s", exc)
        progress.set_error(name, str(exc))
        return False


def _run_init_concurrent(
    built: list[CloudImage], progress: "_InitProgress", *, jobs: int, env_name: str
) -> list[bool]:
    """Run the per-image workers concurrently, rendering progress.

    On a terminal, a Rich ``Live`` table is refreshed from the main thread off
    the shared (locked) progress state; piped/redirected output degrades to
    plain per-image completion lines (no ANSI / no Live), so logs stay clean.

    Returns:
        A list of per-image booleans (``False`` = a fatal error occurred).
    """
    if is_tty():
        console = get_console()
        with Live(console=console, refresh_per_second=8) as live:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
                futures = [
                    pool.submit(_init_image_worker, img, progress) for img in built
                ]
                pending = set(futures)
                while pending:
                    _done, pending = concurrent.futures.wait(pending, timeout=0.2)
                    live.update(
                        _render_init_table(
                            progress.snapshot(), env_name=env_name, jobs=jobs
                        )
                    )
            live.update(
                _render_init_table(progress.snapshot(), env_name=env_name, jobs=jobs)
            )
        return [f.result() for f in futures]

    # Non-TTY: plain incremental lines, no ANSI / no Live.
    results: list[bool] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {
            pool.submit(_init_image_worker, img, progress): img.name for img in built
        }
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            ok = fut.result()
            state = next(s for s in progress.snapshot() if s.name == name)
            status = (
                "done"
                if ok and state.phase != "failed"
                else f"FAILED ({state.error or 'see log'})"
            )
            typer.echo(f"  {name}: {status}")
            results.append(ok)
    return results


@app.command()
def init(
    jobs: int = typer.Option(
        2,
        "--jobs",
        "-j",
        min=1,
        help="Number of images to download/verify concurrently (default 2).",
    ),
) -> None:
    """Initialize cloud images: the manifest's, or the built-in defaults.

    With an ``Lvlab.yml`` in the current directory, downloads and verifies the
    images its ``images:`` section names. **With no manifest, it initializes
    the built-in default catalog** (issue #97). Images are fetched a few at a
    time (``--jobs``), with a compact live progress table on a terminal that
    degrades to plain per-image lines when output is piped (issue #104).
    """
    environment, images, config_defaults = _init_image_source()
    env_name = environment.get("name", "default")

    if not images:
        typer.echo("No images to initialize.")
        return

    built = [
        CloudImage(name, cfg, environment, config_defaults)
        for name, cfg in images.items()
    ]
    # Best-guess upstream version per image — surfaced in the init table
    # so an operator can see WHICH dated build / codename they cached (#124).
    versions = {img.name: image_version(img.image_url, img.filename) for img in built}
    progress = _InitProgress([img.name for img in built], versions=versions)

    typer.echo()
    typer.echo(f"Initializing Libvirt Lab Environment: {env_name}\n")

    results = _run_init_concurrent(built, progress, jobs=jobs, env_name=env_name)

    if not all(results):
        raise typer.Exit(code=1)


def _init_image_source() -> tuple[dict, dict, dict]:
    """Resolve ``lvlab init``'s image source: manifest images, else built-ins.

    Reads the cwd ``Lvlab.yml`` via :func:`parse_config`. When a manifest is
    present its ``environment[0]`` / ``images`` / ``config_defaults`` are
    used. When **no** manifest exists (``parse_config`` returns ``None``),
    the built-in default catalog
    (:data:`tkc_lvlab.utils.catalog.BUILTIN_IMAGES`) is initialized into the
    shared cache under a synthetic ``default`` environment (issue #97). A
    structurally invalid manifest still fails loudly.

    Returns:
        ``(environment, images, config_defaults)``.

    Raises:
        typer.Exit: Code 1 when a manifest exists but cannot be parsed.
    """
    try:
        parsed = parse_config()
    except (ConfigError, TypeError):
        logger.error(CONFIG_PARSE_ERROR_MSG)
        raise typer.Exit(code=1)

    if parsed is None:
        logger.info("No Lvlab.yml found; initializing the built-in default catalog.")
        return {"name": "default"}, dict(BUILTIN_IMAGES), {}

    environment, images, config_defaults, _ = parsed
    return environment, images, config_defaults


def _read_manifest_text(fpath: str = "Lvlab.yml") -> str:
    """Read the raw manifest text for comment-scanning, best-effort.

    The comment-referenced protection (#91) needs the verbatim ``Lvlab.yml``
    bytes — commented-out image entries are invisible to ``parse_config``.
    Reading is best-effort: an unreadable or absent file yields an empty
    string (no comment protection) rather than failing the clean command,
    which has already validated the manifest via ``parse_config``.

    Args:
        fpath: Path to the manifest. Defaults to ``"Lvlab.yml"`` in the
            current working directory (the same default ``parse_config``
            uses).

    Returns:
        The file's text, or ``""`` if it could not be read.
    """
    try:
        with open(fpath, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _echo_clean_plan(candidates: list[CleanupCandidate], force: bool) -> None:
    """Print the per-candidate removal plan (dry-run preview or live action)."""
    verb = "Removing" if force else "Would remove"
    for candidate in candidates:
        typer.echo(f"{verb}: {candidate.image_fpath}")
        for sidecar in candidate.sidecar_fpaths:
            typer.echo(f"  - sidecar: {sidecar}")


@images_app.command("clean")
def images_clean(
    force: bool = typer.Option(
        False,
        "--force",
        "--yes",
        "--delete",
        help="Actually delete the unreferenced files. Without this, the "
        "command is a dry run that only lists what WOULD be removed.",
    ),
) -> None:
    """Remove cloud-image cache files no manifest image claims (dry-run by default).

    Builds a protected set from EVERY entry in the manifest's images section
    (image qcow2 + checksum + .verified + GPG sidecar, via the canonical
    CloudImage derivation) and, as defense-in-depth, any cache image currently
    used as a qcow2 backing file by an on-disk disk. Every other file in the
    cloud-image cache directory is a removal candidate; its sidecars are
    removed together with it.

    Safety model: dry-run by default (without --force/--yes/--delete nothing
    is deleted, only listed); the config_defaults.prevent_cloud_image_cleanup
    lock refuses all deletion even with --force; and a missing or unparseable
    Lvlab.yml aborts rather than guessing what is protected.

    Raises:
        typer.Exit: Code 1 on a missing/unparseable manifest, or when the
            lock parameter is set and ``--force`` was requested.
    """
    try:
        parsed = parse_config()
    except (ConfigError, TypeError) as exc:
        logger.error("%s (%s)", CONFIG_PARSE_ERROR_MSG, exc)
        raise typer.Exit(code=1)

    if parsed is None:
        # A missing Lvlab.yml: refuse rather than guess what is protected.
        logger.error(
            "No Lvlab.yml found in the current directory; refusing to clean "
            "the cloud-image cache without a manifest to protect against."
        )
        raise typer.Exit(code=1)

    environment, images, config_defaults, _ = parsed

    locked = bool(config_defaults.get(PREVENT_CLEANUP_FLAG, False))

    image_dir = resolve_cloud_image_dir(config_defaults)
    typer.echo(f"Cloud-image cache: {image_dir}")

    manifest_protected = enumerate_protected_files(images, environment, config_defaults)
    backing = backing_files_in_use(environment, config_defaults)
    # Commented-out image entries (#91): a filename mentioned in a comment is
    # treated as referenced so a temporarily-disabled image isn't re-downloaded.
    commented = comment_referenced_files(image_dir, _read_manifest_text())
    protected = manifest_protected | backing | commented

    candidates = find_cleanup_candidates(image_dir, protected)

    # Report what survives and why — manifest-protected vs. in-use backing vs.
    # comment-referenced. Precedence (most authoritative first): an in-use
    # backing file, then an active manifest entry, then a commented-out mention.
    if os.path.isdir(image_dir):
        backing_abs = {os.path.abspath(p) for p in backing}
        manifest_abs = {os.path.abspath(p) for p in manifest_protected}
        commented_abs = {os.path.abspath(p) for p in commented}
        for fname in sorted(os.listdir(image_dir)):
            fpath = os.path.join(image_dir, fname)
            if not os.path.isfile(fpath):
                continue
            abspath = os.path.abspath(fpath)
            if abspath in backing_abs:
                typer.echo(f"Protected (in use as backing file): {fpath}")
            elif abspath in manifest_abs:
                typer.echo(f"Protected (defined in manifest): {fpath}")
            elif abspath in commented_abs:
                typer.echo(f"Protected (commented out in manifest): {fpath}")

    if not candidates:
        typer.echo("No unreferenced cloud-image files to remove.")
        return

    if locked:
        # Lock parameter: refuse all deletion regardless of --force.
        typer.echo(
            f"Cleanup is disabled by config_defaults.{PREVENT_CLEANUP_FLAG}; "
            f"{len(candidates)} candidate(s) left untouched."
        )
        _echo_clean_plan(candidates, force=False)
        if force:
            raise typer.Exit(code=1)
        return

    _echo_clean_plan(candidates, force=force)

    if not force:
        typer.echo("Dry run: nothing deleted. Re-run with --force to remove the above.")
        return

    removed = 0
    for candidate in candidates:
        for fpath in candidate.all_fpaths:
            try:
                os.remove(fpath)
                removed += 1
            except OSError as exc:
                logger.error("Failed to remove %s: %s", fpath, exc)
    typer.echo(f"Removed {removed} file(s) across {len(candidates)} candidate(s).")


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
    machine, libvirt_uri = _resolve_existing_machine(vm_name)
    if machine is None:
        return

    try:
        machine.create_snapshot(libvirt_uri, snapshot_name, snapshot_description)
        typer.echo(f"Snapshot {snapshot_name} created for {machine.vm_name}")
    except VirshError as e:
        logger.error(
            "Failed to create snapshot %s for %s: %s",
            snapshot_name,
            machine.vm_name,
            e,
        )


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

    Issue #103 reshaped the output into the shared-style tables (see
    :mod:`tkc_lvlab.utils.output`): a Machines table (VM + state) and an
    Images table that merges the built-in default catalog with the
    manifest's ``images:`` — labelling each image's source (``manifest``
    vs ``default``) and whether it's cached on disk — so defaults show
    up even when the manifest doesn't reference them.

    Issue #149: when no ``Lvlab.yml`` is present in the current
    directory, ``status`` renders a friendly landing (built-in images
    table + ``createvm`` pointer + docs URL) instead of erroring — this
    is the most common first-run command and a missing manifest is a
    missing-context signal, not a misconfiguration. A *malformed*
    manifest still exits 1 loudly through :func:`_load_config`.
    """
    # Detect the genuine "no Lvlab.yml" case directly via parse_config so
    # the landing only triggers on file-absent. Anything else (structural
    # invalid → ConfigError; missing-file-as-TypeError some tests still
    # simulate) routes through _load_config and keeps the loud exit-1.
    try:
        parsed = parse_config()
    except (ConfigError, TypeError):
        logger.error(CONFIG_PARSE_ERROR_MSG)
        raise typer.Exit(code=1)
    if parsed is None:
        _render_no_manifest_landing()
        return
    environment, images, config_defaults, machines = ConfigManager.from_parsed(
        parsed
    ).as_tuple()

    uri = environment.get("libvirt_uri", DEFAULT_LIBVIRT_URI)
    env_name = environment.get("name", "no-name-lvlab")

    console = get_console()
    console.print(f"\nLvLab Environment Name: {env_name}\n")

    try:
        current_vms = virsh_list_all_names(uri)
    except VirshError as exc:
        logger.error("Failed to list domains at %s: %s", uri, exc)
        raise typer.Exit(code=1)

    machines_table = styled_table(title="Machines")
    machines_table.add_column("VM", style="bold")
    machines_table.add_column("State")
    for machine in machines:
        vm_name = machine["vm_name"]
        libvirt_vm_name = f"{vm_name}_{env_name}"
        if libvirt_vm_name in current_vms:
            try:
                state = virsh_domstate(uri, libvirt_vm_name)
            except VirshError as exc:
                logger.error("Failed to query state for %s: %s", libvirt_vm_name, exc)
                machines_table.add_row(vm_name, "unknown (virsh error)")
                continue
            machines_table.add_row(vm_name, state)
        else:
            machines_table.add_row(vm_name, "undeployed")
    console.print(machines_table)

    console.print()
    console.print(_build_images_table(images, environment, config_defaults))
    console.print()


_DOCS_URL = "https://github.com/memblin/tkc-lvlab-py"


def _render_no_manifest_landing() -> None:
    """Render the friendly landing for ``lvlab status`` without a manifest (#149).

    Triggered when ``parse_config()`` returns ``None`` (the file is
    genuinely absent — distinct from a parse error). Prints:

    1. A short "no manifest here" line — informational, not an error.
    2. The built-in cloud-images table (same shape as the happy-path
        Images table, just with ``images=None`` so only built-ins land).
    3. A ``createvm`` pointer with a concrete usage hint for the
        no-manifest one-off path.
    4. A link to the project docs for the manifest-authoring workflow.

    Exits cleanly (returns to the caller, which returns to Typer with
    exit 0). A *malformed* manifest still routes through the strict
    error path in :func:`status`.
    """
    console = get_console()
    console.print()
    console.print("No Lvlab.yml in this directory. Showing built-in cloud images.")
    console.print()
    console.print(_build_images_table(images=None, environment={}, config_defaults={}))
    console.print()
    console.print(
        "Need a one-off VM? Use [bold]createvm[/bold] — no manifest required:"
    )
    console.print()
    console.print("    createvm web01.example debian13 --ip4 192.168.122.50")
    console.print()
    console.print(
        "For the manifest-driven workflow (multi-VM environments, snapshots, "
        "runcmd composition, IPv6 dual-stack, user_data overrides, ...) see:"
    )
    console.print(f"    {_DOCS_URL}")
    console.print()


def _build_images_table(
    images: dict[str, Any] | None,
    environment: dict[str, Any],
    config_defaults: dict[str, Any],
) -> Table:
    """Build the ``status`` Images table: built-in defaults merged with the manifest.

    Merges the built-in catalog (:data:`tkc_lvlab.utils.catalog.BUILTIN_IMAGES`)
    with the manifest's ``images:`` — manifest wins on a name collision —
    so built-in defaults are listed even when the manifest doesn't
    reference them (``status`` answers "what images are available", not
    just "what this manifest names"). Each row is labelled with its source
    (``manifest`` vs ``default``) and whether the image is already cached
    on disk.

    Args:
        images: The manifest's ``images:`` dict, or ``None`` when absent.
        environment: The manifest's ``environment[0]`` dict (passed
            through to :class:`~tkc_lvlab.utils.images.CloudImage`).
        config_defaults: The manifest's ``config_defaults`` dict (supplies
            ``cloud_image_basedir`` for the cache-path lookup).

    Returns:
        A populated :class:`rich.table.Table` titled ``Images``.
    """
    manifest_names = set(images or {})
    catalog = resolve_catalog(images)

    table = styled_table(title="Images")
    table.add_column("image", style="bold")
    table.add_column("source")
    table.add_column("cached")
    table.add_column("url")

    for name in sorted(catalog):
        cfg = catalog[name]
        source = "manifest" if name in manifest_names else "default"
        url = cfg.get("image_url")
        if url:
            cached = (
                "yes"
                if CloudImage(name, cfg, environment, config_defaults).exists_locally(
                    "image"
                )
                else "no"
            )
        else:
            url = "(missing image_url)"
            cached = "?"
        table.add_row(name, source, cached, url)

    return table


@app.command()
def smoke(
    config: str = typer.Option(
        "Lvlab.yml",
        "--config",
        "-c",
        help="Manifest to drive the smoke run (default: ./Lvlab.yml).",
    ),
    output_format: OutputFormat = typer.Option(
        OutputFormat.TEXT,
        "--format",
        "-f",
        help="Output format: text (default), json, or yaml.",
    ),
    batch_size: int = typer.Option(
        None,
        "--batch-size",
        help="Explicit concurrent VMs per batch; overrides memory packing.",
    ),
    max_memory: int = typer.Option(
        None,
        "--max-memory",
        help="Cap the memory budget (MiB) the scheduler packs batches under.",
    ),
    reserve: int = typer.Option(
        2048,
        "--reserve",
        help="Memory (MiB) held back from available RAM for host + qemu slack.",
    ),
    skip_preflight: bool = typer.Option(
        False,
        "--skip-preflight",
        help="Skip the preflight checks (debugging only).",
    ),
    assume_yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the memory-heavy confirmation prompt (use in CI / scripts).",
    ),
    list_only: bool = typer.Option(
        False,
        "--list",
        help=(
            "Resolve the manifest into the case list and print a preview "
            "table (vm_name / os / mode / IP / memory / vCPUs / ssh_user); "
            "exit 0 WITHOUT booting any VMs. Useful before a real run."
        ),
    ),
) -> None:
    """Boot every manifest VM, SSH-verify it, then tear it down (manual only).

    For each machine in the manifest: `lvlab up` -> resolve its IP (static
    from the manifest, else the DHCP lease for its pinned MAC) -> SSH in as
    the image's default user and run `id -un`/`hostname` -> `lvlab down` ->
    `lvlab destroy --force`. Runs a preflight gate first (cached images, free
    static addresses, SSH key present), then detects host memory + vCPUs and
    bin-packs the machines into concurrent batches under a memory budget,
    printing the computed plan before booting anything. Use `--batch-size` to
    pin an explicit concurrency, or `--max-memory`/`--reserve` to tune the
    budget.

    This boots REAL qemu:///system VMs and is never wired into CI — run it
    only on a libvirt host with no developer VMs at risk. Exit code is 0 when
    every machine passes, 1 on any failure (for every output format).
    """
    if list_only:
        _smoke_print_case_list(config)
        raise typer.Exit(code=0)

    try:
        code = run_smoke(
            config,
            fmt=output_format,
            batch_size=batch_size,
            max_memory_mib=max_memory,
            reserve_mib=reserve,
            skip_preflight=skip_preflight,
            assume_yes=assume_yes,
        )
    except SmokeError as exc:
        secho(f"smoke: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    raise typer.Exit(code=code)


def _smoke_print_case_list(config_path: str) -> None:
    """Render and print the smoke preview table for ``--list``.

    Reads the manifest, resolves cases via :func:`build_cases` (same path
    the runner takes), and emits a styled Rich table to stdout. Empty
    manifest -> plain "no machines" line, still exit 0.
    """
    try:
        parsed = parse_config(config_path)
    except (ConfigError, TypeError):
        secho(
            f"smoke --list: could not parse '{config_path}'.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    if parsed is None:
        typer.echo(f"smoke --list: no manifest at '{config_path}'.")
        raise typer.Exit(code=1)
    environment, images, config_defaults, machines = parsed
    if not machines:
        typer.echo("smoke --list: no machines in manifest.")
        return

    cases = build_cases(environment, images, config_defaults, machines)
    table = styled_table(
        title=f"lvlab smoke --list — {environment.get('name', 'default')} · "
        f"{len(cases)} machine(s)"
    )
    table.add_column("vm_name", style="bold")
    table.add_column("os")
    table.add_column("mode")
    table.add_column("ip")
    table.add_column("memory", justify="right")
    table.add_column("vcpus", justify="right")
    table.add_column("ssh_user")
    for case in cases:
        table.add_row(
            case.vm_name,
            case.os,
            case.mode,
            case.static_ip or "—",
            f"{case.memory_mib} MiB",
            str(case.vcpus),
            case.ssh_user,
        )
    get_console().print(table)


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


def _resolve_up_password(
    machine: Machine, config_defaults: dict
) -> tuple[str | None, str | None]:
    """Decide the one-time console password for a first-boot ``up`` (issue #106).

    Returns ``(plaintext, hash)``. ``(None, None)`` when the manifest
    already configures a password (``cloud_init.passwd``) or explicitly
    opts out (``cloud_init.password: false`` / ``generate_password: false``)
    — nothing to generate, inject, or print. Otherwise a freshly generated
    phrase and its SHA-512-crypt hash. If ``openssl`` is unavailable the
    password is skipped (logged) rather than failing the deploy — the SSH
    key is the primary access path; the console password is a convenience.

    Args:
        machine: The resolved :class:`Machine`.
        config_defaults: The manifest's ``config_defaults`` block.

    Returns:
        ``(plaintext, hash)`` to generate+print, or ``(None, None)``.
    """
    ci_machine = machine.cloud_init_config or {}
    ci_defaults = config_defaults.get("cloud_init", {}) or {}

    if ci_machine.get("passwd") or ci_defaults.get("passwd"):
        return None, None

    def _opted_out(ci: dict) -> bool:
        return ci.get("password") is False or ci.get("generate_password") is False

    if _opted_out(ci_machine) or _opted_out(ci_defaults):
        return None, None

    try:
        return generate_one_time_password()
    except PasswordHashError as exc:
        logger.warning("Skipping one-time console password: %s", exc)
        return None, None


def _machine_login_user(machine: Machine, config_defaults: dict) -> str:
    """Return the effective first-boot login user for the SSH hint.

    Mirrors :meth:`Machine.cloud_init`'s resolution: an explicit
    ``cloud_init.user`` (machine, then defaults) wins, else the image's
    conventional account (already set on the machine's resolved ``os``
    via the catalog) — falling back to ``root``.
    """
    ci_machine = machine.cloud_init_config or {}
    ci_defaults = config_defaults.get("cloud_init", {}) or {}
    return ci_machine.get("user") or ci_defaults.get("user") or "root"


def _machine_static_ip(machine: Machine) -> str | None:
    """Return the first interface's static IPv4 (CIDR stripped), or ``None``.

    ``None`` means the machine uses DHCP, so the SSH hint can't name an
    address up front.
    """
    interfaces = machine.interfaces or []
    if isinstance(interfaces, dict):
        interfaces = [interfaces]
    for iface in interfaces:
        ip4 = iface.get("ip4") if isinstance(iface, dict) else None
        if ip4:
            return str(ip4).split("/", maxsplit=1)[0]
    return None


def _up_build_cloud_init_iso(
    machine: Machine,
    cloud_image: CloudImage,
    config_defaults: dict,
    machines: list,
    password_hash: str | None = None,
) -> None:
    """Render cloud-init files, pack them into cidata.iso, exit on failure."""
    try:
        metadata_config_fpath, userdata_config_fpath, network_config_fpath = (
            machine.cloud_init(
                cloud_image, config_defaults, machines, password_hash=password_hash
            )
        )
    except LvlabError as exc:
        logger.error("%s", exc)
        raise typer.Exit(code=1)
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
    machine: Machine,
    environment: dict,
    images: dict,
    config_defaults: dict,
    machines: list,
) -> None:
    """First-time create: vdisks → cloud-init ISO → virt-install."""
    typer.echo(f"Creating virtual machine: {machine.vm_name}")

    image_config = _resolve_image_config(images, machine.os, machine.vm_name)
    cloud_image = CloudImage(machine.os, image_config, environment, config_defaults)

    # Generate a one-time console password (issue #106) unless the manifest
    # configures or opts out of one. The hash goes into cloud-init; the
    # plaintext is printed once below.
    password_plain, password_hash = _resolve_up_password(machine, config_defaults)

    machine.create_vdisks(environment, config_defaults, cloud_image)
    _up_build_cloud_init_iso(
        machine, cloud_image, config_defaults, machines, password_hash=password_hash
    )

    typer.echo(f"Attempting to start virtual machine: {machine.vm_name}")
    if machine.deploy(
        machine.config_fpath,
        config_defaults,
        environment.get("libvirt_uri", DEFAULT_LIBVIRT_URI),
        os_variant=cloud_image.os_variant,
    ):
        typer.echo("Virtual machine deployment complete.")
        typer.echo()
        # Surface the one-time password (shown once) + an SSH hint, aligned
        # with createvm's output (issue #106). The plaintext is never logged.
        if password_plain:
            render_one_time_password(password_plain)
        render_ssh_hint(
            _machine_login_user(machine, config_defaults),
            _machine_static_ip(machine),
        )
    else:
        logger.error("Virtual machine installation failed.")
        raise typer.Exit(code=1)


@app.command()
def up(
    vm_name: str = typer.Argument(
        None,
        help="The vm_name of the manifest machine to boot. Omit and pass --all to boot every machine.",
    ),
    boot_all: bool = typer.Option(
        False,
        "--all",
        help="Boot every machine in the manifest sequentially (manifest order). Mutually exclusive with VM_NAME.",
    ),
) -> None:
    """Start a machine defined in the Lvlab.yml manifest.

    Creates the VM on first run (qcow2 disks -> cloud-init render ->
    ISO pack -> virt-install) or powers it on if it's shut off.
    Already-running VMs are a no-op. With ``--all``, every machine in
    the manifest is booted sequentially in manifest order.
    """
    if boot_all and vm_name:
        typer.echo("lvlab up: pass either a VM_NAME or --all, not both.")
        raise typer.Exit(code=1)
    if not boot_all and not vm_name:
        typer.echo("lvlab up: specify a VM_NAME, or pass --all to boot every machine.")
        raise typer.Exit(code=1)

    config = _load_config()
    environment, images, config_defaults, machines = config.as_tuple()

    if boot_all:
        if not machines:
            typer.echo("lvlab up --all: no machines in manifest.")
            return
        for machine_config in machines:
            _up_one(machine_config, environment, images, config_defaults, machines)
        return

    machine_config = config.get_machine(vm_name)
    if not machine_config:
        logger.error("Machine %s not found in manifest.", vm_name)
        return
    _up_one(machine_config, environment, images, config_defaults, machines)


def _up_one(
    machine_config: dict,
    environment: dict,
    images: dict,
    config_defaults: dict,
    machines: list[dict],
) -> None:
    """Boot one manifest machine — create on first run, power-on otherwise.

    The body of :func:`up` factored out so the single-VM and the
    ``--all`` paths share one implementation.
    """
    machine = Machine(
        machine_config, environment, config_defaults, networks=_host_networks()
    )
    libvirt_uri = environment.get("libvirt_uri", DEFAULT_LIBVIRT_URI)
    exists, status_state, _ = machine.exists_in_libvirt(libvirt_uri)

    if exists:
        _up_start_existing(machine, status_state, environment)
    else:
        _up_first_time_create(machine, environment, images, config_defaults, machines)


def _global_manifest_domain_names() -> set[str] | None:
    """Return the manifest's ``<vm_name>_<env>`` domain names, or ``None``.

    Used to populate the ``In manifest`` column of ``global show instances``.
    Returns ``None`` when no ``Lvlab.yml`` is present in the CWD (or it cannot
    be parsed), which signals the caller to omit the column entirely rather
    than render an all-``no`` column for a directory with no manifest.

    Returns:
        The set of namespaced domain names the manifest would create, or
        ``None`` when there is no usable manifest in the current directory.
    """
    try:
        parsed = parse_config()
    except (ConfigError, TypeError):
        return None
    if not parsed:
        return None
    environment, _, _, machines = parsed
    env_name = environment.get("name", "")
    return {f"{m['vm_name']}_{env_name}" for m in machines if m.get("vm_name")}


def _global_collect_instances(
    uris: list[str],
) -> tuple[list[tuple[str, str, DomInfo]], list[tuple[str, str]]]:
    """Enumerate every domain across ``uris`` with its cheap :class:`DomInfo`.

    Each connection is probed independently: one ``virsh list --all --name``
    plus one ``virsh dominfo`` per domain. A :class:`VirshError` anywhere in a
    connection's enumeration aborts only that connection — the URI is recorded
    as unreachable and the remaining connections are still processed.

    Args:
        uris: Ordered, de-duplicated connection URIs to enumerate.

    Returns:
        A ``(rows, skipped)`` pair. ``rows`` is a list of
        ``(uri, domain_name, DomInfo)`` triples in connection-then-domain
        order. ``skipped`` is a list of ``(uri, reason)`` pairs for the
        connections that could not be reached.
    """
    rows: list[tuple[str, str, DomInfo]] = []
    skipped: list[tuple[str, str]] = []
    for uri in uris:
        try:
            names = virsh_list_all_names(uri)
            for name in names:
                rows.append((uri, name, virsh_dominfo(uri, name)))
        except VirshError as exc:
            skipped.append((uri, str(exc)))
    return rows, skipped


def _global_console() -> Console:
    """Return a Console that does not truncate the instances table.

    Thin wrapper over the shared :func:`tkc_lvlab.utils.output.get_console`
    (issue #103): a non-interactive console is widened so long domain
    names and connection URIs aren't clipped, while a real terminal's
    width and a user-set ``COLUMNS`` are honored.
    """
    return get_console()


def _global_format_memory(max_memory_kib: int | None) -> str:
    """Render a ``Max memory`` KiB value as a compact human string.

    Returns ``-`` when the value is missing. Whole-MiB values render as
    ``<n> MiB`` (the common case for a libvirt domain); anything that is not a
    clean MiB multiple falls back to the raw ``<n> KiB``.
    """
    if max_memory_kib is None:
        return "-"
    if max_memory_kib % 1024 == 0:
        return f"{max_memory_kib // 1024} MiB"
    return f"{max_memory_kib} KiB"


def _global_build_table(
    rows: list[tuple[str, str, DomInfo]],
    manifest_names: set[str] | None,
) -> Table:
    """Build the Rich table for ``global show instances``.

    Args:
        rows: ``(uri, domain_name, DomInfo)`` triples from
            :func:`_global_collect_instances`.
        manifest_names: Namespaced manifest domain names, or ``None`` to omit
            the ``In manifest`` column (no manifest in CWD).

    Returns:
        A populated :class:`rich.table.Table` ready to print.
    """
    table = Table(title="Libvirt Instances")
    table.add_column("Name", style="bold")
    table.add_column("Connection (URI)")
    table.add_column("State")
    table.add_column("vCPUs", justify="right")
    table.add_column("Memory", justify="right")
    table.add_column("Autostart")
    table.add_column("Persistent")
    if manifest_names is not None:
        table.add_column("In manifest")

    for uri, name, info in rows:
        cells = [
            name,
            uri,
            info.state or "unknown",
            "-" if info.vcpus is None else str(info.vcpus),
            _global_format_memory(info.max_memory_kib),
            "yes" if info.autostart else "no",
            "yes" if info.persistent else "no",
        ]
        if manifest_names is not None:
            cells.append("yes" if name in manifest_names else "no")
        table.add_row(*cells)

    return table


@global_show_app.command("instances")
def global_show_instances(
    uris: list[str] = typer.Option(
        None,
        "--uri",
        "-u",
        help=(
            "Additional libvirt connection URI to include. Repeatable. "
            "qemu:///system and qemu:///session are always included."
        ),
    ),
) -> None:
    """Show every libvirt domain across connections in one table.

    Enumerates qemu:///system and qemu:///session (plus any --uri) and prints
    a table of cheap per-domain facts: name, connection, state, vCPUs, memory,
    autostart, and persistent. When an Lvlab.yml is present in the working
    directory, an "In manifest" column flags which domains the manifest would
    create.

    Only cheap reads are issued (one "virsh list --all" plus one "virsh
    dominfo" per domain). No running guest is stunned and no live CPU/disk/net
    stats are sampled. A connection that is unreachable (missing socket,
    permission denied, daemon down) is skipped with a dim note; the reachable
    ones are still shown.
    """
    # Preserve order while de-duplicating: the fixed pair first, then any
    # user-supplied URIs not already covered.
    seen: set[str] = set()
    ordered_uris: list[str] = []
    for uri in (*DEFAULT_GLOBAL_URIS, *(uris or [])):
        if uri not in seen:
            seen.add(uri)
            ordered_uris.append(uri)

    rows, skipped = _global_collect_instances(ordered_uris)
    manifest_names = _global_manifest_domain_names()

    console = _global_console()
    for uri, reason in skipped:
        console.print(f"[dim]Skipping unreachable connection {uri}: {reason}[/dim]")

    if not rows:
        console.print("No instances found on the reachable connection(s).")
        return

    console.print(_global_build_table(rows, manifest_names))


# Backwards-compatible aliases. ``pyproject.toml`` entry-point references
# ``run``; tests reference ``snapshot`` for the snapshot subcommand group.
# Typer instances are callable, so both aliases work the same way the original
# Click group objects did.
run = app
snapshot = snapshot_app


if __name__ == "__main__":
    app()
