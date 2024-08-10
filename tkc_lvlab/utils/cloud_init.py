"""Module containing all things cloud-init"""

import click
import os
import re
import pycdlib
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
    nameservers: dict

    def __post_init__(self):
        if isinstance(self.network_version, int):
            self.network_version = NetworkVersion(self.network_version)

    def render_config(self) -> str:
        """Render a Jinja2 template with the data"""
        env = Environment(
            loader=PackageLoader("tkc_lvlab"), autoescape=select_autoescape()
        )

        if self.network_version == NetworkVersion.V1:
            template_file = "network-config.v1.j2"
        elif self.network_version == NetworkVersion.V2:
            template_file = "network-config.v2.j2"
        else:
            raise ValueError(f"Unsupported network version: {self.network_version}")

        template = env.get_template(template_file)
        return template.render(config=self)


@dataclass
class MetaData:
    """A cloud-init meta-data configuration"""

    hostname: str

    def render_config(self) -> str:
        """Render a Jinja2 template with the data"""
        env = Environment(
            loader=PackageLoader("tkc_lvlab"), autoescape=select_autoescape()
        )
        template_file = "meta-data.j2"
        template = env.get_template(template_file)
        return template.render(config=self)


@dataclass
class UserData:
    """A cloud-init user-data configuration"""

    cloud_init: dict
    hostname: str
    domain: str

    @staticmethod
    def _is_valid_ssh_public_key(key_str):
        patterns = {
            "ssh-rsa": r"^ssh-rsa\s+[A-Za-z0-9+/=]+\s*(?:[^\s]+)?$",
            "ssh-dss": r"^ssh-dss\s+[A-Za-z0-9+/=]+\s*(?:[^\s]+)?$",
            "ssh-ed25519": r"^ssh-ed25519\s+[A-Za-z0-9+/=]+\s*(?:[^\s]+)?$",
        }

        for key_type, pattern in patterns.items():
            if re.match(pattern, key_str):
                return (True, key_type)

        return False

    def __post_init__(self):
        pubkey_config = self.cloud_init.get("pubkey", None)
        if "~" in pubkey_config or "/" in pubkey_config:
            click.echo("Pubkey appears to be a file path, attempting to read contents")
            pubkey_path = os.path.expanduser(self.cloud_init.get("pubkey", None))
            if os.path.isfile(pubkey_path):
                with open(pubkey_path, "r", encoding="utf-8") as pubkey_file:
                    pubkey_content = pubkey_file.read()

                # Attempt some light pubkey syntax checking to guess if we got a key
                is_pubkey, pubkey_type = self._is_valid_ssh_public_key(pubkey_content)

                if is_pubkey:
                    click.echo(f"Successfully read {pubkey_type} pubkey")
                    # Swap the file path for the content
                    self.cloud_init["pubkey"] = pubkey_content.strip()
                else:
                    click.echo(
                        f"Read file contents does not appear to be an SSH pubkey"
                    )
        else:
            if self._is_valid_ssh_public_key(pubkey_config):
                click.echo("Pubkey appears to be a pubkey, using as-is")

    def render_config(self) -> str:
        """Render a Jinja2 template with the data"""
        env = Environment(
            loader=PackageLoader("tkc_lvlab"), autoescape=select_autoescape()
        )
        template_file = "user-data.j2"
        template = env.get_template(template_file)
        return template.render(config=self)


class CloudInitIso:
    """A Cloud init ISO"""

    def __init__(
        self,
        meta_data_fpath: str,
        user_data_fpath: str,
        network_config_fpath: str,
        iso_fpath: str,
    ):
        self.meta_data_fpath = meta_data_fpath
        self.user_data_fpath = user_data_fpath
        self.network_config_fpath = network_config_fpath
        self.fpath = iso_fpath

    def write(self, config_fpath):
        """Add the cloud-init config files and write our cloud-init data ISO"""

        try:
            iso = pycdlib.PyCdlib()
            iso.new(
                interchange_level=3, vol_ident="cidata", joliet=True, rock_ridge="1.09"
            )

            iso.add_file(
                self.meta_data_fpath,
                iso_path="/METADATA;1",
                rr_name="meta-data",
                joliet_path="/meta-data",
            )
            iso.add_file(
                self.user_data_fpath,
                iso_path="/USERDATA;1",
                rr_name="user-data",
                joliet_path="/user-data",
            )
            iso.add_file(
                self.network_config_fpath,
                iso_path="/NETCNFIG;1",
                rr_name="network-config",
                joliet_path="/network-config",
            )
            iso.write(self.fpath)
            iso.close()
            return True

        except Exception as e:
            click.echo(f"Error writting ISO: {e}")
            return False
