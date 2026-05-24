"""Manifest-side cloud-init artifact builders and ISO writer.

The three dataclasses (:class:`NetworkConfig`, :class:`MetaData`,
:class:`UserData`) render their respective cloud-init documents from
manifest-shaped dicts via Jinja2 templates packaged with the wheel.
:class:`CloudInitIso` packs the rendered files into a ``cidata.iso``
using :mod:`pycdlib` — no external ``genisoimage`` / ``mkisofs``
dependency.

The standalone ``createvm`` workflow has a sibling builder at
:mod:`tkc_lvlab.utils.standalone_cloud_init` that takes explicit
fields (multiple SSH keys, a password hash, an explicit username) and
renders different templates. The manifest version stays untouched
because long-time `Lvlab.yml` users rely on its current `cloud_init.pubkey`
scalar shape.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape
import pycdlib

from .._logging import get_logger


logger = get_logger(__name__)


class NetworkVersion(Enum):
    """cloud-init ``network-config`` schema versions.

    Two are emitted by lvlab today:

    - ``V1`` — ENI-style; used for Debian 11 (where the v2/netplan
        path stalls ``networking.service`` for ~5 minutes via the
        ifupdown DHCPv6 hang).
    - ``V2`` — netplan-style; the default for every other supported
        distro.
    """

    V1 = 1
    V2 = 2


@dataclass
class NetworkConfig:
    """Renders a cloud-init ``network-config`` document.

    Attributes:
        network_version: Schema selector. Accepts either a
            :class:`NetworkVersion` or a bare ``int`` (auto-coerced in
            ``__post_init__``).
        interfaces: List of interface dicts from the manifest. Each
            entry typically carries ``name``, ``ip4``, ``ip4gw``.
        nameservers: DNS configuration dict — ``search`` (list of
            domains) and ``addresses`` (list of resolver IPs).
    """

    network_version: NetworkVersion
    interfaces: list[dict[str, Any]]
    nameservers: dict[str, Any]

    def __post_init__(self) -> None:
        """Coerce a bare-int ``network_version`` to :class:`NetworkVersion`."""
        if isinstance(self.network_version, int):
            self.network_version = NetworkVersion(self.network_version)

    def render_config(self) -> str:
        """Render the version-appropriate ``network-config`` template.

        Returns:
            The rendered cloud-init network-config document.

        Raises:
            ValueError: If ``network_version`` is not :attr:`NetworkVersion.V1`
                or :attr:`NetworkVersion.V2`.
        """
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
    """Renders a cloud-init ``meta-data`` document.

    Attributes:
        libvirt_vm_name: The libvirt domain name. Used as the
            ``instance-id`` (prefixed ``iid-``) — cloud-init's NoCloud
            datasource keys re-runs off this.
        fqdn: The guest's fully-qualified domain name. Written as
            ``local-hostname``.
    """

    libvirt_vm_name: str
    fqdn: str

    def render_config(self) -> str:
        """Render the ``meta-data.j2`` template.

        Returns:
            The rendered cloud-init meta-data document.
        """
        env = Environment(
            loader=PackageLoader("tkc_lvlab"), autoescape=select_autoescape()
        )
        template_file = "meta-data.j2"
        template = env.get_template(template_file)
        return template.render(config=self)


@dataclass
class UserData:
    """Renders a cloud-init ``user-data`` document from a manifest config.

    Reads the manifest's ``cloud_init.pubkey`` field — a single key,
    either as a literal SSH public-key string or as a path on disk that
    gets read at construction time. The standalone ``createvm``
    workflow needs multiple keys + a password hash and uses
    :class:`tkc_lvlab.utils.standalone_cloud_init.OneoffCloudInit`
    instead.

    Attributes:
        cloud_init: Manifest ``cloud_init`` dict. Honors ``user``,
            ``pubkey``, ``sudo``, ``shell``, ``runcmd``, ``mounts``.
        hostname: Short guest hostname (used by the template).
        domain: Domain name (used by the template).
        fqdn: Fully-qualified hostname.
    """

    cloud_init: dict[str, Any]
    hostname: str
    domain: str
    fqdn: str

    @staticmethod
    def _is_valid_ssh_public_key(key_str: str) -> tuple[bool, str]:
        """Light SSH-key syntax check for the manifest pubkey field.

        Recognizes three classic key types (rsa, dss, ed25519). For a
        broader validator that handles ECDSA and hardware-backed keys,
        see :func:`tkc_lvlab.utils.ssh_keys.validate_public_key` —
        the standalone workflow's path that the manifest workflow
        will adopt in a follow-up.

        The match anchors at the start of the string and requires a
        recognized key-type prefix followed by a base64 blob. Anything
        after that — whitespace, multi-word comments, trailing
        newlines — is tolerated. SSH key comments are free-form (they
        can legitimately contain spaces, parens, anything up to
        end-of-line); the previous pattern required at most a single
        non-whitespace token there, which rejected real keys generated
        with multi-word ``-C`` comments and crashed the manifest
        ``up`` path.

        Args:
            key_str: Raw string to check.

        Returns:
            ``(True, key_type)`` on a match, or ``(False, "")`` on
            miss. The return shape is consistent so callers can
            unconditionally unpack — previously the legacy version
            returned a bare ``False`` on miss which crashed any
            caller doing tuple-unpack.
        """
        patterns = {
            "ssh-rsa": r"^ssh-rsa\s+[A-Za-z0-9+/=]+",
            "ssh-dss": r"^ssh-dss\s+[A-Za-z0-9+/=]+",
            "ssh-ed25519": r"^ssh-ed25519\s+[A-Za-z0-9+/=]+",
        }

        for key_type, pattern in patterns.items():
            if re.match(pattern, key_str):
                return (True, key_type)

        return (False, "")

    def __post_init__(self) -> None:
        """Resolve ``cloud_init.pubkey`` — read from disk when it's a path.

        The manifest may set ``cloud_init.pubkey`` either to a literal
        key string or to a path on disk. The path heuristic looks for
        ``"~"`` or ``"/"`` in the value. When path-like, the file is
        read; the contents are validated and the manifest value is
        rewritten in-place to the literal key.
        """
        pubkey_config = self.cloud_init.get("pubkey", None)
        if "~" in pubkey_config or "/" in pubkey_config:
            logger.debug(
                "Pubkey appears to be a file path, attempting to read contents"
            )
            pubkey_path = os.path.expanduser(self.cloud_init.get("pubkey", None))
            if os.path.isfile(pubkey_path):
                with open(pubkey_path, "r", encoding="utf-8") as pubkey_file:
                    pubkey_content = pubkey_file.read()

                is_pubkey, pubkey_type = self._is_valid_ssh_public_key(pubkey_content)

                if is_pubkey:
                    logger.debug("Successfully read %s pubkey", pubkey_type)
                    self.cloud_init["pubkey"] = pubkey_content.strip()
                else:
                    logger.warning(
                        "Read file contents does not appear to be an SSH pubkey"
                    )
        else:
            is_pubkey, _ = self._is_valid_ssh_public_key(pubkey_config)
            if is_pubkey:
                logger.debug("Pubkey appears to be a pubkey, using as-is")

    def render_config(self) -> str:
        """Render the ``user-data.j2`` template against this dataclass.

        Returns:
            The rendered cloud-init user-data document (starts with
            ``#cloud-config``).
        """
        env = Environment(
            loader=PackageLoader("tkc_lvlab"), autoescape=select_autoescape()
        )
        template_file = "user-data.j2"
        template = env.get_template(template_file)
        return template.render(config=self)


class CloudInitIso:
    """A cloud-init seed ISO (NoCloud datasource format).

    Packs ``meta-data``, ``user-data``, and ``network-config`` into a
    single ISO9660 image with Joliet + Rock Ridge extensions so
    cloud-init's NoCloud datasource picks them up at the canonical
    paths.

    Attributes:
        meta_data_fpath: Path to the rendered ``meta-data`` file.
        user_data_fpath: Path to the rendered ``user-data`` file.
        network_config_fpath: Path to the rendered ``network-config`` file.
        fpath: Output path for the generated ISO.
    """

    def __init__(
        self,
        meta_data_fpath: str,
        user_data_fpath: str,
        network_config_fpath: str,
        iso_fpath: str,
    ) -> None:
        """Store the input file paths and the target ISO path.

        Args:
            meta_data_fpath: Path to the rendered ``meta-data``.
            user_data_fpath: Path to the rendered ``user-data``.
            network_config_fpath: Path to the rendered ``network-config``.
            iso_fpath: Output path for the generated ISO.
        """
        self.meta_data_fpath = meta_data_fpath
        self.user_data_fpath = user_data_fpath
        self.network_config_fpath = network_config_fpath
        self.fpath = iso_fpath

    def write(self) -> bool:
        """Build and write the ISO.

        Creates a fresh :class:`pycdlib.PyCdlib` with ``interchange_level=3``,
        ``vol_ident="cidata"``, Joliet, and Rock Ridge "1.09" extensions,
        then adds the three input files at the names cloud-init
        expects. The output path comes from :attr:`fpath`.

        Returns:
            ``True`` on a successful write. ``False`` if pycdlib raised
            for any reason; the error is logged.
        """
        try:
            iso = pycdlib.PyCdlib()
            iso.new(
                interchange_level=3,
                vol_ident="cidata",
                joliet=True,
                rock_ridge="1.09",
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

        except Exception as e:  # pylint: disable=broad-except
            logger.error("Error writting ISO: %s", e)
            return False
