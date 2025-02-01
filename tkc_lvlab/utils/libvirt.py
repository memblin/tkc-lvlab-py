"""Module for libvirt related functions and classes"""

import glob
import os
import click
import libvirt
import re
import subprocess
import sys

from ..config import parse_config, generate_hosts
from .vdisk import VirtualDisk
from .cloud_init import MetaData, NetworkConfig, UserData


class Machine:
    """Libvirt Lab Virtual Machine"""

    def __init__(self, machine, environment, config_defaults):

        # Apply interface defaults
        for index, iface in enumerate(machine.get("interfaces", [])):
            machine["interfaces"][index] = {
                **config_defaults.get("interfaces", {}),
                **iface,
            }

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
        self.cloud_init_config = machine.get("cloud_init", {})
        self.config_fpath = config_fpath

    def cloud_init(self, cloud_image, config_defaults):
        """Render Cloud Init configuraion files"""
        if not os.path.exists(self.config_fpath):
            try:
                os.makedirs(self.config_fpath)
            except Exception as e:  # pylint: disable=broad-except
                click.echo(f"Exception creating : {e}")
                sys.exit(1)

        # Render and write cloud-init: network-config
        network_config = NetworkConfig(
            cloud_image.network_version, self.interfaces, self.nameservers
        )
        rendered_network_config = network_config.render_config()
        network_config_fpath = os.path.join(self.config_fpath, "network-config")
        click.echo(f"Writing cloud-init network config file {network_config_fpath}")
        with open(network_config_fpath, "w", encoding="utf-8") as network_config_file:
            network_config_file.write(rendered_network_config)

        # Render and write cloud-init: meta-data
        metadata_config = MetaData(self.libvirt_vm_name, self.fqdn)
        rendered_metadata_config = metadata_config.render_config()
        metadata_config_fpath = os.path.join(self.config_fpath, "meta-data")
        click.echo(f"Writing cloud-init meta-data file {metadata_config_fpath}")
        with open(metadata_config_fpath, "w", encoding="utf-8") as metadata_config_file:
            metadata_config_file.write(rendered_metadata_config)

        # Render and write cloud-init: user-data
        cloud_init_defaults = config_defaults.get("cloud_init", {})

        # Apply cloud_init defaults
        if self.cloud_init_config.get("runcmd_ignore_defaults", False):
            click.echo(f"Ignoring config_defaults:cloud_init:runcmd for {self.vm_name}")
            cloud_init_defaults_filtered = {
                k: v for k, v in cloud_init_defaults.items() if k != "runcmd"
            }
            cloud_init_config = {
                **cloud_init_defaults_filtered,
                **self.cloud_init_config,
            }
        else:
            click.echo(
                f"Including config_defaults:cloud_init:runcmd for {self.vm_name}"
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
            click.echo("Could not parse config file.")
            sys.exit()

        hosts_snippet = generate_hosts(
            self.environment, config_defaults, machines, heredoc="/etc/hosts"
        )

        # We need to append entries to the correct cloud-init hosts template
        # file in /etc/cloud/templates too.
        template_file_mapping = {
            "hosts.debian.tmpl": ["debian", "ubuntu"],
            "hosts.redhat.tmpl": ["fedora", "rockylinux", "almalinux", "rhel"],
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
        click.echo(f"Writing cloud-init user-data file {userdata_config_fpath}")
        with open(userdata_config_fpath, "w", encoding="utf-8") as userdata_config_file:
            userdata_config_file.write(rendered_userdata_config)

        return metadata_config_fpath, userdata_config_fpath, network_config_fpath

    def create_vdisks(self, environment={}, config_defaults={}, cloud_image=None):
        """Create all machine virtual disks"""

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
                click.echo(f"Virtual Disk: {vdisk.name} exists at {vdisk.fpath}")
            else:
                click.echo(f"Creating Virtual Disk: {vdisk.fpath} at {vdisk.size}")
                if vdisk.create():
                    if vdisk.exists():
                        click.echo(f"Virtual Disk Created Successfully")
                else:
                    click.echo(f"Failed to create Virtual Disk: {vdisk.fpath}")

    def delete_vdisks(self, environment={}, config_defaults={}, cloud_image=None):
        """Delete all machine virtual disks"""

        for index, disk in enumerate(self.disks):
            vdisk = VirtualDisk(
                self.hostname,
                disk,
                index,
                cloud_image,
                environment,
                config_defaults,
            )

            if vdisk.exists():
                click.echo(f"Virtual Disk: {vdisk.name} exists at {vdisk.fpath}")
                if vdisk.delete():
                    if vdisk.exists():
                        click.echo(f"Deletion of virtual disk appears to have failed.")
                    else:
                        click.echo(f"Deletion of virtual disk successful.")
            else:
                click.echo(
                    f"Virtual Disk: {vdisk.name} does not exist at {vdisk.fpath}"
                )

    def deploy(self, config_path, config_defaults, uri):
        """Use virt-install to create a virtual machine"""
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
            f'network={self.interfaces[0].get("network", "default")},model=virtio,address.type=pci,address.domain=0,address.bus=1,address.slot=0,address.function=0',
            "--graphics",
            "vnc,listen=0.0.0.0",
            "--noautoconsole",
        ]

        # Extend the command to enable shared_directories if found in the config
        if config_defaults.get("shared_directories", None):
            command.append("--memorybacking=source.type=memfd,access.mode=shared")
            for filesystem in config_defaults.get("shared_directories", None):
                command.append(
                    f'--filesystem={filesystem["source"]},{filesystem["mount_tag"]},driver.type=virtiofs'
                )

        try:
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            return True
        except subprocess.CalledProcessError as e:
            click.echo(f"Error in virt-install call: {e}")
            click.echo(f"{' '.join(command)}")
            return False

    def destroy(self, uri):
        """Destroy a machine by shutting it down, undefining it, and deleting the directory

        config_path is to be used in the fugure
        """
        conn = connect_to_libvirt(uri)

        current_vms = [dom.name() for dom in conn.listAllDomains()]

        if self.libvirt_vm_name in current_vms:
            vm = conn.lookupByName(self.libvirt_vm_name)
            vm_state, _, _, _ = get_machine_state(vm.state())

            if vm_state in ["VIR_DOMAIN_RUNNING", "VIR_DOMAIN_PAUSED"]:
                click.echo(f"Forcefully shutting down {self.vm_name}")
                if vm.destroy() > 0:
                    click.echo(f"Failed to forcefully shutdown {self.vm_name}")
                vm_state, _, _, _ = get_machine_state(vm.state())

            if vm_state in ["VIR_DOMAIN_SHUTOFF", "VIR_DOMAIN_CRASHED"]:
                if vm.hasCurrentSnapshot():
                    click.echo(f"Deleting all snapshots for {self.vm_name}")
                    for snapshot in vm.listAllSnapshots():
                        click.echo(f"Deleting snapshot {snapshot.getName()}")
                        snapshot.delete()

                click.echo(f"Undefining {self.vm_name}")
                if vm.undefine() > 0:
                    click.echo(
                        f"Failed to undefine (remove from Libvirt) {self.vm_name} "
                    )
                else:
                    # Done with libvirt connection
                    conn.close()
                    try:
                        machine_files = glob.glob(os.path.join(self.config_fpath, "*"))
                        for file in machine_files:
                            if os.path.isfile(file):
                                click.echo(f"Removing file {file}.")
                                os.remove(file)
                    except Exception as e:
                        click.echo(f"Exception when removing machine files {e}")
                        return False

                    try:
                        # Check if the directory is empty and then remove it
                        if not os.listdir(self.config_fpath):
                            click.echo(
                                f"Removing machine directory {self.config_fpath}."
                            )
                            os.rmdir(self.config_fpath)
                        else:
                            click.echo(
                                f"Machine directory {self.config_fpath} is not empty."
                            )
                    except Exception as e:
                        click.echo(f"Exception when removing machine files {e}")
                        return False

        else:
            click.echo(
                f"The virtual machine {self.vm_name} does not exist in this Libvirt URI"
            )
            conn.close()
            return False

        return True

    def exists_in_libvirt(self, uri):
        """Virtual machine existance and status"""
        exists, state, state_reason = (False, 0, 0)

        conn = connect_to_libvirt(uri)
        current_vms = [dom.name() for dom in conn.listAllDomains()]

        if self.libvirt_vm_name in current_vms:
            vm = conn.lookupByName(self.libvirt_vm_name)
            state, state_reason, _, _ = get_machine_state(vm.state())
            exists = True

        conn.close()
        return exists, state, state_reason

    def list_snapshots(self, uri):
        """List snapshots for a virtual machine"""
        snapshots = []
        conn = connect_to_libvirt(uri)
        current_vms = [dom.name() for dom in conn.listAllDomains()]

        if self.libvirt_vm_name in current_vms:
            vm = conn.lookupByName(self.libvirt_vm_name)
            snapshots = vm.listAllSnapshots()

        conn.close()
        return snapshots

    def create_snapshot(self, uri, snapshot_name, snapshot_description=None):
        """Create a snapshot of a virtual machine with optional description"""
        snapshot_status = 0
        conn = connect_to_libvirt(uri)
        current_vms = [dom.name() for dom in conn.listAllDomains()]

        if self.libvirt_vm_name in current_vms:
            vm = conn.lookupByName(self.libvirt_vm_name)

            if not snapshot_description:
                snapshot_description = f"Snapshot of {vm.name()}"

            snapshot_xml = f"""
            <domainsnapshot>
                <name>{snapshot_name}</name>
                <description>Snapshot of {vm.name()}</description>
            </domainsnapshot>
            """

            try:
                snapshot_status = vm.snapshotCreateXML(snapshot_xml, 0)
            except libvirt.libvirtError as e:
                snapshot_status = e
            finally:
                conn.close()
                return snapshot_status

    def delete_snapshot(self, uri, snapshot_name):
        """Create a snapshot of a virtual machine with optional description"""
        snapshot_status = 0
        conn = connect_to_libvirt(uri)
        current_vms = [dom.name() for dom in conn.listAllDomains()]

        if self.libvirt_vm_name in current_vms:
            try:
                vm = conn.lookupByName(self.libvirt_vm_name)
                snapshot = vm.snapshotLookupByName(snapshot_name)

                if snapshot:
                    snapshot_status = snapshot.delete()
            except libvirt.libvirtError as e:
                snapshot_status = e
            finally:
                conn.close()
                return snapshot_status

    def poweron(self, uri):
        """Powreon a virtual machine"""
        create_status = 0
        conn = connect_to_libvirt(uri)
        current_vms = [dom.name() for dom in conn.listAllDomains()]

        if self.libvirt_vm_name in current_vms:
            vm = conn.lookupByName(self.libvirt_vm_name)
            vm_state, _, _, _ = get_machine_state(vm.state())

            if vm_state in ["VIR_DOMAIN_SHUTOFF", "VIR_DOMAIN_CRASHED"]:
                create_status = vm.create()

        else:
            click.echo(
                f"The virtual machine {self.vm_name} does not exist in this Libvirt URI"
            )

        conn.close()
        return create_status

    def shutdown(self, uri):
        """Shutdown the virtual machine"""
        shutdown_status = 0
        conn = connect_to_libvirt(uri)

        # Get a list of current VMs
        current_vms = [dom.name() for dom in conn.listAllDomains()]

        if self.libvirt_vm_name in current_vms:
            vm = conn.lookupByName(self.libvirt_vm_name)
            vm_state, _, _, _ = get_machine_state(vm.state())

            if vm_state in [
                "VIR_DOMAIN_RUNNING",
                "VIR_DOMAIN_BLOCKED",
                "VIR_DOMAIN_PAUSED",
                "VIR_DOMAIN_PMSUSPENDED",
            ]:
                shutdown_status = vm.shutdown()

            if shutdown_status > 0:
                click.echo(f"Error with machine.shutdown")

        else:
            click.echo(f"The virtual machine {self.vm_name} does not exist in Libvirt")

        conn.close()
        return shutdown_status


def connect_to_libvirt(uri=None):
    """Connect to Hypervisor"""
    if uri == None:
        uri = "qemu:///session"

    conn = libvirt.open(uri)
    if not conn:
        raise SystemExit(f"Failed to open connection to {uri}")

    return conn


def _humanize_machine_status(state: str, state_reason: str) -> tuple:
    """Convert status constant value into something more descriptive"""

    vir_domain_state_descriptions = {
        # https://libvirt.org/html/libvirt-libvirt-domain.html#virDomainState
        "VIR_DOMAIN_NOSTATE": "no state",
        "VIR_DOMAIN_RUNNING": "the machine is running",
        "VIR_DOMAIN_BLOCKED": "the machine is blocked on resource",
        "VIR_DOMAIN_PAUSED": "the machine is paused by user",
        "VIR_DOMAIN_SHUTDOWN": "the machine is being shut down",
        "VIR_DOMAIN_SHUTOFF": "the machine is shut off",
        "VIR_DOMAIN_CRASHED": "the machine is crashed",
        "VIR_DOMAIN_PMSUSPENDED": "the machine is suspended by guest power management",
    }

    vir_domain_state_reason_descriptions = {
        # https://libvirt.org/html/libvirt-libvirt-domain.html#virDomainRunningReason
        "VIR_DOMAIN_RUNNING_UNKNOWN": "Unknown",
        "VIR_DOMAIN_RUNNING_BOOTED": "normal startup from boot",
        "VIR_DOMAIN_RUNNING_MIGRATED": "migrated from another host",
        "VIR_DOMAIN_RUNNING_RESTORED": "restored from a state file",
        "VIR_DOMAIN_RUNNING_FROM_SNAPSHOT": "restored from snapshot",
        "VIR_DOMAIN_RUNNING_UNPAUSED": "returned from paused state",
        "VIR_DOMAIN_RUNNING_MIGRATION_CANCELED": "returned from migration",
        "VIR_DOMAIN_RUNNING_SAVE_CANCELED": "returned from failed save process",
        "VIR_DOMAIN_RUNNING_WAKEUP": "returned from pmsuspended due to wakeup event",
        "VIR_DOMAIN_RUNNING_CRASHED": "resumed from crashed",
        "VIR_DOMAIN_RUNNING_POSTCOPY": "running in post-copy migration mode",
        "VIR_DOMAIN_RUNNING_POSTCOPY_FAILED": "running in failed post-copy migration",
        # https://libvirt.org/html/libvirt-libvirt-domain.html#virDomainShutdownReason
        "VIR_DOMAIN_SHUTDOWN_UNKNOWN": "the reason is unknown",
        "VIR_DOMAIN_SHUTDOWN_USER": "shutting down on user request",
        # https://libvirt.org/html/libvirt-libvirt-domain.html#virDomainShutoffReason
        "VIR_DOMAIN_SHUTOFF_UNKNOWN": "the reason is unknown",
        "VIR_DOMAIN_SHUTOFF_SHUTDOWN": "normal shutdown",
        "VIR_DOMAIN_SHUTOFF_DESTROYED": "forced poweroff",
        "VIR_DOMAIN_SHUTOFF_CRASHED": "machine crashed",
        "VIR_DOMAIN_SHUTOFF_MIGRATED": "migrated to another host",
        "VIR_DOMAIN_SHUTOFF_SAVED": "saved to a file",
        "VIR_DOMAIN_SHUTOFF_FAILED": "machine failed to start",
        "VIR_DOMAIN_SHUTOFF_FROM_SNAPSHOT": "restored from a snapshot which was taken while machine was shutoff",
        "VIR_DOMAIN_SHUTOFF_DAEMON": "daemon decided to kill machine during reconnection processing",
    }

    # Lookup the state in vir_domain_state_descriptions, if not found use the
    # state as-is, a constant is better than nothing.
    status = vir_domain_state_descriptions.get(state, state)
    # Same for state_reason
    reason = vir_domain_state_reason_descriptions.get(state_reason, state_reason)

    return status, reason


def get_machine_state(state: tuple) -> tuple:
    """Gets the current state of the domain.

    https://libvirt.org/html/libvirt-libvirt-domain.html#virDomainState
    https://libvirt.org/html/libvirt-libvirt-domain.html#virDomainShutoffReason
    https://libvirt.org/html/libvirt-libvirt-domain.html#virDomainRunningReason

    """
    # Get possible domain states from the library
    vir_domain_states = {}
    # https://docs.python.org/3/library/functions.html#vars
    for k, v in vars(libvirt).items():
        if re.match("VIR_DOMAIN_[A-Z]+$", k):
            vir_domain_states[v] = k

    # Get possible domain state reasons from the library
    vir_domain_state_reasons = {}
    for vir_domain_state in vir_domain_states.items():
        pattern = vir_domain_state[1] + "_[A-Z]+$"
        reason = {}
        # https://docs.python.org/3/library/functions.html#vars
        for k, v in vars(libvirt).items():
            if re.match(pattern, k):
                reason[v] = k
                vir_domain_state_reasons[vir_domain_state[0]] = reason

    machine_state = vir_domain_states.get(state[0], "Unknown State")
    machine_state_reason = vir_domain_state_reasons[state[0]][state[1]]
    status, reason = _humanize_machine_status(machine_state, machine_state_reason)

    return machine_state, machine_state_reason, status, reason


def get_machine_by_vm_name(machines, vm_name):
    """Get a machine by vm_name from the machines list"""
    for machine in machines:
        if machine.get("vm_name", None) == vm_name:
            return machine
    return None
