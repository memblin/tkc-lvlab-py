"""Manifest loading and ``/etc/hosts`` rendering for the lvlab CLI.

Every ``lvlab`` subcommand starts by calling :func:`parse_config` to read
``Lvlab.yml`` from the current working directory and unpack it into the
four pieces every command needs (environment, images, defaults,
machines).

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
            if not machine_fqdn:
                if machine.get("hostname", None) and config_defaults.get(
                    "domain", None
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
        that callers rely on. Raises :class:`yaml.YAMLError` on malformed
        YAML.

    Raises:
        yaml.YAMLError: The manifest content was not valid YAML.
    """
    if fpath is None:
        fpath = "Lvlab.yml"

    if os.path.isfile(fpath):
        with open(fpath, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        environment = config["environment"][0]
        images = config["images"]
        config_defaults = environment.get("config_defaults", {})
        machines = environment.get("machines", {})

        return (environment, images, config_defaults, machines)

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
