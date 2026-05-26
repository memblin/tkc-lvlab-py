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
