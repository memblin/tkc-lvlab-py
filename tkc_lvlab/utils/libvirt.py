"""Module for libvirt related functions and classes"""

import glob
import os
import click
import libvirt
import subprocess
import sys

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
        # vm_name = machine.get("vm_name", machine.get("hostname", None) + machine.get("domain", None))
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

        self.vm_name = vm_name
        self.hostname = machine.get("hostname", None)
        self.domain = config_defaults.get("domain", None)
        # If we don't have an os by now set a default of Generic Linux 2022
        self.os = machine.get("os", config_defaults.get("os", "linux2022"))
        self.cpu = machine.get("cpu", config_defaults.get("cpu", 2))
        self.memory = machine.get("memory", config_defaults.get("memory", 2024))
        self.interfaces = machine.get("interfaces", [])
        self.nameservers = machine.get(
            "nameservers", config_defaults["interfaces"].get("nameservers", {})
        )
        self.disks = machine.get("disks", [])
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
        metadata_config = MetaData(self.hostname)
        rendered_metadata_config = metadata_config.render_config()
        metadata_config_fpath = os.path.join(self.config_fpath, "meta-data")
        click.echo(f"Writing cloud-init meta-data file {metadata_config_fpath}")
        with open(metadata_config_fpath, "w", encoding="utf-8") as metadata_config_file:
            metadata_config_file.write(rendered_metadata_config)

        # Render and write cloud-init: user-data
        userdata_config = UserData(
            config_defaults.get("cloud_init", {}), self.hostname, self.domain
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
            f"--name={self.vm_name}",
            f"--memory={self.memory}",
            f"--vcpus={self.cpu}",
            "--import",
            "--disk",
            f"path={os.path.join(config_path, 'disk0.qcow2')}",
            "--disk",
            f"path={os.path.join(config_path, 'cidata.iso') + ',device=cdrom'}",
            f"--os-variant={self.os}",
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

    def destroy(self, config_path, uri):
        """Destroy a machine by shutting it down, undefining it, and deleting the directory"""
        conn = connect_to_libvirt(uri)

        current_vms = [dom.name() for dom in conn.listAllDomains()]

        if self.vm_name in current_vms:
            vm = conn.lookupByName(self.vm_name)
            vm_status, _ = get_domain_state_string(vm.state())

            if vm_status in ["Running"]:
                click.echo(f"Destroying {self.vm_name}")
                if vm.destroy() > 0:
                    click.echo(
                        f"Failed to destroy (forcefully shutdown) {self.vm_name}"
                    )
                vm_status, _ = get_domain_state_string(vm.state())

            if vm_status in ["Shut Off", "Crashed"]:
                if vm.undefine() > 0:
                    click.echo(
                        f"Failed to undefine (remove from Libvirt) {self.vm_name} "
                    )
                else:
                    try:
                        machine_files = glob.glob(os.path.join(self.config_fpath, "*"))
                        for file in machine_files:
                            if os.path.isfile(file):
                                os.remove(file)
                            elif os.path.isdir(file):
                                os.rmdir(file)

                    except Exception as e:
                        click.echo(f"Exception when deleting: {e}")
        else:
            click.echo(
                f"The virtual machine {self.vm_name} does not exist in this Libvirt URI"
            )

        conn.close()

    def exists_in_libvirt(self, uri):
        """Virtual machine existance and status"""
        exists, status, status_reason = (False, 0, 0)

        conn = connect_to_libvirt(uri)
        current_vms = [dom.name() for dom in conn.listAllDomains()]

        if self.vm_name in current_vms:
            vm = conn.lookupByName(self.vm_name)
            status, status_reason = get_domain_state_string(vm.state())
            exists = True

        conn.close()
        return exists, status, status_reason

    def poweron(self, uri):
        """Powreon a virtual machine"""
        create_status = 0
        conn = connect_to_libvirt(uri)
        current_vms = [dom.name() for dom in conn.listAllDomains()]

        if self.vm_name in current_vms:
            vm = conn.lookupByName(self.vm_name)
            vm_status, _ = get_domain_state_string(vm.state())

            if vm_status in ["Shut Off", "Crashed"]:
                create_status = vm.create()
                vm_status, _ = get_domain_state_string(vm.state())

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

        if self.vm_name in current_vms:
            vm = conn.lookupByName(self.vm_name)
            vm_status, _ = get_domain_state_string(vm.state())

            if vm_status in ["Running"]:
                shutdown_status = vm.shutdown()
                vm_status, _ = get_domain_state_string(vm.state())

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


def get_domain_state_string(state):
    """Humanize the current state of the domain."""

    # https://libvirt.org/html/libvirt-libvirt-domain.html#virDomainState
    vir_domain_state = {
        libvirt.VIR_DOMAIN_NOSTATE: "No State",
        libvirt.VIR_DOMAIN_RUNNING: "Running",
        libvirt.VIR_DOMAIN_BLOCKED: "Blocked",
        libvirt.VIR_DOMAIN_PAUSED: "Paused",
        libvirt.VIR_DOMAIN_SHUTDOWN: "Shutting Down",
        libvirt.VIR_DOMAIN_SHUTOFF: "Shut Off",
        libvirt.VIR_DOMAIN_CRASHED: "Crashed",
        libvirt.VIR_DOMAIN_PMSUSPENDED: "Suspended by Power Management",
    }

    # https://libvirt.org/html/libvirt-libvirt-domain.html#virDomainShutoffReason
    vir_domain_shutoff_reason = {
        libvirt.VIR_DOMAIN_SHUTOFF_UNKNOWN: "the reason is unknown",
        libvirt.VIR_DOMAIN_SHUTOFF_SHUTDOWN: "normal shutdown",
        libvirt.VIR_DOMAIN_SHUTOFF_DESTROYED: "forced poweroff",
        libvirt.VIR_DOMAIN_SHUTOFF_CRASHED: "domain crashed",
        libvirt.VIR_DOMAIN_SHUTOFF_MIGRATED: "migrated to another host",
        libvirt.VIR_DOMAIN_SHUTOFF_SAVED: "saved to a file",
        libvirt.VIR_DOMAIN_SHUTOFF_FAILED: "domain failed to start",
        libvirt.VIR_DOMAIN_SHUTOFF_FROM_SNAPSHOT: "restored from a snapshot which was taken while domain was shutoff",
        libvirt.VIR_DOMAIN_SHUTOFF_DAEMON: "daemon decided to kill domain during reconnection processing",
    }

    # https://libvirt.org/html/libvirt-libvirt-domain.html#virDomainRunningReason
    vir_domain_running_reason = {
        libvirt.VIR_DOMAIN_RUNNING_BOOTED: "normal startup from boot",
        libvirt.VIR_DOMAIN_RUNNING_CRASHED: "resumed from crashed",
        libvirt.VIR_DOMAIN_RUNNING_FROM_SNAPSHOT: "restored from snapshot",
        libvirt.VIR_DOMAIN_RUNNING_MIGRATED: "migrated from another host",
        libvirt.VIR_DOMAIN_RUNNING_MIGRATION_CANCELED: "returned from migration",
        libvirt.VIR_DOMAIN_RUNNING_POSTCOPY: "running in post-copy migration mode",
        libvirt.VIR_DOMAIN_RUNNING_POSTCOPY_FAILED: "running in failed post-copy migration",
        libvirt.VIR_DOMAIN_RUNNING_RESTORED: "restored from a state file",
        libvirt.VIR_DOMAIN_RUNNING_SAVE_CANCELED: "returned from failed save process",
        libvirt.VIR_DOMAIN_RUNNING_UNKNOWN: "Unknown",
        libvirt.VIR_DOMAIN_RUNNING_UNPAUSED: "returned from paused state",
        libvirt.VIR_DOMAIN_RUNNING_WAKEUP: "returned from pmsuspended due to wakeup event",
    }

    vir_domain_state = vir_domain_state.get(state[0], "Unknown State")

    vir_domain_state_reason = "Unsupported Reason"
    if state[0] == libvirt.VIR_DOMAIN_RUNNING:
        vir_domain_state_reason = vir_domain_running_reason.get(
            state[1], "Unknown Reason"
        )
    elif state[0] == libvirt.VIR_DOMAIN_SHUTOFF:
        vir_domain_state_reason = vir_domain_shutoff_reason.get(
            state[1], "Unknown Reason"
        )

    return vir_domain_state, vir_domain_state_reason


def get_machine_by_vm_name(machines, vm_name):
    """Get a machine by vm_name from the machines list"""
    for machine in machines:
        if machine.get("vm_name", None) == vm_name:
            return machine
    return None
