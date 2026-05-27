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

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import yaml
from jinja2 import Environment, PackageLoader, select_autoescape

from ..exceptions import CloudInitError

_DEFAULT_SUDO = "ALL=(ALL) NOPASSWD:ALL"
_DEFAULT_SHELL = "/bin/bash"

# Placeholders a ``user_data:`` override may reference. ``createvm`` fills
# these from the resolved create context; an override naming anything else is
# a hard error (see ``render_user_data_override``) rather than a silent blank.
USER_DATA_PLACEHOLDERS: tuple[str, ...] = (
    "vm_name",
    "vm_hostname",
    "default_vm_username",
    "password_hash",
)


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


def render_user_data_override(
    user_data: Mapping[str, Any],
    *,
    context: Mapping[str, Any],
    authorized_keys: Sequence[str] = (),
    runcmd_prefix: Sequence[str] = (),
) -> str:
    """Render a layered-config ``user_data:`` block into ``user-data``.

    Modeled on the lvscripts user-data override (which named the key
    ``users_data``; tkc-lvlab uses the cloud-init-aligned ``user_data``): the
    override is a full cloud-config mapping that the operator owns. Every string
    value is ``{placeholder}``-substituted from ``context`` (see
    :data:`USER_DATA_PLACEHOLDERS`); discovered SSH keys are then appended to
    each user's ``ssh_authorized_keys`` (so an operator's ``~/.ssh`` keys land
    alongside any keys the override hard-codes); and ``runcmd_prefix`` (the
    host-wide top-level ``runcmd:``) is prepended ahead of the override's own
    ``runcmd`` so host-wide bootstrap (CA installs, etc.) runs first.

    Args:
        user_data: The raw ``user_data:`` mapping from the layered config.
        context: Placeholder values; keys should be a subset of
            :data:`USER_DATA_PLACEHOLDERS`.
        authorized_keys: Discovered/``--public-key`` SSH keys to append to each
            user's ``ssh_authorized_keys`` (deduped against keys already there).
        runcmd_prefix: Commands to run before the override's own ``runcmd``.

    Returns:
        A ``#cloud-config`` YAML string ready to write as ``user-data``.

    Raises:
        CloudInitError: ``user_data`` is not a mapping, references an unknown
            placeholder, or carries a non-list ``runcmd``/``ssh_authorized_keys``.
    """
    if not isinstance(user_data, Mapping):
        raise CloudInitError("'user_data' must be a YAML mapping.")

    rendered = _render_value(dict(user_data), dict(context))
    if not isinstance(rendered, dict):  # pragma: no cover - mapping in => dict out
        raise CloudInitError("'user_data' must render to a YAML mapping.")

    _append_authorized_keys(rendered, list(authorized_keys))

    if runcmd_prefix:
        existing = rendered.get("runcmd") or []
        if not isinstance(existing, list):
            raise CloudInitError("'user_data.runcmd' must be a list of commands.")
        rendered["runcmd"] = list(runcmd_prefix) + existing

    text = yaml.safe_dump(rendered, sort_keys=False, width=float("inf"))
    if not text.endswith("\n"):
        text += "\n"
    return "#cloud-config\n" + text


def user_data_supplies_keys(user_data: Mapping[str, Any]) -> bool:
    """Return ``True`` if ``user_data`` already declares any SSH key.

    Lets ``createvm`` relax its "no way to log in" refusal: an override that
    hard-codes ``ssh_authorized_keys`` is a valid login path even when no key
    is discovered from ``~/.ssh`` and none is passed via ``--public-key``.

    Args:
        user_data: The raw ``user_data:`` mapping.

    Returns:
        ``True`` when at least one ``users[*].ssh_authorized_keys`` is non-empty.
    """
    users = user_data.get("users")
    if not isinstance(users, list):
        return False
    return any(
        isinstance(user, Mapping) and user.get("ssh_authorized_keys") for user in users
    )


def _append_authorized_keys(
    document: dict[str, Any], authorized_keys: list[str]
) -> None:
    """Append ``authorized_keys`` to every user's ``ssh_authorized_keys`` (deduped)."""
    if not authorized_keys:
        return
    users = document.get("users")
    if not isinstance(users, list):
        return
    for user in users:
        if not isinstance(user, dict):
            continue
        key_list = user.setdefault("ssh_authorized_keys", [])
        if not isinstance(key_list, list):
            raise CloudInitError(
                "'user_data' ssh_authorized_keys must be a list when set."
            )
        for key in authorized_keys:
            if key not in key_list:
                key_list.append(key)


def _render_value(value: Any, context: dict[str, Any]) -> Any:
    """Recursively ``{placeholder}``-substitute every string in ``value``."""
    if isinstance(value, dict):
        return {
            _render_value(key, context): _render_value(item, context)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_render_value(item, context) for item in value]
    if isinstance(value, str):
        return _render_string(value, context)
    return value


def _render_string(template: str, context: dict[str, Any]) -> Any:
    """Substitute placeholders in one string.

    A whole-string ``{key}`` returns the raw context value (preserving its
    type); embedded placeholders use ``format_map``. An unknown placeholder is
    a :class:`CloudInitError`, never a silent blank.
    """
    whole = re.fullmatch(r"\{([a-zA-Z_]\w*)\}", template)
    if whole and whole.group(1) in context:
        return context[whole.group(1)]
    try:
        return template.format_map(_StrictFormatDict(context))
    except KeyError as exc:
        raise CloudInitError(
            f"Unknown 'user_data' placeholder '{exc.args[0]}'. "
            f"Known placeholders: {', '.join(USER_DATA_PLACEHOLDERS)}."
        ) from exc


class _StrictFormatDict(dict):
    """``format_map`` backing dict that raises ``KeyError`` on a missing key."""

    def __missing__(self, key: str) -> Any:
        raise KeyError(key)
