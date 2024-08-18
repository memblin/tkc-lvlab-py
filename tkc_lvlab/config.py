import os
from jinja2 import Environment, PackageLoader, select_autoescape
from urllib.parse import urlparse

import click
import yaml


def generate_hosts(environment, config_defaults, machines):
    """Generte hosts file entry content from config

    Parses the manifest to create sets of /etc/hosts
    entries for each defined machine.

    Output created by rendering a jinja template
    with the data
    """
    env = Environment(loader=PackageLoader("tkc_lvlab"), autoescape=select_autoescape())

    hosts = []
    for machine in machines:
        if len(machine.get("interfaces", [])) > 0 and machine["interfaces"][0].get(
            "ip4", None
        ):
            hosts_entry = {
                "ip4": machine["interfaces"][0]["ip4"].split("/")[0],
                "fqdn": machine["hostname"] + "." + config_defaults["domain"],
                "hostname": machine["hostname"],
            }
            hosts.append(hosts_entry)

    config = {"env": environment, "defaults": config_defaults, "hosts": hosts}
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
