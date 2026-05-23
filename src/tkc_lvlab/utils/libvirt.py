"""Manifest-side ``Machine`` class and lookup helpers.

This module owns the manifest-driven side of lvlab — every command in
:mod:`tkc_lvlab.cli` constructs a :class:`Machine` from the
parsed-manifest tuple and dispatches operations against
``self.libvirt_vm_name``. The hypervisor side is invoked via
:mod:`tkc_lvlab.utils.virsh` (a thin ``subprocess.run`` wrapper); no
``libvirt-python`` import lives in this module — Phase 2 removed that
C-extension dependency.

The standalone one-off workflow (``createvm`` / ``destroyvm``) does not
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
import sys
from typing import TYPE_CHECKING, Any

from .._logging import get_logger
from ..config import parse_config, generate_hosts
from .subprocess_env import system_first_env
from .vdisk import VirtualDisk
from .cloud_init import MetaData, NetworkConfig, UserData
from .network import NETWORK_TYPES, USER_MODE_NETWORK_TYPES
from .virsh import (
    DEAD_STATES,
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


def _virt_install_network_arg(iface: dict[str, Any]) -> str:
    """Build the ``virt-install --network`` argument for one interface.

    The default (``network_type="network"``) emits a managed-network
    arg with a fixed PCI address — matching pre-Phase-12 behaviour so
    existing manifests are unchanged. ``user`` and ``passt`` emit the
    user-mode forms, which don't take a libvirt network name and
    don't need a fixed PCI address (virt-install picks one). User-mode
    is required for ``qemu:///session`` where rootless libvirt cannot
    manage a NAT network.

    Args:
        iface: The merged interface dict from ``Machine.interfaces``
            (already had ``config_defaults['interfaces']`` applied).

    Returns:
        The exact string to pass after ``--network`` on the
        ``virt-install`` command line.
    """
    network_type = iface.get("network_type", "network")
    if network_type == "user":
        return "user,model=virtio"
    if network_type == "passt":
        return "passt,model=virtio"
    libvirt_network = iface.get("network", "default")
    return (
        f"network={libvirt_network},model=virtio,"
        "address.type=pci,address.domain=0,address.bus=1,"
        "address.slot=0,address.function=0"
    )


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
        nameservers: Resolved nameservers dict — per-machine override
            with fallback to ``config_defaults['interfaces']['nameservers']``.
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
    ) -> None:

        # Apply interface defaults
        for index, iface in enumerate(machine.get("interfaces", [])):
            machine["interfaces"][index] = {
                **config_defaults.get("interfaces", {}),
                **iface,
            }

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
            if network_type in USER_MODE_NETWORK_TYPES and iface.get("ip4"):
                raise ValueError(
                    f"Interface {iface.get('name', '<unnamed>')!r} declares "
                    f"network_type={network_type!r} together with a static "
                    f"ip4 ({iface['ip4']!r}). User-mode networking "
                    f"(SLIRP/passt) does not honour static IPs — remove the "
                    f"ip4 field or switch to network_type='network'."
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
        self.nameservers = machine.get(
            "nameservers", config_defaults["interfaces"].get("nameservers", {})
        )
        self.disks = machine.get("disks", [])
        self.shared_directories = machine.get("shared_directories", [])
        self.cloud_init_config = machine.get("cloud_init", {})
        self.config_fpath = config_fpath

    def cloud_init(
        self,
        cloud_image: "CloudImage",
        config_defaults: dict[str, Any],
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

        Returns:
            ``(meta_data_path, user_data_path, network_config_path)`` —
            the three rendered file paths. Callers feed these to
            :class:`tkc_lvlab.utils.cloud_init.CloudInitIso`.

        Raises:
            ValueError: When :attr:`os` does not match a known
                ``/etc/cloud/templates/hosts.*.tmpl`` distro family.
                Extend ``template_file_mapping`` in the method body to
                add a new family.
            SystemExit: When :attr:`config_fpath` cannot be created or
                when ``parse_config`` returns ``None``. This preserves
                the historical "log + exit" behavior — future cleanup
                may replace it with a raised exception.
        """
        if not os.path.exists(self.config_fpath):
            try:
                os.makedirs(self.config_fpath)
            except Exception as e:  # pylint: disable=broad-except
                logger.error("Exception creating %s: %s", self.config_fpath, e)
                sys.exit(1)

        # Render and write cloud-init: network-config
        network_config = NetworkConfig(
            cloud_image.network_version, self.interfaces, self.nameservers
        )
        rendered_network_config = network_config.render_config()
        network_config_fpath = os.path.join(self.config_fpath, "network-config")
        logger.info("Writing cloud-init network config file %s", network_config_fpath)
        with open(network_config_fpath, "w", encoding="utf-8") as network_config_file:
            network_config_file.write(rendered_network_config)

        # Render and write cloud-init: meta-data
        metadata_config = MetaData(self.libvirt_vm_name, self.fqdn)
        rendered_metadata_config = metadata_config.render_config()
        metadata_config_fpath = os.path.join(self.config_fpath, "meta-data")
        logger.info("Writing cloud-init meta-data file %s", metadata_config_fpath)
        with open(metadata_config_fpath, "w", encoding="utf-8") as metadata_config_file:
            metadata_config_file.write(rendered_metadata_config)

        # Render and write cloud-init: user-data
        cloud_init_defaults = config_defaults.get("cloud_init", {})

        # Apply cloud_init defaults
        if self.cloud_init_config.get("runcmd_ignore_defaults", False):
            logger.debug(
                "Ignoring config_defaults:cloud_init:runcmd for %s", self.vm_name
            )
            cloud_init_defaults_filtered = {
                k: v for k, v in cloud_init_defaults.items() if k != "runcmd"
            }
            cloud_init_config = {
                **cloud_init_defaults_filtered,
                **self.cloud_init_config,
            }
        else:
            logger.debug(
                "Including config_defaults:cloud_init:runcmd for %s", self.vm_name
            )
            cloud_init_config = {**cloud_init_defaults, **self.cloud_init_config}

            if "runcmd" in cloud_init_defaults and "runcmd" in self.cloud_init_config:
                cloud_init_config["runcmd"] = (
                    cloud_init_defaults["runcmd"] + self.cloud_init_config["runcmd"]
                )
            elif "runcmd" in cloud_init_defaults:
                cloud_init_config["runcmd"] = cloud_init_defaults["runcmd"]

        # Append /etc/hosts snippet to machine user-data runcmd list
        try:
            _, _, _, machines = parse_config()
        except TypeError as e:
            logger.error("Could not parse config file.")
            sys.exit()

        hosts_snippet = generate_hosts(
            self.environment, config_defaults, machines, heredoc="/etc/hosts"
        )

        # We need to append entries to the correct cloud-init hosts template
        # file in /etc/cloud/templates too.
        template_file_mapping = {
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
        template_fpath = None

        for template, distros in template_file_mapping.items():
            for distro in distros:
                if self.os.lower().startswith(distro.lower()):
                    template_fpath = "/etc/cloud/templates/" + template
                    break
            if template_fpath:
                break

        if not template_fpath:
            raise ValueError(f"Could not find a template file for {self.os}")

        hosts_template_snippet = generate_hosts(
            self.environment, config_defaults, machines, heredoc=template_fpath
        )

        if "runcmd" not in cloud_init_config:
            cloud_init_config["runcmd"] = []

        # Append the hosts_snippet to the top of the runcmd list so /etc/hosts gets
        # populated first as the runcmd is processed.
        cloud_init_config["runcmd"] = (
            [hosts_snippet] + [hosts_template_snippet] + cloud_init_config["runcmd"]
        )

        userdata_config = UserData(
            cloud_init_config, self.hostname, self.domain, self.fqdn
        )
        rendered_userdata_config = userdata_config.render_config()
        userdata_config_fpath = os.path.join(self.config_fpath, "user-data")
        logger.info("Writing cloud-init user-data file %s", userdata_config_fpath)
        with open(userdata_config_fpath, "w", encoding="utf-8") as userdata_config_file:
            userdata_config_file.write(rendered_userdata_config)

        return metadata_config_fpath, userdata_config_fpath, network_config_fpath

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
    ) -> bool:
        """Define and start the libvirt domain via ``virt-install``.

        Builds the ``virt-install`` command from this machine's
        resolved config: memory, vCPUs, the qcow2 at
        ``<config_path>/disk0.qcow2``, the cloud-init ISO at
        ``<config_path>/cidata.iso``, the first interface's libvirt
        network, and the ``--os-variant`` derived by splitting
        :attr:`os` on ``-`` and taking the first segment.

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
            f"--os-variant={self.os.split('-')[0]}",
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
        steps (this preserves the previous "stop at first error" behavior).

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
            logger.warning(
                "The virtual machine %s does not exist in this Libvirt URI",
                self.vm_name,
            )
            return False

        try:
            vm_state = virsh_domstate(uri, self.libvirt_vm_name)
        except VirshError as e:
            logger.error("Failed to query state of %s: %s", self.vm_name, e)
            return False

        # Force-off if the domain is still running. virsh destroy is the
        # power-cord-pull equivalent; the previous libvirt-python code called
        # this on RUNNING or PAUSED.
        if vm_state in {"running", "paused"}:
            logger.warning("Forcefully shutting down %s", self.vm_name)
            try:
                run_virsh(uri, ["destroy", self.libvirt_vm_name])
            except VirshError as e:
                logger.error("Failed to forcefully shutdown %s: %s", self.vm_name, e)
                return False
            try:
                vm_state = virsh_domstate(uri, self.libvirt_vm_name)
            except VirshError as e:
                logger.error(
                    "Failed to query state of %s after destroy: %s", self.vm_name, e
                )
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

        # Delete every snapshot before undefining. ``virsh undefine`` will
        # refuse if snapshot metadata exists, so this is mandatory cleanup,
        # not a courtesy.
        try:
            snapshots = virsh_snapshot_names(uri, self.libvirt_vm_name)
        except VirshError as e:
            logger.error("Failed to list snapshots for %s: %s", self.vm_name, e)
            return False

        if snapshots:
            logger.warning("Deleting all snapshots for %s", self.vm_name)
            for snap in snapshots:
                logger.info("Deleting snapshot %s", snap)
                try:
                    run_virsh(uri, ["snapshot-delete", self.libvirt_vm_name, snap])
                except VirshError as e:
                    logger.error(
                        "Failed to delete snapshot %s of %s: %s",
                        snap,
                        self.vm_name,
                        e,
                    )
                    return False

        logger.info("Undefining %s", self.vm_name)
        try:
            run_virsh(uri, ["undefine", self.libvirt_vm_name])
        except VirshError as e:
            logger.error(
                "Failed to undefine (remove from Libvirt) %s: %s", self.vm_name, e
            )
            return False

        # Remove on-disk machine files. Mirrors the previous behavior: a
        # filesystem error here logs and returns False even though libvirt
        # state has already been cleaned up.
        try:
            machine_files = glob.glob(os.path.join(self.config_fpath, "*"))
            for file in machine_files:
                if os.path.isfile(file):
                    logger.info("Removing file %s.", file)
                    os.remove(file)
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Exception when removing machine files %s", e)
            return False

        try:
            # Check if the directory is empty and then remove it
            if not os.listdir(self.config_fpath):
                logger.info("Removing machine directory %s.", self.config_fpath)
                os.rmdir(self.config_fpath)
            else:
                logger.warning("Machine directory %s is not empty.", self.config_fpath)
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Exception when removing machine files %s", e)
            return False

        return True

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
        if self.libvirt_vm_name not in virsh_list_all_names(uri):
            return []

        try:
            return virsh_snapshot_names(uri, self.libvirt_vm_name)
        except VirshError:
            # The domain disappeared between the list and the snapshot-list
            # call. Treat as absent — caller wanted snapshot names, there
            # are none to report.
            return []

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
        if self.libvirt_vm_name not in virsh_list_all_names(uri):
            logger.warning(
                "The virtual machine %s does not exist in this Libvirt URI",
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
        if self.libvirt_vm_name not in virsh_list_all_names(uri):
            logger.warning(
                "The virtual machine %s does not exist in this Libvirt URI",
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
                "The virtual machine %s does not exist in this Libvirt URI",
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
