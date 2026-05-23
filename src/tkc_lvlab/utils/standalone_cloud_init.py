"""Cloud-init data builders for the standalone ``createvm`` script.

Phase 6 step 3. The existing :mod:`tkc_lvlab.utils.cloud_init` builds
``user-data`` and ``meta-data`` from a manifest-shaped dict that references
a single SSH public key via ``cloud_init.pubkey`` and pulls every other
field through a :class:`tkc_lvlab.utils.libvirt.Machine`. The standalone
``createvm`` workflow has no manifest — fields come from CLI args, SSH
keys are a discovered list, and a generated password phrase hash needs
to land on the VM as ``users[*].passwd``.

This module defines :class:`OneoffCloudInit`, a dataclass that holds the
explicit set of fields ``createvm`` resolves and exposes two rendering
methods that emit valid cloud-init ``user-data`` and ``meta-data``
strings. For ``network-config`` the existing
:class:`tkc_lvlab.utils.cloud_init.NetworkConfig` is fine — its render
path is already manifest-agnostic.

Templates land in ``tkc_lvlab/templates/`` (``user-data.oneoff.j2`` and
``meta-data.oneoff.j2``); both ship via the wheel because the
``pyproject.toml`` template glob already covers ``*.j2``.

Nothing here reads ``Lvlab.yml`` or talks to libvirt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jinja2 import Environment, PackageLoader, select_autoescape


_DEFAULT_SUDO = "ALL=(ALL) NOPASSWD:ALL"
_DEFAULT_SHELL = "/bin/bash"


@dataclass
class OneoffCloudInit:
    """Resolved fields for a one-off VM's cloud-init artifacts.

    Attributes:
        libvirt_vm_name: The libvirt domain name. For one-off VMs this is
            ``oneoff-<vm_name>`` per the Phase 6 architecture lock.
            Becomes the cloud-init ``instance-id`` (prefixed ``iid-``).
        hostname: Short hostname for the guest.
        fqdn: Fully-qualified domain name. May equal ``hostname`` when no
            domain is configured.
        username: First-boot account name (e.g. ``cloud-user``,
            ``tkcadmin``). Created by cloud-init in the ``users:`` list.
        ssh_public_keys: List of validated SSH public-key strings. Each
            becomes an entry under ``users[0].ssh_authorized_keys``.
            Typically populated from
            :func:`tkc_lvlab.utils.ssh_keys.discover_default_public_keys`
            plus any ``--public-key`` argument.
        password_hash: SHA-512-crypt hash from
            :func:`tkc_lvlab.utils.passwords.hash_password_sha512`. Rendered
            as ``users[0].passwd``.
        runcmd: Optional list of shell commands to run at first boot.
            Multi-line commands are emitted as ``|`` heredocs. Empty by
            default — one-off VMs are typically provisioned by hand
            after first boot.
        sudo: Sudoers fragment for the first-boot user. Defaults to
            passwordless ALL (typical lab setup).
        shell: Login shell. Defaults to ``/bin/bash``.
    """

    libvirt_vm_name: str
    hostname: str
    fqdn: str
    username: str
    ssh_public_keys: list[str]
    password_hash: str
    runcmd: list[str] = field(default_factory=list)
    sudo: str = _DEFAULT_SUDO
    shell: str = _DEFAULT_SHELL

    def render_user_data(self) -> str:
        """Render the cloud-init ``user-data`` document.

        Returns:
            A cloud-config YAML string starting with the
            ``#cloud-config`` magic line. Includes a ``users:`` entry
            with every key in :attr:`ssh_public_keys` and the
            :attr:`password_hash`, plus a ``runcmd:`` block when
            :attr:`runcmd` is non-empty.
        """
        env = Environment(
            loader=PackageLoader("tkc_lvlab"), autoescape=select_autoescape()
        )
        template = env.get_template("user-data.oneoff.j2")
        return template.render(config=self)

    def render_meta_data(self) -> str:
        """Render the cloud-init ``meta-data`` document.

        Returns:
            A two-line ``instance-id`` + ``local-hostname`` string,
            matching the shape cloud-init's NoCloud datasource expects.
        """
        env = Environment(
            loader=PackageLoader("tkc_lvlab"), autoescape=select_autoescape()
        )
        template = env.get_template("meta-data.oneoff.j2")
        return template.render(config=self)
