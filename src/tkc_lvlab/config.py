"""Manifest loading and ``/etc/hosts`` rendering for the lvlab CLI.

Every ``lvlab`` subcommand starts by loading ``Lvlab.yml`` from the current
working directory and unpacking it into the four pieces every command needs
(environment, images, defaults, machines). :func:`parse_config` is the
low-level reader; :class:`ConfigManager` wraps it to give each command a
single parsed view (and to expose a :meth:`ConfigManager.get_machine`
accessor) so the manifest is read once per command path rather than
re-parsed at each call site.

``/etc/hosts``-style rendering lives here too because the same manifest
data drives the standalone ``lvlab hosts`` output AND the
runcmd-injected guest snippet in
:meth:`tkc_lvlab.utils.libvirt.Machine.cloud_init` — keeping both
emitters in one module keeps them in sync.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jinja2 import Environment, PackageLoader, select_autoescape
import yaml

from .exceptions import ConfigError


def generate_hosts_entries(
    config_defaults: dict[str, Any], machines: list[dict[str, Any]]
) -> list[dict[str, str | None]]:
    """Build per-machine ``/etc/hosts`` entries from the manifest data.

    Each machine in ``machines`` contributes at most one entry, and only
    when its first interface declares a static ``ip4``. Machines without
    a static IP (DHCP-only) are skipped because we don't know what to
    point the entry at.

    Args:
        config_defaults: Manifest ``config_defaults`` dict. Honors
            ``domain`` for the optional FQDN suffix when a machine
            doesn't carry its own ``fqdn``.
        machines: Manifest ``machines`` list.

    Returns:
        A list of dicts with keys ``ip4``, ``fqdn``, and ``hostname``.
        ``ip4`` is the bare address (CIDR suffix stripped). ``fqdn`` may
        be ``None`` if neither an explicit ``fqdn`` nor a ``hostname`` +
        defaults ``domain`` pair is available.
    """
    hosts: list[dict[str, str | None]] = []
    for machine in machines:
        if len(machine.get("interfaces", [])) > 0 and machine["interfaces"][0].get(
            "ip4", None
        ):
            machine_fqdn = machine.get("fqdn", None)
            if (
                not machine_fqdn
                and machine.get("hostname", None)
                and config_defaults.get("domain", None)
            ):
                machine_fqdn = f'{machine.get("hostname", None) + "." + config_defaults.get("domain", None)}'

            hosts_entry: dict[str, str | None] = {
                "ip4": machine["interfaces"][0]["ip4"].split("/")[0],
                "fqdn": machine_fqdn,
                "hostname": machine["hostname"],
            }
            hosts.append(hosts_entry)
    return hosts


def parse_hosts_file(fpath: str) -> tuple[set[str], set[str]]:
    """Parse an existing ``/etc/hosts``-style file into IPs and names sets.

    Used by the ``--append`` mode of ``lvlab hosts`` to skip entries that
    are already present.

    Loopback lines (IPs starting with ``127.`` or ``::1``) are skipped
    entirely so a VM whose hostname matches the workstation's own
    hostname doesn't get blocked from being added.

    Args:
        fpath: Filesystem path to the hosts-format file. A missing path
            returns two empty sets (no error) so the caller can treat
            every candidate as new.

    Returns:
        ``(ips, names)``:
            - ``ips`` — set of non-loopback IPs already present.
            - ``names`` — set of hostnames + aliases on non-loopback
                lines, lowercased.
    """
    ips: set[str] = set()
    names: set[str] = set()

    if not os.path.exists(fpath):
        return ips, names

    with open(fpath, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            tokens = line.split()
            if len(tokens) < 2:
                continue
            ip = tokens[0]
            entry_names = [t.lower() for t in tokens[1:]]
            if ip.startswith("127.") or ip == "::1":
                continue
            ips.add(ip)
            names.update(entry_names)

    return ips, names


def generate_hosts(
    environment: dict[str, Any],
    config_defaults: dict[str, Any],
    machines: list[dict[str, Any]],
    heredoc: bool | None = None,
) -> str:
    """Render the ``hosts.j2`` template against the current manifest.

    Args:
        environment: The manifest's ``environment[0]`` dict.
        config_defaults: The manifest's ``config_defaults`` dict.
        machines: The manifest's ``machines`` list.
        heredoc: ``True`` selects the heredoc-friendly rendering mode the
            cloud-init ``runcmd`` path uses; ``False``/``None`` selects
            the stdout-friendly mode the ``lvlab hosts`` command uses.

    Returns:
        The rendered hosts snippet as a string.
    """
    env = Environment(loader=PackageLoader("tkc_lvlab"), autoescape=select_autoescape())

    hosts = generate_hosts_entries(config_defaults, machines)

    config = {
        "env": environment,
        "defaults": config_defaults,
        "hosts": hosts,
        "heredoc": heredoc,
    }
    template_file = "hosts.j2"
    template = env.get_template(template_file)

    return template.render(config=config)


def parse_config(
    fpath: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    """Read a Lvlab.yml manifest and unpack it into the four pieces every command needs.

    The manifest schema is a single-element ``environment`` list plus an
    ``images`` map. This function picks ``environment[0]`` (the schema's
    one-environment-per-file convention) and returns the four constituent
    parts as a tuple.

    Args:
        fpath: Path to the manifest. Defaults to ``"Lvlab.yml"`` in the
            current working directory.

    Returns:
        ``(environment, images, config_defaults, machines)`` on success.
        ``None`` if the file does not exist — legacy soft-fail behavior
        that callers rely on, kept distinct from a structural error.

    Raises:
        yaml.YAMLError: The manifest content was not valid YAML.
        ConfigError: The manifest exists and parsed, but is structurally
            invalid — it is not a mapping, lacks a non-empty ``environment``
            list, or lacks an ``images`` section. (A *missing* file is not a
            ``ConfigError``; it returns ``None``.)
    """
    if fpath is None:
        fpath = "Lvlab.yml"

    if not os.path.isfile(fpath):
        return None

    with open(fpath, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        raise ConfigError(
            f"Manifest '{fpath}' is not a mapping; expected top-level "
            "'environment' and 'images' keys."
        )

    environments = config.get("environment")
    if not isinstance(environments, list) or not environments:
        raise ConfigError(
            f"Manifest '{fpath}' must define a non-empty 'environment' list."
        )

    if "images" not in config:
        raise ConfigError(f"Manifest '{fpath}' is missing the 'images' section.")

    environment = environments[0]
    images = config["images"]
    config_defaults = environment.get("config_defaults", {})
    machines = environment.get("machines", {})

    return (environment, images, config_defaults, machines)


class ConfigManager:
    """Load, validate, and expose a single ``Lvlab.yml`` manifest.

    A thin wrapper around :func:`parse_config` that reads the manifest once
    and exposes its four constituent pieces (``environment``, ``images``,
    ``config_defaults``, ``machines``) as properties plus a
    :meth:`get_machine` convenience accessor. The intent is to give every
    command a single parsed view of the manifest instead of re-reading the
    file at each call site (see the duplicate parse that used to live in
    :meth:`tkc_lvlab.utils.libvirt.Machine.cloud_init`).

    The two manifest-absence outcomes from :func:`parse_config` are kept as
    **distinct, documented states**:

    - **Missing file** — the soft path. :attr:`loaded` is ``False`` and the
        four section properties return empty values. Callers that need to
        refuse rather than guess should check :attr:`loaded`.
    - **Structurally invalid file** — :func:`parse_config` raises
        :class:`tkc_lvlab.exceptions.ConfigError`, which propagates out of the
        constructor unchanged. (Malformed YAML still surfaces as
        ``yaml.YAMLError`` from the underlying loader.)

    Args:
        fpath: Path to the manifest. Defaults to ``"Lvlab.yml"`` in the
            current working directory when ``None``.

    Attributes:
        fpath: The manifest path this manager loaded (or attempted to load).
        loaded: ``True`` when the manifest existed and parsed; ``False`` for
            the soft missing-file path.

    Raises:
        ConfigError: The manifest exists but is structurally invalid (not a
            mapping, no non-empty ``environment`` list, or no ``images``
            section). Propagated from :func:`parse_config`.
        yaml.YAMLError: The manifest content was not valid YAML.
    """

    def __init__(self, fpath: str | None = None) -> None:
        self.fpath: str | None = fpath
        self._load(parse_config(fpath))

    @classmethod
    def from_parsed(
        cls,
        parsed: (
            tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]
            | None
        ),
        fpath: str | None = None,
    ) -> "ConfigManager":
        """Wrap an already-parsed manifest tuple in a manager.

        This is the seam the CLI uses: it lets a command call the
        module-level :func:`parse_config` (which tests patch at the
        ``tkc_lvlab.cli`` import boundary) and hand the result here, so the
        manager never re-reads the file and the existing CLI test seam stays
        intact.

        Args:
            parsed: The :func:`parse_config` return value — the four-tuple
                ``(environment, images, config_defaults, machines)`` on
                success, or ``None`` for the soft missing-file path.
            fpath: The manifest path that produced ``parsed`` (recorded on
                :attr:`fpath`; purely informational).

        Returns:
            A :class:`ConfigManager` reflecting ``parsed``.
        """
        manager = cls.__new__(cls)
        manager.fpath = fpath
        manager._load(parsed)
        return manager

    def _load(
        self,
        parsed: (
            tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]
            | None
        ),
    ) -> None:
        """Populate the section state from a :func:`parse_config` result."""
        self.loaded: bool = parsed is not None
        if parsed is None:
            self._environment: dict[str, Any] = {}
            self._images: dict[str, Any] = {}
            self._config_defaults: dict[str, Any] = {}
            self._machines: list[dict[str, Any]] = []
        else:
            (
                self._environment,
                self._images,
                self._config_defaults,
                self._machines,
            ) = parsed

    @property
    def environment(self) -> dict[str, Any]:
        """The manifest's ``environment[0]`` dict (``{}`` when not loaded)."""
        return self._environment

    @property
    def images(self) -> dict[str, Any]:
        """The manifest's ``images`` map (``{}`` when not loaded)."""
        return self._images

    @property
    def config_defaults(self) -> dict[str, Any]:
        """The manifest's ``config_defaults`` block (``{}`` when not loaded)."""
        return self._config_defaults

    @property
    def machines(self) -> list[dict[str, Any]]:
        """The manifest's ``machines`` list (``[]`` when not loaded)."""
        return self._machines

    def as_tuple(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        """Return the four sections as the legacy :func:`parse_config` tuple.

        Provided so call sites that unpack
        ``environment, images, config_defaults, machines`` keep their exact
        existing shape while sourcing the data from this manager.

        Returns:
            ``(environment, images, config_defaults, machines)``.
        """
        return (
            self._environment,
            self._images,
            self._config_defaults,
            self._machines,
        )

    def get_machine(self, vm_name: str) -> dict[str, Any] | None:
        """Find a machine dict by its ``vm_name``.

        Args:
            vm_name: The short name to match against each machine entry's
                ``vm_name`` field.

        Returns:
            The matching machine dict, or ``None`` if no machine in the
            manifest has the requested ``vm_name``.
        """
        for machine in self._machines:
            if machine.get("vm_name", None) == vm_name:
                return machine
        return None


# ---------------------------------------------------------------------------
# Layered host config (#138)
# ---------------------------------------------------------------------------
#
# ``createvm`` needs host-wide defaults — chiefly per-network gateway/DNS for
# bridge networks (#136) — without retyping flags on every invocation. The
# config is layered, lowest precedence first: ``/etc/Lvlab.yml`` (host-wide
# base) → ``~/.Lvlab.yml`` (per-user) → ``./Lvlab.yml`` (project, CWD) → an
# explicit ``--config`` path. Each higher layer deep-merges over the lower
# ones, so a project manifest can override one nested field of a user/host
# default while inheriting the rest.
#
# This path is deliberately separate from :func:`parse_config` /
# :class:`ConfigManager`: a host-wide ``/etc/Lvlab.yml`` legitimately carries
# only ``networks:`` / ``images:`` (no ``environment``/``machines``), which the
# strict manifest validation would reject. Failures raise ``ValueError`` to
# match ``createvm``'s error idiom (its command boundary maps ``ValueError`` ->
# a clean ``_fail``), not the manifest path's ``ConfigError``.

SYSTEM_CONFIG_DIR = Path("/etc")
CONFIG_FILENAMES: tuple[str, ...] = ("Lvlab.yml", "Lvlab.yaml")
# The per-user layer is a home-directory dotfile (``~/.Lvlab.yml``), so it has
# its own dotted filenames rather than the bare ``Lvlab.yml`` used in ``/etc``
# and the project directory.
USER_CONFIG_FILENAMES: tuple[str, ...] = (".Lvlab.yml", ".Lvlab.yaml")


@dataclass(frozen=True)
class NetworkDefaults:
    """Per-network DNS/gateway/search defaults from a ``networks:`` entry.

    Populated from one entry of the ``networks:`` map (e.g. a ``vlan10``
    bridge). All fields are optional; an unset field means "no host default,
    fall back to the next precedence level" (a CLI flag, NAT self-derivation,
    or the #136 "bridge needs gateway+dns" error).

    Attributes:
        gateway: Gateway IPv4 address for the network, or ``None``.
        dns: DNS server addresses, or ``None`` when unset.
        search: DNS search domains, or ``None`` when unset.
    """

    gateway: str | None = None
    dns: list[str] | None = None
    search: list[str] | None = None


@dataclass(frozen=True)
class HostConfig:
    """The layered ``Lvlab.yml`` view that ``createvm`` consumes.

    Built by :func:`load_host_config` from the ``/etc`` -> CWD -> ``--config``
    layer stack. Only the sections ``createvm`` needs are surfaced; the
    manifest's ``environment``/``machines`` are not parsed here (that's the
    :class:`ConfigManager` path).

    Attributes:
        images: The merged ``images:`` map (``{}`` when none of the layers
            define one). Fed to
            :func:`tkc_lvlab.utils.catalog.resolve_catalog`.
        networks: The per-network defaults map, keyed by network name.
        default_network: The configured ``default_network`` (used when neither
            ``--network`` nor a ``NETWORK,IP`` ``--ip4`` names one), or ``None``.
        default_vm_username: The host-wide first-boot account name (used when an
            image entry doesn't pin an explicit ``username:``), or ``None``.
        runcmd: Host-wide cloud-init ``runcmd`` commands run at first boot.
            The highest-precedence layer that sets ``runcmd`` wins wholesale
            (deep-merge replaces lists), so identical commands in ``/etc`` and
            ``~`` don't run twice. Empty when no layer sets it.
        sources: The config files that contributed, lowest precedence first.
    """

    images: dict[str, Any] = field(default_factory=dict)
    networks: dict[str, NetworkDefaults] = field(default_factory=dict)
    default_network: str | None = None
    default_vm_username: str | None = None
    runcmd: list[str] = field(default_factory=list)
    sources: list[Path] = field(default_factory=list)

    def network_defaults(self, name: str) -> NetworkDefaults | None:
        """Return the configured defaults for ``name``, or ``None`` if absent.

        Args:
            name: The libvirt network name to look up.

        Returns:
            The matching :class:`NetworkDefaults`, or ``None`` when no
            ``networks:`` entry names that network.
        """
        return self.networks.get(name)


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` onto ``base``, returning a new dict.

    Where both layers hold a mapping at the same key, the mappings merge
    recursively — so a higher-precedence layer can override a single nested
    field (e.g. one network's ``dns``) while inheriting its siblings. For any
    other value type the ``overlay`` value replaces the ``base`` value
    wholesale. Neither input is mutated.

    Args:
        base: The lower-precedence mapping.
        overlay: The higher-precedence mapping whose values win on a clash.

    Returns:
        A new merged dict.
    """
    merged: dict[str, Any] = dict(base)
    for key, overlay_value in overlay.items():
        base_value = merged.get(key)
        if isinstance(base_value, dict) and isinstance(overlay_value, dict):
            merged[key] = deep_merge(base_value, overlay_value)
        else:
            merged[key] = overlay_value
    return merged


def _first_config_in(
    directory: Path, filenames: tuple[str, ...] = CONFIG_FILENAMES
) -> Path | None:
    """Return the first readable ``Lvlab.yml``/``Lvlab.yaml`` in ``directory``."""
    for filename in filenames:
        candidate = directory / filename
        if candidate.is_file() and os.access(candidate, os.R_OK):
            return candidate
    return None


def host_config_layers(
    config_path: str | Path | None = None,
    *,
    system_dir: Path = SYSTEM_CONFIG_DIR,
    home_dir: Path | None = None,
    cwd: Path | None = None,
) -> list[Path]:
    """Return the config files contributing to the layered host config.

    Lowest precedence first: ``<system_dir>/Lvlab.yml`` (host-wide base), then
    ``<home_dir>/.Lvlab.yml`` (per-user dotfile), then ``<cwd>/Lvlab.yml``
    (project), then an explicit ``config_path``. Files that don't exist are
    skipped silently; an explicit ``config_path`` that doesn't exist is an
    error (the operator asked for a specific file).

    Args:
        config_path: An explicit ``--config`` path, or ``None``.
        system_dir: Directory holding the host-wide config (``/etc`` in
            production; a test seam otherwise).
        home_dir: Directory holding the per-user dotfile (defaults to the real
            ``~``; a test seam otherwise).
        cwd: Directory to treat as the project root (defaults to the real CWD).

    Returns:
        The existing layer files, ordered lowest precedence first (so a later
        deep-merge of each over the running result yields the right winner).

    Raises:
        ValueError: ``config_path`` was given but the file does not exist.
    """
    home_dir = Path.home() if home_dir is None else home_dir
    cwd = Path.cwd() if cwd is None else cwd
    layers: list[Path] = []

    system = _first_config_in(system_dir)
    if system is not None:
        layers.append(system)

    user = _first_config_in(home_dir, USER_CONFIG_FILENAMES)
    if user is not None:
        layers.append(user)

    project = _first_config_in(cwd)
    if project is not None:
        layers.append(project)

    if config_path is not None:
        explicit = Path(config_path)
        if not explicit.is_file():
            raise ValueError(f"Config file '{config_path}' does not exist.")
        layers.append(explicit)

    return layers


def _load_config_mapping(path: Path) -> dict[str, Any]:
    """Read one config file into a mapping (lenient: empty file -> ``{}``)."""
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Found '{path}' but couldn't parse it: {exc}") from exc
    if content is None:
        return {}
    if not isinstance(content, dict):
        raise ValueError(f"Config file '{path}' must contain a YAML mapping.")
    return content


def _normalize_optional_str_list(raw: Any, key: str) -> list[str] | None:
    """Coerce a scalar/list YAML value into ``list[str] | None``."""
    if raw is None:
        return None
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return list(raw)
    raise ValueError(f"'{key}' must be a string or a list of strings.")


def parse_networks(raw: Any) -> dict[str, NetworkDefaults]:
    """Parse the ``networks:`` section into a map of :class:`NetworkDefaults`.

    Each value may set ``gateway`` (scalar), ``dns`` (scalar or list), and
    ``search`` (scalar or list). An entry of ``null``/empty is a valid
    no-defaults placeholder.

    Args:
        raw: The raw ``networks:`` value (``None`` when absent).

    Returns:
        ``{network_name: NetworkDefaults}`` (empty when ``raw`` is ``None``).

    Raises:
        ValueError: ``raw`` is not a mapping, an entry is not a mapping, or a
            ``dns``/``search`` value is neither a string nor a list of strings.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            "The 'networks' section must be a mapping of network name to its defaults."
        )
    networks: dict[str, NetworkDefaults] = {}
    for name, entry in raw.items():
        key = str(name)
        if entry is None:
            networks[key] = NetworkDefaults()
            continue
        if not isinstance(entry, dict):
            raise ValueError(
                f"Network '{key}' must be a mapping of gateway/dns/search values."
            )
        raw_gateway = entry.get("gateway")
        gateway = None if raw_gateway in (None, "") else str(raw_gateway)
        dns = _normalize_optional_str_list(entry.get("dns"), f"networks.{key}.dns")
        search = _normalize_optional_str_list(
            entry.get("search"), f"networks.{key}.search"
        )
        networks[key] = NetworkDefaults(gateway=gateway, dns=dns, search=search)
    return networks


def parse_runcmd(raw: Any) -> list[str]:
    """Parse the ``runcmd:`` section into a list of command strings.

    Each entry is a shell command cloud-init runs at first boot; a multi-line
    string is rendered as a ``|`` heredoc by the user-data template.

    Args:
        raw: The raw ``runcmd:`` value (``None`` when absent).

    Returns:
        The list of command strings (empty when ``raw`` is ``None``).

    Raises:
        ValueError: ``raw`` is not a list, or any entry is not a string.
    """
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError("The 'runcmd' value must be a list of command strings.")
    return list(raw)


def load_host_config(
    config_path: str | Path | None = None,
    *,
    system_dir: Path = SYSTEM_CONFIG_DIR,
    home_dir: Path | None = None,
    cwd: Path | None = None,
) -> HostConfig:
    """Discover, layer, and parse the host + user + project config for ``createvm``.

    Resolves ``/etc/Lvlab.yml`` (base) -> ``~/.Lvlab.yml`` (user) ->
    ``./Lvlab.yml`` (CWD) -> an explicit ``--config`` path and deep-merges them
    (higher precedence wins per key), then extracts the ``images:`` map, the
    ``networks:`` per-network defaults, ``default_network``,
    ``default_vm_username``, and ``runcmd``.

    Args:
        config_path: An explicit ``--config`` path, or ``None`` to use only
            the discovered ``/etc`` + ``~`` + CWD layers.
        system_dir: Directory holding the host-wide config (test seam).
        home_dir: Directory holding the per-user dotfile (test seam).
        cwd: Directory to treat as the project root (test seam).

    Returns:
        A :class:`HostConfig`. When no layer exists, every section is empty.

    Raises:
        ValueError: An explicit ``config_path`` is missing, a layer is not
            valid YAML / not a mapping, or the ``images:``/``networks:`` /
            ``default_network`` sections are structurally invalid.
    """
    layers = host_config_layers(
        config_path, system_dir=system_dir, home_dir=home_dir, cwd=cwd
    )

    merged: dict[str, Any] = {}
    for path in layers:
        merged = deep_merge(merged, _load_config_mapping(path))

    images = merged.get("images") or {}
    if not isinstance(images, dict):
        raise ValueError("The 'images' section must be a mapping.")

    default_network = merged.get("default_network")
    if default_network is not None and not isinstance(default_network, str):
        raise ValueError("The 'default_network' value must be a string.")

    default_vm_username = merged.get("default_vm_username")
    if default_vm_username is not None:
        if not isinstance(default_vm_username, str) or not default_vm_username.strip():
            raise ValueError(
                "The 'default_vm_username' value must be a non-empty string."
            )
        default_vm_username = default_vm_username.strip()

    return HostConfig(
        images=images,
        networks=parse_networks(merged.get("networks")),
        default_network=default_network,
        default_vm_username=default_vm_username,
        runcmd=parse_runcmd(merged.get("runcmd")),
        sources=layers,
    )


def parse_file_from_url(url: str) -> str:
    """Return the basename of the path component of a URL.

    Strips query strings and fragments; returns ``""`` for URLs with no
    path. Used to derive the on-disk filename for downloaded cloud
    images and checksum files.

    Args:
        url: Any URL string.

    Returns:
        Basename of the URL's path. e.g.
        ``"https://example.invalid/cloud/img.qcow2?v=1"`` →
        ``"img.qcow2"``.
    """
    parsed_url = urlparse(url)
    filename = os.path.basename(parsed_url.path)

    return filename
