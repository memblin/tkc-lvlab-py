"""Module containing all things cloud-init"""

import click
import yaml
import json
from dataclasses import dataclass
from enum import Enum
from jinja2 import Environment, PackageLoader, select_autoescape


class NetworkVersion(Enum):
    V1 = 1
    V2 = 2


@dataclass
class NetworkConfig:
    """A cloud-init network-config"""
    network_version: NetworkVersion
    interfaces: list

    def __post_init__(self):
        if isinstance(self.network_version, int):
            self.network_version = NetworkVersion(self.network_version)

    def render_config(self, template_dir: str = 'templates') -> str:
        """Render a Jinja2 template with the data"""
        env = Environment(
            loader=PackageLoader("tkc_lvlab"),
            autoescape=select_autoescape()
        )

        if self.network_version == NetworkVersion.V1:
            template_file = 'network-config.v1.j2'
        elif self.network_version == NetworkVersion.V2:
            template_file = 'network-config.v2.j2'
        else:
            raise ValueError(f"Unsupported network version: {self.network_version}")
        
        template = env.get_template(template_file)
        return template.render(config=self)


@dataclass
class MetaData:
    """A cloud-init meta-data configuration"""
    hostname: str

    def render_config(self, template_dir: str = 'templates') -> str:
        """Render a Jinja2 template with the data"""
        env = Environment(
            loader=PackageLoader("tkc_lvlab"),
            autoescape=select_autoescape()
        )
        template_file = 'meta-data.j2'
        template = env.get_template(template_file)
        return template.render(config=self)
