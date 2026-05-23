import os
from jinja2 import Environment, PackageLoader, select_autoescape
from urllib.parse import urlparse

import click
import yaml


def generate_hosts_entries(config_defaults, machines):
    """Build the list of /etc/hosts entries for the configured machines.

    Returns a list of dicts with keys: ip4, fqdn, hostname. Entries are
    only emitted for machines whose first interface defines an ip4 address.
    """
    hosts = []
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

            hosts_entry = {
                "ip4": machine["interfaces"][0]["ip4"].split("/")[0],
                "fqdn": machine_fqdn,
                "hostname": machine["hostname"],
            }
            hosts.append(hosts_entry)
    return hosts


def parse_hosts_file(fpath):
    """Parse an /etc/hosts-style file into a structured view.

    Returns a tuple of (ips, names) where:
      - ips: set of IP addresses already present (loopback IPs excluded so
        a VM whose hostname matches the workstation's own hostname does not
        get blocked from being added).
      - names: set of hostnames and aliases present on non-loopback lines
        (lowercased).

    Comments (lines starting with '#' or trailing '#' content) and blank lines
    are ignored. If the file does not exist this returns two empty sets so the
    caller treats every candidate as new.
    """
    ips = set()
    names = set()

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
            # Skip loopback entries so they cannot block a real VM record.
            if ip.startswith("127.") or ip == "::1":
                continue
            ips.add(ip)
            names.update(entry_names)

    return ips, names


def generate_hosts(environment, config_defaults, machines, heredoc=None):
    """Generte hosts file entry content from config

    Parses the manifest to create sets of /etc/hosts
    entries for each defined machine.

    Output created by rendering a jinja template
    with the data
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


def parse_config(fpath=None):
    """Read config file"""

    if fpath == None:
        fpath = "Lvlab.yml"

    if os.path.isfile(fpath):
        with open(fpath, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        environment = config["environment"][0]
        images = config["images"]
        config_defaults = environment.get("config_defaults", {})
        machines = environment.get("machines", {})

        return (environment, images, config_defaults, machines)


def parse_file_from_url(url):
    """Return the filename from the end of a URL"""
    parsed_url = urlparse(url)
    filename = os.path.basename(parsed_url.path)

    return filename
