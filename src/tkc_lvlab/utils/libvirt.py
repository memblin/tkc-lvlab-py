"""Manifest-side ``Machine`` class and lookup helpers.

This module owns the manifest-driven side of lvlab — every command in
:mod:`tkc_lvlab.cli` constructs a :class:`Machine` from the
parsed-manifest tuple and dispatches operations against
``self.libvirt_vm_name``. The hypervisor side is invoked via
:mod:`tkc_lvlab.utils.virsh` (a thin ``subprocess.run`` wrapper); no
``libvirt-python`` import lives in this module — Phase 2 removed that
C-extension dependency.

The standalone one-off workflow (``createvm`` / ``deletevm``) does not
use this module — it talks to virsh directly via the helpers in
:mod:`tkc_lvlab.utils.virsh`,
:mod:`tkc_lvlab.utils.snapshot_cleanup`,
:mod:`tkc_lvlab.utils.network`, and
:mod:`tkc_lvlab.utils.standalone_cloud_init`.
"""

from __future__ import annotations

import glob
import os
import subprocess
from typing import TYPE_CHECKING, Any

from .._logging import get_logger
from ..config import NetworkDefaults, parse_config, generate_hosts
from ..exceptions import ConfigError, LvlabError
from .osinfo import OsInfoLookupError, resolve_os_variant
from .subprocess_env import system_first_env
from .vdisk import VirtualDisk
from .cloud_init import MetaData, NetworkConfig, UserData
from .network import NETWORK_TYPES, USER_MODE_NETWORK_TYPES, generate_mac
from .standalone_cloud_init import render_user_data_override
from .snapshot_cleanup import undefine_with_snapshot_cleanup
from .virsh import (
    DEAD_STATES,
    RUNNING_STATES,
    SHUTDOWNABLE_STATES,
    VirshError,
    _xml_tempfile,
    run_virsh,
    virsh_domstate,
    virsh_domstate_reason,
    virsh_list_all_names,
    virsh_snapshot_names,
)

if TYPE_CHECKING:
    from .images import CloudImage


logger = get_logger(__name__)

# Reused as a logger format string across the four ``virsh`` operations that
# bail out when the requested domain isn't defined at the configured URI.
# Extracted to a module constant to satisfy SonarQube python:S1192 — keep the
# wording in lockstep with the four call sites in this module.
VM_DOES_NOT_EXIST_MSG = "The virtual machine %s does not exist in this Libvirt URI"

# Maps a ``hosts.{family}.tmpl`` filename to the lowercased ``machine.os``
# prefixes that should select it. Used by
# :meth:`_CloudInitComposer._resolve_hosts_template_path` so the
# manifest-wide /etc/hosts snippet lands in the right cloud-init template
# path for the guest distro. Extend here to support a new family.
_HOSTS_TEMPLATE_MAPPING: dict[str, list[str]] = {
    "hosts.debian.tmpl": ["debian", "ubuntu"],
    "hosts.redhat.tmpl": [
        "fedora",
        "rocky",
        "rockylinux",
        "alma",
        "almalinux",
        "rhel",
    ],
}


def _virt_install_network_arg(iface: dict[str, Any]) -> str:
    """Build the ``virt-install --network`` argument for one interface.

    The default (``network_type="network"``) emits a managed-network
    arg with a fixed PCI address — matching pre-Phase-12 behaviour so
    existing manifests are unchanged. ``user`` and ``passt`` emit the
    user-mode forms, which don't take a libvirt network name and
    don't need a fixed PCI address (virt-install picks one). User-mode
    is required for ``qemu:///session`` where rootless libvirt cannot
    manage a NAT network.

    The interface's pinned MAC (``Machine.__init__`` assigns one via
    :func:`tkc_lvlab.utils.network.generate_mac` if the manifest omits it)
    is appended as ``mac=`` so it matches the ``match: macaddress`` selector
    rendered into the guest's cloud-init network-config. Without that pin
    the two would disagree and the static/DHCP config would bind to the
    wrong (or no) device on the NetworkManager renderer.

    Args:
        iface: The merged interface dict from ``Machine.interfaces``
            (already had ``config_defaults['interfaces']`` applied, and a
            ``macaddress`` pinned by ``Machine.__init__``).

    Returns:
        The exact string to pass after ``--network`` on the
        ``virt-install`` command line.
    """
    mac = iface.get("macaddress")
    mac_suffix = f",mac={mac}" if mac else ""
    network_type = iface.get("network_type", "network")
    if network_type == "user":
        return f"user,model=virtio{mac_suffix}"
    if network_type == "passt":
        return f"passt,model=virtio{mac_suffix}"
    libvirt_network = iface.get("network", "default")
    return (
        f"network={libvirt_network},model=virtio{mac_suffix},"
        "address.type=pci,address.domain=0,address.bus=1,"
        "address.slot=0,address.function=0"
    )


def _nameservers_from_networks(
    interfaces: list[dict[str, Any]], networks: dict[str, NetworkDefaults]
) -> dict[str, Any]:
    """Derive machine nameservers from the layered ``networks:`` map (#138 Phase 3).

    Used as the lowest-precedence fallback when neither the machine nor
    ``config_defaults`` declares ``nameservers``: returns the DNS/search of the
    first interface whose libvirt ``network`` has a configured ``networks:``
    entry with DNS servers. (Machine nameservers are a single per-machine block,
    so the first matching interface wins — the common single-NIC case.)

    Args:
        interfaces: The machine's resolved interface dicts.
        networks: The layered per-network defaults map (network name -> defaults).

    Returns:
        ``{"addresses": [...], "search": [...]}`` from the first matching
        interface's network, or ``{}`` when no interface's network supplies DNS.
    """
    if not networks:
        return {}
    for iface in interfaces:
        # ``self.interfaces`` is normally a list of dicts, but degrades to the
        # ``config_defaults['interfaces']`` mapping when a machine declares no
        # interfaces of its own; skip anything that isn't an interface dict.
        if not isinstance(iface, dict):
            continue
        net_defaults = networks.get(iface.get("network"))
        if net_defaults and net_defaults.dns:
            return {
                "addresses": list(net_defaults.dns),
                "search": list(net_defaults.search or []),
            }
    return {}


class _SnapshotManager:
    """Snapshot operations for a single libvirt domain.

    A focused collaborator extracted from :class:`Machine` (issue #48). It
    owns the ``virsh snapshot-*`` interactions for one domain so the
    :class:`Machine` facade methods ``list_snapshots`` / ``create_snapshot``
    / ``delete_snapshot`` can delegate without changing their public
    contracts. All ``virsh`` work goes through the module-level helpers
    (``virsh_list_all_names``, ``virsh_snapshot_names``, ``run_virsh``,
    ``_xml_tempfile``). (Bulk teardown before ``undefine`` now lives in
    :class:`_DomainDestroyer`, which uses the one-shot
    ``undefine --snapshots-metadata`` — issue #96 — rather than a
    snapshot-delete loop here.)

    Args:
        libvirt_vm_name: The env-namespaced libvirt domain name (the real
            domain name, e.g. ``web01_lab``). Every ``virsh`` lookup uses
            this, never the short ``vm_name``.
        vm_name: The short manifest name, used only for human-facing log
            lines (matching the pre-refactor wording).
    """

    def __init__(self, libvirt_vm_name: str, vm_name: str) -> None:
        self.libvirt_vm_name = libvirt_vm_name
        self.vm_name = vm_name

    def list(self, uri: str) -> list[str]:
        """Return the snapshot names defined for the domain in creation order.

        Args:
            uri: A libvirt connection URI (e.g. ``qemu:///session``).

        Returns:
            Snapshot names in creation order. ``[]`` when the domain isn't
            defined or has no snapshots.

        Raises:
            VirshError: If ``virsh`` itself fails for a reason other than
                "domain not defined" (e.g. cannot reach the URI).
        """
        if self.libvirt_vm_name not in virsh_list_all_names(uri):
            return []

        try:
            return virsh_snapshot_names(uri, self.libvirt_vm_name)
        except VirshError:
            # The domain disappeared between the list and the snapshot-list
            # call. Treat as absent — caller wanted snapshot names, there
            # are none to report.
            return []

    def create(
        self,
        uri: str,
        snapshot_name: str,
        snapshot_description: str | None = None,
    ) -> bool:
        """Create a snapshot of the domain via ``virsh snapshot-create``.

        Args:
            uri: A libvirt connection URI (e.g. ``qemu:///session``).
            snapshot_name: Name to assign to the new snapshot.
            snapshot_description: Optional human-readable description.
                Defaults to ``"Snapshot of <libvirt_vm_name>"``.

        Returns:
            ``True`` on success.

        Raises:
            VirshError: When the domain isn't defined at ``uri`` or when
                ``virsh snapshot-create`` itself fails.
        """
        if self.libvirt_vm_name not in virsh_list_all_names(uri):
            logger.warning(
                VM_DOES_NOT_EXIST_MSG,
                self.vm_name,
            )
            raise VirshError(
                1,
                f"domain {self.libvirt_vm_name} is not defined at {uri}",
                ["snapshot-create", self.libvirt_vm_name],
            )

        if not snapshot_description:
            snapshot_description = f"Snapshot of {self.libvirt_vm_name}"

        snapshot_xml = (
            "<domainsnapshot>\n"
            f"    <name>{snapshot_name}</name>\n"
            f"    <description>{snapshot_description}</description>\n"
            "</domainsnapshot>\n"
        )

        with _xml_tempfile(snapshot_xml) as xml_path:
            run_virsh(
                uri,
                ["snapshot-create", self.libvirt_vm_name, "--xmlfile", xml_path],
                timeout=120.0,
            )
        return True

    def delete(self, uri: str, snapshot_name: str) -> None:
        """Delete a named snapshot via ``virsh snapshot-delete``.

        Args:
            uri: A libvirt connection URI (e.g. ``qemu:///session``).
            snapshot_name: Name of the snapshot to delete.

        Raises:
            VirshError: When the domain isn't defined at ``uri``, when the
                named snapshot doesn't exist, or when ``virsh
                snapshot-delete`` itself fails.
        """
        if self.libvirt_vm_name not in virsh_list_all_names(uri):
            logger.warning(
                VM_DOES_NOT_EXIST_MSG,
                self.vm_name,
            )
            raise VirshError(
                1,
                f"domain {self.libvirt_vm_name} is not defined at {uri}",
                ["snapshot-delete", self.libvirt_vm_name, snapshot_name],
            )

        run_virsh(
            uri,
            ["snapshot-delete", self.libvirt_vm_name, snapshot_name],
            timeout=120.0,
        )


class _DomainDestroyer:
    """The full destroy sequence for a single libvirt domain.

    A focused collaborator extracted from :class:`Machine` (issue #48). It
    owns the ordered teardown — force-off (if alive) -> undefine (which
    drops any snapshots in one shot, issue #96) -> on-disk file cleanup —
    so the :class:`Machine.destroy` facade can delegate without changing
    its ``bool`` contract or its "stop at the first failed step" behaviour.

    Args:
        libvirt_vm_name: The env-namespaced libvirt domain name used for
            every ``virsh`` lookup.
        vm_name: The short manifest name, used for human-facing log lines.
        config_fpath: On-disk directory holding the domain's artifacts
            (qcow2 disks, ``cidata.iso``, rendered cloud-init files); the
            target of the file-cleanup step.
    """

    def __init__(
        self,
        libvirt_vm_name: str,
        vm_name: str,
        config_fpath: str,
    ) -> None:
        self.libvirt_vm_name = libvirt_vm_name
        self.vm_name = vm_name
        self.config_fpath = config_fpath

    def destroy(self, uri: str) -> bool:
        """Forcefully power off, undefine, and clean up files for the domain.

        Args:
            uri: libvirt connection URI (e.g. ``qemu:///session``).

        Returns:
            ``True`` if the domain was undefined and file cleanup succeeded.
            ``False`` if the domain was not found, any ``virsh`` call failed,
            or file cleanup raised.
        """
        try:
            current_vms = virsh_list_all_names(uri)
        except VirshError as e:
            logger.error("Failed to list domains at %s: %s", uri, e)
            return False

        if self.libvirt_vm_name not in current_vms:
            logger.warning(VM_DOES_NOT_EXIST_MSG, self.vm_name)
            return False

        try:
            vm_state = virsh_domstate(uri, self.libvirt_vm_name)
        except VirshError as e:
            logger.error("Failed to query state of %s: %s", self.vm_name, e)
            return False

        vm_state = self._force_off_if_alive(uri, vm_state)
        if vm_state is None:
            return False

        # Only proceed with snapshot cleanup + undefine when the domain has
        # actually reached a dead state. If destroy didn't take effect (state
        # is still running, in shutdown, etc.) we abort rather than undefining
        # a live domain.
        if vm_state not in DEAD_STATES:
            logger.error(
                "Machine %s is in state %r; refusing to undefine.",
                self.vm_name,
                vm_state,
            )
            return False

        # Undefine drops any snapshots in one shot (issue #96), so there's no
        # separate pre-undefine snapshot-deletion step.
        if not self._undefine(uri):
            return False
        return self._cleanup_files()

    def _force_off_if_alive(self, uri: str, vm_state: str) -> str | None:
        """If the domain is still running/paused, force it off and return new state.

        The previous libvirt-python code called ``virsh destroy`` (a
        power-cord-pull) on RUNNING or PAUSED — the same set
        :data:`RUNNING_STATES` carries.

        Returns:
            The post-destroy domain state, or ``vm_state`` unchanged when
            the domain was already in a non-running state. ``None`` when
            either the force-off or the follow-up state query failed
            (error already logged).
        """
        if vm_state not in RUNNING_STATES:
            return vm_state
        logger.warning("Forcefully shutting down %s", self.vm_name)
        try:
            run_virsh(uri, ["destroy", self.libvirt_vm_name])
        except VirshError as e:
            logger.error("Failed to forcefully shutdown %s: %s", self.vm_name, e)
            return None
        try:
            return virsh_domstate(uri, self.libvirt_vm_name)
        except VirshError as e:
            logger.error(
                "Failed to query state of %s after destroy: %s", self.vm_name, e
            )
            return None

    def _undefine(self, uri: str) -> bool:
        """Undefine the libvirt domain, dropping any snapshots in one shot.

        Delegates to
        :func:`tkc_lvlab.utils.snapshot_cleanup.undefine_with_snapshot_cleanup`,
        which retries with ``undefine --snapshots-metadata`` if the domain
        still owns snapshots (issue #96). Returns False on virsh failure.
        """
        logger.info("Undefining %s", self.vm_name)
        try:
            undefine_with_snapshot_cleanup(uri, self.libvirt_vm_name)
        except VirshError as e:
            logger.error(
                "Failed to undefine (remove from Libvirt) %s: %s", self.vm_name, e
            )
            return False
        return True

    def _cleanup_files(self) -> bool:
        """Remove on-disk machine files and the (empty) config directory.

        Mirrors the previous behavior: a filesystem error here logs and
        returns ``False`` even though libvirt state has already been
        cleaned up by the time we reach this point.
        """
        try:
            for path in glob.glob(os.path.join(self.config_fpath, "*")):
                if os.path.isfile(path):
                    logger.info("Removing file %s.", path)
                    os.remove(path)
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Exception when removing machine files %s", e)
            return False

        try:
            if not os.listdir(self.config_fpath):
                logger.info("Removing machine directory %s.", self.config_fpath)
                os.rmdir(self.config_fpath)
            else:
                logger.warning("Machine directory %s is not empty.", self.config_fpath)
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Exception when removing machine files %s", e)
            return False
        return True


class _CloudInitComposer:
    """Cloud-init document orchestration for a single :class:`Machine`.

    A focused collaborator extracted from :class:`Machine` (issue #48). It
    owns rendering the three NoCloud documents (``meta-data``,
    ``user-data``, ``network-config``) to disk plus the runcmd/defaults
    merge, the manifest-wide ``/etc/hosts`` injection, and the
    distro-family hosts-template resolution. The :class:`Machine.cloud_init`
    facade delegates to :meth:`render`, preserving the ``machines`` param
    (issue #49) and the top-of-runcmd ``/etc/hosts`` behaviour verbatim.

    Rendering and IO go through the module-level names
    (``NetworkConfig`` / ``MetaData`` / ``UserData`` / ``parse_config`` /
    ``generate_hosts``) so existing tests that patch them at the
    :mod:`tkc_lvlab.utils.libvirt` boundary keep working unchanged.

    Args:
        machine: The owning :class:`Machine`. The composer reads its
            identity/state attributes (``environment``, ``vm_name``,
            ``libvirt_vm_name``, ``hostname``, ``domain``, ``fqdn``, ``os``,
            ``interfaces``, ``nameservers``, ``cloud_init_config``,
            ``config_fpath``) rather than copying them, so a constructed
            :class:`Machine` and its composer never drift.
    """

    def __init__(self, machine: "Machine") -> None:
        self.machine = machine

    def render(
        self,
        cloud_image: "CloudImage",
        config_defaults: dict[str, Any],
        machines: list[dict[str, Any]] | None = None,
        password_hash: str | None = None,
    ) -> tuple[str, str, str]:
        """Render the three cloud-init documents to disk.

        See :meth:`Machine.cloud_init` for the full contract — this is the
        body it delegates to.

        Args:
            cloud_image: The :class:`tkc_lvlab.utils.images.CloudImage` for
                this machine — its ``network_version`` and
                ``default_username`` are consulted.
            config_defaults: The manifest's ``config_defaults`` block.
            machines: The manifest's ``machines`` list for the ``/etc/hosts``
                render. ``None`` triggers a one-time :func:`parse_config`
                fallback.
            password_hash: A generated SHA-512-crypt console password hash
                to inject as ``users[*].passwd`` (issue #106). Only applied
                when the merged ``cloud_init`` has no explicit ``passwd`` —
                a manifest-configured password always wins. ``None`` injects
                nothing (key-only VM).

        Returns:
            ``(meta_data_path, user_data_path, network_config_path)``.

        Raises:
            ValueError: When the machine's ``os`` matches no known
                ``/etc/cloud/templates/hosts.*.tmpl`` distro family.
            LvlabError: When the config directory cannot be created.
            ConfigError: On the ``machines is None`` fallback when
                :func:`parse_config` cannot read the manifest.
        """
        machine = self.machine
        self._ensure_config_dir()

        network_config_fpath = self._render_and_write(
            NetworkConfig(
                cloud_image.network_version, machine.interfaces, machine.nameservers
            ),
            "network-config",
        )
        metadata_config_fpath = self._render_and_write(
            MetaData(machine.libvirt_vm_name, machine.fqdn),
            "meta-data",
        )

        cloud_init_config = self._merge_cloud_init_config(
            config_defaults.get("cloud_init", {})
        )
        # Default the first-boot user to the image's conventional account
        # (debian/fedora/almalinux/...) when the manifest doesn't set one,
        # using the same derivation createvm applies. An explicit
        # cloud_init.user still wins.
        cloud_init_config.setdefault("user", cloud_image.default_username)
        # Inject a generated one-time console password hash (issue #106) only
        # when the manifest didn't configure one — an explicit cloud_init.passwd
        # always wins, and password_hash=None (opt-out / key-only) injects
        # nothing.
        if password_hash is not None:
            cloud_init_config.setdefault("passwd", password_hash)

        # The CLI passes the already-parsed machines list so the manifest is
        # read once per command path. The None fallback re-parses only for
        # callers that don't hold the list (kept distinct so the common path
        # never touches disk a second time).
        if machines is None:
            try:
                _, _, _, machines = parse_config()
            except (ConfigError, TypeError) as exc:
                logger.error("Could not parse config file.")
                raise ConfigError("Could not parse config file.") from exc

        # Compute the manifest-wide /etc/hosts heredocs once; both the
        # structured render path and the user_data override path use the
        # same prefix when ``manage_etc_hosts`` (#120) is on.
        hosts_runcmd_prefix = self._build_hosts_runcmd_prefix(
            cloud_init_config, config_defaults, machines
        )

        # User-data override (#140) — a per-machine ``cloud_init.user_data``
        # (or config-defaults equivalent; manifest precedence applies via
        # _merge_cloud_init_config) is a full cloud-config document owned
        # by the operator. When set, it replaces the structured UserData
        # template render. The hosts heredocs from above are prepended to
        # the override's runcmd (same as createvm prepends its
        # host-wide runcmd), so manifest-wide /etc/hosts bootstrap still
        # happens; an operator who wants full control sets
        # ``manage_etc_hosts: false`` (then ``hosts_runcmd_prefix`` is empty).
        user_data_override = cloud_init_config.pop("user_data", None)
        if user_data_override is not None:
            userdata_config_fpath = self._render_user_data_override(
                user_data_override,
                cloud_init_config,
                password_hash,
                hosts_runcmd_prefix,
            )
            return metadata_config_fpath, userdata_config_fpath, network_config_fpath

        # The manage_etc_hosts flag (#120) gates BOTH halves of lvlab's
        # in-guest /etc/hosts management together: the cloud-init template's
        # ``manage_etc_hosts: true`` line AND the two runcmd heredocs that
        # rewrite /etc/hosts + the distro hosts.*.tmpl. Default true preserves
        # today's behaviour; set ``cloud_init.manage_etc_hosts: false`` (in
        # config_defaults or per-machine) when an external CM tool (Salt /
        # Ansible) owns /etc/hosts on the guest.
        if hosts_runcmd_prefix:
            # Prepend the two hosts heredoc snippets so /etc/hosts (and the
            # cloud-init template) are populated before any runcmd entry that
            # does DNS-ish work.
            cloud_init_config["runcmd"] = list(
                hosts_runcmd_prefix
            ) + cloud_init_config.get("runcmd", [])

        userdata_config_fpath = self._render_and_write(
            UserData(cloud_init_config, machine.hostname, machine.domain, machine.fqdn),
            "user-data",
        )

        return metadata_config_fpath, userdata_config_fpath, network_config_fpath

    def _build_hosts_runcmd_prefix(
        self,
        cloud_init_config: dict[str, Any],
        config_defaults: dict[str, Any],
        machines: list[dict[str, Any]],
    ) -> list[str]:
        """Build the two manifest-wide /etc/hosts heredocs as a runcmd prefix.

        Returns an empty list when ``cloud_init.manage_etc_hosts`` is
        ``False`` (the #120 opt-out). Otherwise returns the
        ``/etc/hosts`` heredoc followed by the distro-specific
        ``hosts.*.tmpl`` heredoc — the same two snippets the structured
        path has prepended since #120, factored out so the user_data
        override path (#140) can also consume them.

        Args:
            cloud_init_config: The merged cloud_init mapping (defaults +
                per-machine, after :meth:`_merge_cloud_init_config`).
            config_defaults: The manifest's ``config_defaults`` block,
                used by :func:`generate_hosts` for hostname/domain.
            machines: The machine list from the manifest, used to render
                each machine into ``/etc/hosts``.

        Returns:
            ``[hosts_snippet, hosts_template_snippet]`` when
            ``manage_etc_hosts`` is on, else ``[]``.

        Raises:
            ValueError: When the machine's ``os`` matches no known
                ``/etc/cloud/templates/hosts.*.tmpl`` distro family.
        """
        if not cloud_init_config.get("manage_etc_hosts", True):
            return []
        machine = self.machine
        hosts_snippet = generate_hosts(
            machine.environment, config_defaults, machines, heredoc="/etc/hosts"
        )
        template_fpath = self._resolve_hosts_template_path()
        hosts_template_snippet = generate_hosts(
            machine.environment, config_defaults, machines, heredoc=template_fpath
        )
        return [hosts_snippet, hosts_template_snippet]

    def _render_user_data_override(
        self,
        user_data: Any,
        cloud_init_config: dict[str, Any],
        password_hash: str | None,
        runcmd_prefix: list[str],
    ) -> str:
        """Render a manifest ``cloud_init.user_data`` override and write to disk.

        The override is a full cloud-config document owned by the
        operator. Placeholder context covers the manifest's per-machine
        identity (``vm_name`` / ``vm_hostname`` / ``fqdn`` /
        ``environment``), the resolved first-boot username (defaulted
        from the image when not set), and the optional generated
        password hash. The ``cloud_init.pubkey`` (when set) is resolved
        to a literal SSH public key and appended to every user's
        ``ssh_authorized_keys`` via :func:`render_user_data_override`.

        Args:
            user_data: The raw ``user_data:`` mapping popped out of
                ``cloud_init_config``.
            cloud_init_config: The remaining merged cloud_init mapping
                (after the ``user_data`` pop). Consulted for the
                resolved first-boot username (``user``), password hash
                (``passwd``, when manifest-set), and the SSH public key
                (``pubkey``).
            password_hash: The generated console password hash from
                ``Machine.cloud_init(password_hash=...)``. The
                placeholder reflects whichever the manifest path would
                have used: a manifest-set ``passwd`` wins, else this
                generated hash, else empty string.
            runcmd_prefix: Hosts heredocs to inject ahead of the
                override's own ``runcmd`` (from
                :meth:`_build_hosts_runcmd_prefix`).

        Returns:
            The file path of the written ``user-data`` document.
        """
        machine = self.machine
        # Resolve the first-boot username: prefer an explicit setting,
        # then the image's default that was setdefault-ed above.
        resolved_user = cloud_init_config.get("user", "")
        # Mirror the structured-path precedence: a manifest-set passwd
        # wins over the generated password_hash; otherwise use whichever
        # the structured path would have rendered (or "" when neither).
        resolved_password_hash = cloud_init_config.get("passwd") or password_hash or ""
        context = {
            "vm_name": machine.vm_name,
            "vm_hostname": machine.hostname,
            "fqdn": machine.fqdn,
            "default_vm_username": resolved_user,
            "password_hash": resolved_password_hash,
            "environment": machine.environment.get("name", ""),
        }
        authorized_keys = self._resolve_pubkey_list(cloud_init_config)
        rendered = render_user_data_override(
            user_data,
            context=context,
            authorized_keys=authorized_keys,
            runcmd_prefix=runcmd_prefix,
        )
        fpath = os.path.join(machine.config_fpath, "user-data")
        logger.info("Writing cloud-init user-data file (override) %s", fpath)
        with open(fpath, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        return fpath

    @staticmethod
    def _resolve_pubkey_list(cloud_init_config: dict[str, Any]) -> list[str]:
        """Resolve a single ``cloud_init.pubkey`` value to a list of SSH keys.

        The manifest accepts ``cloud_init.pubkey`` as either a literal
        SSH key string or a path on disk (``~``-expanded). The structured
        path resolves it in :class:`UserData.__post_init__`; the override
        path needs the same resolution to feed into
        :func:`render_user_data_override`'s ``authorized_keys``.

        Returns:
            A single-element list (or empty list when no pubkey is set).
            A path that doesn't exist or doesn't look like an SSH key is
            logged and dropped — same forgiving behaviour as the
            structured path.
        """
        pubkey = cloud_init_config.get("pubkey")
        if not pubkey:
            return []
        if "~" in pubkey or "/" in pubkey:
            pubkey_path = os.path.expanduser(pubkey)
            try:
                with open(pubkey_path, "r", encoding="utf-8") as fh:
                    return [fh.read().strip()]
            except OSError as exc:
                logger.warning("Could not read pubkey file %s: %s", pubkey_path, exc)
                return []
        return [pubkey.strip()]

    def _ensure_config_dir(self) -> None:
        """Create the machine's ``config_fpath`` if absent.

        Raises:
            LvlabError: The cloud-init config directory could not be created
                (e.g. permission denied). Raised as a library exception so
                this module never imports ``typer``; the CLI boundary
                (:mod:`tkc_lvlab.cli`) converts it to a ``typer.Exit``.
        """
        config_fpath = self.machine.config_fpath
        if os.path.exists(config_fpath):
            return
        try:
            os.makedirs(config_fpath)
        except OSError as e:
            logger.error("Exception creating %s: %s", config_fpath, e)
            raise LvlabError(
                f"Could not create cloud-init config directory "
                f"'{config_fpath}': {e}"
            ) from e

    def _render_and_write(self, renderable: Any, filename: str) -> str:
        """Render ``renderable.render_config()`` to ``<config_fpath>/<filename>``.

        Returns the resolved file path.
        """
        fpath = os.path.join(self.machine.config_fpath, filename)
        logger.info("Writing cloud-init %s file %s", filename, fpath)
        with open(fpath, "w", encoding="utf-8") as fh:
            fh.write(renderable.render_config())
        return fpath

    def _merge_cloud_init_config(self, cloud_init_defaults: dict[str, Any]) -> dict:
        """Merge ``cloud_init_defaults`` and the machine's ``cloud_init_config``.

        Honours the per-machine ``runcmd_ignore_defaults`` opt-out: when
        truthy, the defaults' ``runcmd`` is dropped before the merge
        (every other defaults key still applies). Without the opt-out,
        ``runcmd`` from defaults is prepended to the machine's runcmd.
        """
        machine_cloud_init = self.machine.cloud_init_config
        if machine_cloud_init.get("runcmd_ignore_defaults", False):
            logger.debug(
                "Ignoring config_defaults:cloud_init:runcmd for %s",
                self.machine.vm_name,
            )
            filtered_defaults = {
                k: v for k, v in cloud_init_defaults.items() if k != "runcmd"
            }
            return {**filtered_defaults, **machine_cloud_init}

        logger.debug(
            "Including config_defaults:cloud_init:runcmd for %s", self.machine.vm_name
        )
        merged = {**cloud_init_defaults, **machine_cloud_init}
        if "runcmd" in cloud_init_defaults and "runcmd" in machine_cloud_init:
            merged["runcmd"] = (
                cloud_init_defaults["runcmd"] + machine_cloud_init["runcmd"]
            )
        elif "runcmd" in cloud_init_defaults:
            merged["runcmd"] = cloud_init_defaults["runcmd"]
        return merged

    def _resolve_hosts_template_path(self) -> str:
        """Return ``/etc/cloud/templates/hosts.<family>.tmpl`` for the machine's OS.

        Raises:
            ValueError: When the machine's ``os`` matches no entry in
                :data:`_HOSTS_TEMPLATE_MAPPING`.
        """
        os_lower = self.machine.os.lower()
        for template, distros in _HOSTS_TEMPLATE_MAPPING.items():
            if any(os_lower.startswith(d) for d in distros):
                return "/etc/cloud/templates/" + template
        raise ValueError(f"Could not find a template file for {self.machine.os}")


class Machine:
    """A libvirt-managed lab VM described by the ``Lvlab.yml`` manifest.

    Constructed from one entry in the manifest's ``machines`` list plus
    the enclosing environment dict and the manifest's ``config_defaults``.
    ``__init__`` merges defaults into the machine — interfaces, disks,
    ``shared_directories``, and top-level keys — so callers can treat
    the resulting object as fully resolved.

    The libvirt domain name is **not** :attr:`vm_name`; it is
    :attr:`libvirt_vm_name`, which prepends the environment name. This
    namespacing is what lets multiple lvlab environments coexist on one
    hypervisor — anything that looks up a domain by name must use
    :attr:`libvirt_vm_name`.

    Args:
        machine: One entry from the manifest's ``machines`` list. Must
            carry at least ``vm_name``; other keys (``hostname``,
            ``fqdn``, ``os``, ``cpu``, ``memory``, ``interfaces``,
            ``disks``, ``cloud_init``, ``shared_directories``) are
            merged from ``config_defaults`` when absent.
        environment: The enclosing ``environment`` dict (carries
            ``name``, ``libvirt_uri``, etc.). The environment name is
            used to namespace :attr:`libvirt_vm_name`.
        config_defaults: The manifest's ``config_defaults`` block —
            applied as a baseline for top-level keys plus interfaces,
            disks, and ``shared_directories``.
        networks: The layered ``networks:`` per-network defaults map
            (network name -> :class:`tkc_lvlab.config.NetworkDefaults`),
            from :func:`tkc_lvlab.config.load_host_config` (#138 Phase 3).
            A static interface whose ``network`` matches an entry inherits
            that network's ``gateway`` (when ``ip4gw`` is unset) and the
            machine inherits its ``dns``/``search`` (when no ``nameservers``
            are declared). Explicit manifest/defaults values always win.
            Defaults to ``None`` (no filling) for callers that don't render
            cloud-init (``destroy``/``down``/snapshot paths).

    Attributes:
        environment: Reference to the environment dict.
        vm_name: Short name from the manifest (e.g. ``minion1``).
        libvirt_vm_name: Namespaced domain name —
            ``f"{vm_name}_{environment['name']}"``. Used for every
            ``virsh`` lookup.
        hostname: Short hostname for the guest.
        domain: Domain (DNS) name from ``config_defaults``.
        fqdn: Fully-qualified hostname. Honors a manifest ``fqdn`` when
            set, otherwise built as ``f"{hostname}.{domain}"``.
        os: OS identifier (e.g. ``fedora40``). The ``virt-install``
            ``--os-variant`` is derived by splitting on ``-`` and taking
            the first segment.
        cpu: vCPU count.
        memory: RAM in MiB.
        interfaces: List of resolved interface dicts.
        nameservers: Resolved nameservers dict — per-machine override,
            then ``config_defaults['interfaces']['nameservers']``, then the
            layered ``networks:`` DNS for an interface's network (#138).
        disks: List of resolved disk dicts.
        shared_directories: Merged shared-directory list (defaults +
            per-machine, keyed by ``mount_tag``).
        cloud_init_config: Per-machine ``cloud_init`` dict from the
            manifest.
        config_fpath: On-disk directory holding this machine's artifacts
            (qcow2 disks, ``cidata.iso``, rendered cloud-init files).
            Resolved from ``config_defaults['disk_image_basedir']``
            joined with the environment name and ``vm_name``.
    """

    def __init__(
        self,
        machine: dict[str, Any],
        environment: dict[str, Any],
        config_defaults: dict[str, Any],
        networks: dict[str, NetworkDefaults] | None = None,
    ) -> None:

        networks = networks or {}

        # Apply interface defaults, then pin a deterministic MAC per
        # interface (unless the manifest supplied one). The same address
        # feeds both the virt-install ``--network ...,mac=`` arg
        # (_virt_install_network_arg) and the cloud-init network-config's
        # ``match: macaddress`` (rendered in cloud_init()), so the guest
        # config binds to the right NIC regardless of the distro-assigned
        # device name — required for the NetworkManager renderer
        # (Fedora/RHEL), which ignores match-by-driver.
        for index, iface in enumerate(machine.get("interfaces", [])):
            machine["interfaces"][index] = {
                **config_defaults.get("interfaces", {}),
                **iface,
            }
            machine["interfaces"][index].setdefault("macaddress", generate_mac())
            # Fill the gateway for a static interface from the layered
            # ``networks:`` map (#138 Phase 3) when the manifest/defaults
            # didn't set one: a bridge interface (``network: vlan10``) inherits
            # ``vlan10``'s configured gateway instead of repeating it per VM.
            # An explicit ``ip4gw`` always wins (setdefault).
            merged_iface = machine["interfaces"][index]
            net_defaults = networks.get(merged_iface.get("network"))
            if net_defaults and merged_iface.get("ip4") and net_defaults.gateway:
                merged_iface.setdefault("ip4gw", net_defaults.gateway)

        # Validate interface network_type and the ip4-with-user-mode
        # combination at construction time so an operator sees a clear
        # error before any state is created (cloud-init render, qcow2
        # disk, virt-install). Static IPs under SLIRP/passt are silently
        # ignored by virt-install; refuse the combination loudly instead.
        for iface in machine.get("interfaces", []):
            network_type = iface.get("network_type", "network")
            if network_type not in NETWORK_TYPES:
                raise ValueError(
                    f"Invalid network_type {network_type!r} on interface "
                    f"{iface.get('name', '<unnamed>')!r}. "
                    f"Valid values: {', '.join(NETWORK_TYPES)}."
                )
            if network_type in USER_MODE_NETWORK_TYPES and (
                iface.get("ip4") or iface.get("ip6")
            ):
                static_field = "ip4" if iface.get("ip4") else "ip6"
                raise ValueError(
                    f"Interface {iface.get('name', '<unnamed>')!r} declares "
                    f"network_type={network_type!r} together with a static "
                    f"{static_field} ({iface[static_field]!r}). User-mode "
                    f"networking (SLIRP/passt) does not honour static IPs — "
                    f"remove the {static_field} field or switch to "
                    f"network_type='network'."
                )

        # Apply disk defaults
        for index, disk in enumerate(machine.get("disks", [])):
            disk_defaults = next(
                (
                    default_disk
                    for default_disk in config_defaults.get("disks", [])
                    if default_disk["name"] == disk["name"]
                ),
                {},
            )
            machine["disks"][index] = {**disk_defaults, **disk}

        # Apply shared_directories defaults.
        # Defaults always apply; per-machine entries extend the list and override any
        # default whose mount_tag they share. Result is a list keyed-by-mount_tag.
        default_shared_dirs = config_defaults.get("shared_directories", []) or []
        machine_shared_dirs = machine.get("shared_directories", []) or []
        merged_shared_dirs = {sd["mount_tag"]: sd for sd in default_shared_dirs}
        for sd in machine_shared_dirs:
            merged_shared_dirs[sd["mount_tag"]] = {
                **merged_shared_dirs.get(sd["mount_tag"], {}),
                **sd,
            }
        machine["shared_directories"] = list(merged_shared_dirs.values())

        # Expand ``~`` and ``$HOME`` references in shared_directories source
        # paths so the manifest can stay portable across users without
        # hardcoding a specific home dir. Matches the expansion behavior
        # already applied to ``disk_image_basedir`` further down.
        for sd in machine["shared_directories"]:
            if "source" in sd:
                sd["source"] = os.path.expanduser(os.path.expandvars(sd["source"]))

        # Apply machine defaults
        machine = {**config_defaults, **machine}

        # Setup a machine file path to contain all of the files associated
        # with the instance of a machine.
        vm_name = machine.get("vm_name")
        config_fpath = os.path.expanduser(
            os.path.join(
                os.path.expanduser(
                    config_defaults.get(
                        "disk_image_basedir", "/var/lib/libvirt/images/lvlab"
                    )
                ),
                environment.get("name", "LvLabEnvironment"),
                vm_name,
            )
        )

        self.environment = environment
        self.vm_name = vm_name
        self.libvirt_vm_name = (
            vm_name + "_" + environment.get("name", "LvLabEnvironment")
        )
        self.hostname = machine.get("hostname", None)
        self.domain = config_defaults.get("domain", None)
        # If the machine has an fqdn use it, otherwise build a
        #  {hostname}.{domain} based fqdn
        self.fqdn = (
            machine.get("fqdn", None)
            if machine.get("fqdn", None)
            else f"{self.hostname}.{self.domain}"
        )
        # If we don't have an os by now set a default of Generic Linux 2022
        self.os = machine.get("os", config_defaults.get("os", "linux2022"))
        self.cpu = machine.get("cpu", config_defaults.get("cpu", 2))
        self.memory = machine.get("memory", config_defaults.get("memory", 2024))
        self.interfaces = machine.get("interfaces", [])
        # Precedence: machine ``nameservers`` -> config_defaults nameservers ->
        # the layered ``networks:`` map's DNS for an interface's network
        # (#138 Phase 3). The networks-derived block is the lowest fallback, so
        # an explicit manifest/defaults nameservers always wins.
        self.nameservers = machine.get(
            "nameservers", config_defaults["interfaces"].get("nameservers", {})
        ) or _nameservers_from_networks(self.interfaces, networks)
        self.disks = machine.get("disks", [])
        self.shared_directories = machine.get("shared_directories", [])
        self.cloud_init_config = machine.get("cloud_init", {})
        self.config_fpath = config_fpath

        # Focused collaborators the public facade methods delegate to
        # (issue #48). They read this Machine's resolved identity/state
        # rather than copying it, so they never drift from the facade.
        self._snapshots = _SnapshotManager(self.libvirt_vm_name, self.vm_name)
        self._destroyer = _DomainDestroyer(
            self.libvirt_vm_name, self.vm_name, self.config_fpath
        )
        self._cloud_init_composer = _CloudInitComposer(self)

    def cloud_init(
        self,
        cloud_image: "CloudImage",
        config_defaults: dict[str, Any],
        machines: list[dict[str, Any]] | None = None,
        password_hash: str | None = None,
    ) -> tuple[str, str, str]:
        """Render this machine's three cloud-init documents to disk.

        Writes ``meta-data``, ``user-data``, and ``network-config`` into
        :attr:`config_fpath`. The manifest's ``config_defaults['cloud_init']``
        is merged with :attr:`cloud_init_config` first; when the per-machine
        ``cloud_init.runcmd_ignore_defaults`` is truthy, the defaults'
        ``runcmd`` is dropped (every other key in defaults still merges).
        Otherwise ``runcmd`` from defaults precedes the per-machine list.

        The manifest-wide ``/etc/hosts`` snippet plus the matching
        ``/etc/cloud/templates/hosts.{debian,redhat}.tmpl`` snippet are
        injected at the **top** of ``runcmd`` so they land before
        anything that does DNS-ish work in the guest.

        Args:
            cloud_image: The :class:`tkc_lvlab.utils.images.CloudImage`
                for this machine — only its ``network_version`` is
                consulted (passed to :class:`NetworkConfig`).
            config_defaults: The manifest's ``config_defaults`` block.
            machines: The manifest's ``machines`` list, used to render the
                ``/etc/hosts`` snippet. Callers (the CLI commands) already
                hold this from their single manifest load and pass it in so
                the manifest is not re-read here. When ``None`` (a
                convenience fallback for callers without the list handy), the
                manifest is parsed once via :func:`parse_config`.
            password_hash: Optional SHA-512-crypt console password hash to
                inject as ``users[*].passwd`` (issue #106). Applied only when
                the manifest has no explicit ``cloud_init.passwd``; ``None``
                injects nothing.

        Returns:
            ``(meta_data_path, user_data_path, network_config_path)`` —
            the three rendered file paths. Callers feed these to
            :class:`tkc_lvlab.utils.cloud_init.CloudInitIso`.

        Raises:
            ValueError: When :attr:`os` does not match a known
                ``/etc/cloud/templates/hosts.*.tmpl`` distro family.
                Extend :data:`_HOSTS_TEMPLATE_MAPPING` to add a new family.
            LvlabError: When :attr:`config_fpath` cannot be created
                (raised by :meth:`_ensure_config_dir`).
            ConfigError: Only on the ``machines is None`` fallback, when
                :func:`parse_config` cannot read the manifest (missing or
                structurally invalid). The CLI boundary converts it to a
                ``typer.Exit``.
        """
        return self._composer().render(
            cloud_image, config_defaults, machines, password_hash=password_hash
        )

    def _composer(self) -> _CloudInitComposer:
        """Return this machine's cloud-init composer, building one if absent.

        Constructed in ``__init__`` for normally-built machines; rebuilt
        on demand for test stubs created via ``object.__new__`` (which set
        the state attributes the composer reads but skip ``__init__``).
        """
        composer = getattr(self, "_cloud_init_composer", None)
        if composer is None:
            composer = _CloudInitComposer(self)
        return composer

    def create_vdisks(
        self,
        environment: dict[str, Any] | None = None,
        config_defaults: dict[str, Any] | None = None,
        cloud_image: "CloudImage" | None = None,
    ) -> None:
        """Create the per-disk qcow2 files declared in :attr:`disks`.

        One :class:`tkc_lvlab.utils.vdisk.VirtualDisk` per entry in
        :attr:`disks`, named ``disk{index}.qcow2`` under
        :attr:`config_fpath`. Existing disks are skipped (logged as
        "exists at <path>"); missing disks are created via ``qemu-img
        create -b <cloud_image>`` so the cloud image becomes the
        backing file.

        Per-disk failures are logged but do not raise; the loop
        continues to the next disk. The original behavior is preserved.

        Args:
            environment: The enclosing environment dict — passed
                through to :class:`VirtualDisk`. ``None`` is treated as
                an empty dict.
            config_defaults: The manifest's ``config_defaults`` block —
                passed through to :class:`VirtualDisk` for path
                resolution. ``None`` is treated as an empty dict.
            cloud_image: The :class:`tkc_lvlab.utils.images.CloudImage`
                to use as the qcow2 backing file. ``None`` is accepted
                in the signature, but :class:`VirtualDisk` requires a
                usable image to actually create a disk.

        Returns:
            ``None``. Errors are logged, not raised.
        """
        if environment is None:
            environment = {}
        if config_defaults is None:
            config_defaults = {}

        for index, disk in enumerate(self.disks):
            vdisk = VirtualDisk(
                self.vm_name,
                disk,
                index,
                cloud_image,
                environment,
                config_defaults,
            )

            if vdisk.exists():
                logger.info("Virtual Disk: %s exists at %s", vdisk.name, vdisk.fpath)
            else:
                logger.info("Creating Virtual Disk: %s at %s", vdisk.fpath, vdisk.size)
                if vdisk.strategy == "backing":
                    # Backing-file disks depend on the shared cloud-images cache
                    # (issue #99). Warn loudly so the operator knows not to run
                    # `lvlab images clean` / wipe the cache while this VM exists.
                    logger.warning(
                        "Disk %s uses backing-file mode: it depends on the "
                        "cached base image %s. Do NOT clean/wipe the "
                        "cloud-images cache while this VM exists, or the disk "
                        "will break. Use the default 'copy' strategy to avoid "
                        "this.",
                        vdisk.fpath,
                        vdisk.backing_image_fpath,
                    )
                if vdisk.create():
                    if vdisk.exists():
                        logger.info("Virtual Disk Created Successfully")
                else:
                    logger.error("Failed to create Virtual Disk: %s", vdisk.fpath)

    def deploy(
        self,
        config_path: str,
        config_defaults: dict[str, Any],
        uri: str,
        os_variant: str | None = None,
    ) -> bool:
        """Define and start the libvirt domain via ``virt-install``.

        Builds the ``virt-install`` command from this machine's
        resolved config: memory, vCPUs, the qcow2 at
        ``<config_path>/disk0.qcow2``, the cloud-init ISO at
        ``<config_path>/cidata.iso``, the first interface's libvirt
        network, and the ``--os-variant``. The os-variant comes from
        the resolved image entry when ``os_variant`` is supplied (so a
        manifest's per-image ``os_variant`` override is honoured —
        e.g. ``ubuntu22.04`` for an ``ubuntu2204`` key); otherwise it
        falls back to splitting :attr:`os` on ``-`` and taking the
        first segment. Either way it's run through
        :func:`tkc_lvlab.utils.osinfo.resolve_os_variant` for osinfo-db
        fuzzy fallback.

        ``--graphics vnc,listen=0.0.0.0`` is hard-coded; review before
        exposing the host on an untrusted network.

        When :attr:`shared_directories` is non-empty, adds
        ``--memorybacking=source.type=memfd,access.mode=shared`` plus
        one ``--filesystem=...,driver.type=virtiofs`` per entry.

        Args:
            config_path: On-disk directory containing this machine's
                ``disk0.qcow2`` and ``cidata.iso``. Usually equals
                :attr:`config_fpath`.
            config_defaults: The manifest's ``config_defaults`` block.
                Currently unused inside the method body — kept in the
                signature for symmetry with the other ``Machine``
                methods and for future hooks.
            uri: A libvirt connection URI (e.g. ``qemu:///session``).

        Returns:
            ``True`` if ``virt-install`` exited cleanly. ``False`` if
            ``virt-install`` raised :class:`subprocess.CalledProcessError`
            (the error and the assembled command line are logged).
        """
        requested_variant = os_variant or self.os.split("-")[0]
        try:
            resolved_variant, fallback_reason = resolve_os_variant(requested_variant)
        except OsInfoLookupError as exc:
            logger.warning(
                "Could not resolve --os-variant against osinfo-db (%s); "
                "using requested %r as-is",
                exc,
                requested_variant,
            )
            resolved_variant = requested_variant
        else:
            if fallback_reason:
                logger.warning("os-variant fallback: %s", fallback_reason)

        command = [
            "virt-install",
            f"--connect={uri}",
            f"--name={self.libvirt_vm_name}",
            f"--memory={self.memory}",
            f"--vcpus={self.cpu}",
            "--import",
            "--disk",
            f"path={os.path.join(config_path, 'disk0.qcow2')}",
            "--disk",
            f"path={os.path.join(config_path, 'cidata.iso') + ',device=cdrom'}",
            f"--os-variant={resolved_variant}",
            "--network",
            _virt_install_network_arg(self.interfaces[0]),
            "--graphics",
            "vnc,listen=0.0.0.0",
            "--noautoconsole",
        ]

        # Extend the command to enable shared_directories if found in the config.
        # self.shared_directories is the merged result of config_defaults and the
        # per-machine entries (see Machine.__init__).
        if self.shared_directories:
            command.append("--memorybacking=source.type=memfd,access.mode=shared")
            for filesystem in self.shared_directories:
                command.append(
                    f'--filesystem={filesystem["source"]},{filesystem["mount_tag"]},driver.type=virtiofs'
                )

        try:
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=system_first_env(),
            )
            return True
        except subprocess.CalledProcessError as e:
            logger.error("Error in virt-install call: %s", e)
            logger.error("%s", " ".join(command))
            return False

    def destroy(self, uri: str) -> bool:
        """Forcefully power off, undefine, and clean up files for this machine.

        The sequence is: force-off (if running/paused) -> delete all snapshots
        -> undefine the domain -> remove on-disk files under
        ``self.config_fpath``. Each ``virsh`` step is independent; if any step
        fails the method logs and returns ``False`` without attempting later
        steps (preserves the previous "stop at first error" behavior).

        Args:
            uri: libvirt connection URI (e.g. ``qemu:///session``).

        Returns:
            ``True`` if the domain was undefined and file cleanup succeeded.
            ``False`` if the domain was not found, any ``virsh`` call failed,
            or file cleanup raised.
        """
        return self._get_destroyer().destroy(uri)

    def _get_destroyer(self) -> _DomainDestroyer:
        """Return this machine's destroyer, building one if absent.

        Constructed in ``__init__`` for normally-built machines; rebuilt on
        demand for test stubs created via ``object.__new__`` (which set
        ``libvirt_vm_name`` / ``vm_name`` / ``config_fpath`` but skip
        ``__init__``).
        """
        destroyer = getattr(self, "_destroyer", None)
        if destroyer is None:
            destroyer = _DomainDestroyer(
                self.libvirt_vm_name,
                self.vm_name,
                self.config_fpath,
            )
        return destroyer

    def exists_in_libvirt(self, uri: str) -> tuple[bool, str, str]:
        """Check whether this machine is defined in libvirt and report its state.

        Looks the domain up by ``self.libvirt_vm_name`` against the list of
        domains known to ``uri`` and, if found, queries its state and reason.

        Args:
            uri: A libvirt connection URI (e.g. ``qemu:///session``).

        Returns:
            A 3-tuple ``(exists, state, state_reason)``. ``state`` and
            ``state_reason`` are the lowercase virsh strings (``running``,
            ``shut off``, ``shutdown user``, etc.). Both are ``""`` when the
            domain is not defined.

        Raises:
            VirshError: If ``virsh`` itself fails (e.g. cannot reach the URI).
        """
        if self.libvirt_vm_name not in virsh_list_all_names(uri):
            return False, "", ""

        try:
            state, reason = virsh_domstate_reason(uri, self.libvirt_vm_name)
        except VirshError:
            # The domain disappeared between the list and the lookup. Treat as
            # absent rather than propagating a transient race.
            return False, "", ""

        return True, state, reason

    def _get_snapshots(self) -> _SnapshotManager:
        """Return this machine's snapshot manager, building one if absent.

        Constructed in ``__init__`` for normally-built machines; rebuilt on
        demand for test stubs created via ``object.__new__`` (which set
        ``libvirt_vm_name`` / ``vm_name`` but skip ``__init__``).
        """
        snapshots = getattr(self, "_snapshots", None)
        if snapshots is None:
            snapshots = _SnapshotManager(self.libvirt_vm_name, self.vm_name)
        return snapshots

    def list_snapshots(self, uri: str) -> list[str]:
        """Return the snapshot names defined for this machine's domain.

        Uses ``virsh snapshot-list --name`` so the result is a flat list of
        snapshot names in creation order. When the domain isn't defined at
        ``uri`` the list is empty — callers can treat "no domain" and
        "no snapshots" identically (the previous libvirt-python
        implementation did the same).

        Args:
            uri: A libvirt connection URI (e.g. ``qemu:///session``).

        Returns:
            Snapshot names in creation order. ``[]`` when the domain isn't
            defined or has no snapshots.

        Raises:
            VirshError: If ``virsh`` itself fails for a reason other than
                "domain not defined" (e.g. cannot reach the URI).
        """
        return self._get_snapshots().list(uri)

    def create_snapshot(
        self,
        uri: str,
        snapshot_name: str,
        snapshot_description: str | None = None,
    ) -> bool:
        """Create a snapshot of this machine's domain.

        Builds a minimal ``<domainsnapshot>`` XML document, writes it to a
        tempfile, and hands it to ``virsh snapshot-create --xmlfile``. The
        tempfile is removed after the call (or on exception) by
        :func:`tkc_lvlab.utils.virsh._xml_tempfile`.

        Args:
            uri: A libvirt connection URI (e.g. ``qemu:///session``).
            snapshot_name: Name to assign to the new snapshot.
            snapshot_description: Optional human-readable description.
                Defaults to ``"Snapshot of <libvirt_vm_name>"``.

        Returns:
            ``True`` on success. Failures raise rather than returning a
            sentinel — the previous libvirt-python implementation returned
            the exception instance from a ``finally`` block, which silently
            swallowed errors.

        Raises:
            VirshError: When the domain isn't defined at ``uri`` or when
                ``virsh snapshot-create`` itself fails (timeout, malformed
                XML, libvirt error, etc.).
        """
        return self._get_snapshots().create(uri, snapshot_name, snapshot_description)

    def delete_snapshot(self, uri: str, snapshot_name: str) -> None:
        """Delete a named snapshot from this machine's domain.

        Args:
            uri: A libvirt connection URI (e.g. ``qemu:///session``).
            snapshot_name: Name of the snapshot to delete.

        Raises:
            VirshError: When the domain isn't defined at ``uri``, when the
                named snapshot doesn't exist, or when ``virsh
                snapshot-delete`` itself fails. The previous libvirt-python
                implementation swallowed errors in a ``finally`` block; this
                port propagates them so callers get a clean signal.
        """
        self._get_snapshots().delete(uri, snapshot_name)

    def poweron(self, uri: str) -> int:
        """Start the virtual machine if it is currently shut off or crashed.

        Returns ``0`` on success or when the machine is already running (or
        absent — preserving the pre-port behavior of warning + returning 0).
        Returns ``1`` when the ``virsh start`` invocation fails.

        Args:
            uri: libvirt connection URI (e.g. ``qemu:///session``).

        Returns:
            ``0`` on success / no-op, ``1`` on ``VirshError``. The integer
            return type matches the ``> 0`` truthy check in ``cli.py``.
        """
        try:
            current_vms = virsh_list_all_names(uri)
        except VirshError as e:
            logger.error("Failed to list domains at %s: %s", uri, e)
            return 1

        if self.libvirt_vm_name not in current_vms:
            logger.warning(
                VM_DOES_NOT_EXIST_MSG,
                self.vm_name,
            )
            return 0

        try:
            vm_state = virsh_domstate(uri, self.libvirt_vm_name)
        except VirshError as e:
            logger.error("Failed to query state of %s: %s", self.vm_name, e)
            return 1

        if vm_state in DEAD_STATES:
            try:
                run_virsh(uri, ["start", self.libvirt_vm_name])
            except VirshError as e:
                logger.error("Failed to power on %s: %s", self.vm_name, e)
                return 1

        return 0

    def shutdown(self, uri: str) -> int:
        """Gracefully shut down the virtual machine if it is currently active.

        Active means any of ``running``, ``idle`` (i.e. blocked on resource),
        ``paused``, or ``pmsuspended``. Already-stopped domains are a no-op.

        Args:
            uri: libvirt connection URI (e.g. ``qemu:///session``).

        Returns:
            ``0`` on success / no-op, ``1`` on ``VirshError``. The integer
            return type matches the ``> 0`` truthy check in ``cli.py``.
        """
        try:
            current_vms = virsh_list_all_names(uri)
        except VirshError as e:
            logger.error("Failed to list domains at %s: %s", uri, e)
            return 1

        if self.libvirt_vm_name not in current_vms:
            logger.warning(
                "The virtual machine %s does not exist in Libvirt", self.vm_name
            )
            return 0

        try:
            vm_state = virsh_domstate(uri, self.libvirt_vm_name)
        except VirshError as e:
            logger.error("Failed to query state of %s: %s", self.vm_name, e)
            return 1

        if vm_state in SHUTDOWNABLE_STATES:
            try:
                run_virsh(uri, ["shutdown", self.libvirt_vm_name])
            except VirshError as e:
                logger.error("Error with machine.shutdown: %s", e)
                return 1

        return 0


def get_machine_by_vm_name(
    machines: list[dict[str, Any]], vm_name: str
) -> dict[str, Any] | None:
    """Find a machine dict in the manifest's machines list by ``vm_name``.

    Args:
        machines: The manifest's ``machines`` list — each entry is a
            dict from the parsed YAML.
        vm_name: The short name to match against each entry's
            ``vm_name`` field.

    Returns:
        The matching machine dict, or ``None`` if no entry has the
        requested ``vm_name``.
    """
    for machine in machines:
        if machine.get("vm_name", None) == vm_name:
            return machine
    return None
