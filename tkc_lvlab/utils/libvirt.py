"""Module for libvirt related functions and classes"""

import os
import click
import libvirt
import subprocess


class Machine:
    """Libvirt Lab Virtual Machine"""

    def __init__(self, machine, config_defaults):

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

        self.vm_name = machine.get("vm_name", machine.get("hostname", None) + machine.get("domain", None))
        self.hostname = machine.get("hostname", None)
        self.domain = config_defaults.get("domain", None)
        # If we don't have an os by now set a default of Generic Linux 2022
        self.os = machine.get("os", config_defaults.get("os", "linux2022"))
        self.cpu = machine.get("cpu", config_defaults.get("cpu", 2))
        self.memory = machine.get("memory", config_defaults.get("memory", 2024))
        self.interfaces = machine.get("interfaces", [])
        self.disks = machine.get("disks", [])

    # virt-install --name=ns02.tkclabs.io --memory 4096 --vcpus=2 --import --disk path=/var/lib/libvirt/images/ns02.tkclabs.io/disk0.qcow2 \
    # --disk path=/var/lib/libvirt/images/ns02.tkclabs.io/cidata.iso,device=cdrom --os-variant=fedora40 --network network=vlan10,model=virtio \
    # --graphics vnc,listen=0.0.0.0 --noautoconsole
    def deploy(self, config_path, uri):
        """Use virt-install to create a virtual machine"""
        command = [
            "virt-install",
            f"--connect={uri}",
            f"--name={self.hostname}.{self.domain}",
            f"--memory={self.memory}",
            f"--vcpus={self.cpu}",
            "--import",
            "--disk",
            f"path={os.path.join(config_path, 'disk0.qcow2')}",
            "--disk",
            f"path={os.path.join(config_path, 'cidata.iso') + ',device=cdrom'}",
            f"--os-variant={self.os}",
            "--network",
            f"network={self.interfaces[0].get("network", "default")},model=virtio",
            "--graphics",
            "vnc,listen=0.0.0.0",
            "--noautoconsole"
        ]

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
        conn = connect_to_libvirt(uri)

        # Get a list of current VMs
        current_vms = [dom.name() for dom in conn.listAllDomains()]

        if self.vm_name in current_vms:
            click.echo(f"Found virtual machine {self.vm_name} ")

            vm = conn.lookupByName(self.vm_name)
            vm_status, vm_status_reason = get_domain_state_string(vm.state())
            click.echo(f"Current Status: {vm_status}\nCurrent Status Reason: {vm_status_reason}")

            if vm_status in ["Running"]:
                click.echo(f"Destroying {self.vm_name}")
                if vm.destroy() > 0:
                    click.echo(f"Failed to destroy (forcefully shutdown) {self.vm_name}")

            if vm_status in ["Shut Off", "Crashed"]:
                click.echo(f"The virtual machine {self.vm_name} is not running, removing it from Libvirt")
                if vm.undefine() > 0:
                    return False
                return True

        else:
            click.echo(f"The virtual machine {self.vm_name} does not exist in this Libvirt URI")

        conn.close()


    def shutdown(self, uri):
        conn = connect_to_libvirt(uri)

        # Get a list of current VMs
        current_vms = [dom.name() for dom in conn.listAllDomains()]

        if self.vm_name in current_vms:
            click.echo(f"Found virtual machine {self.vm_name} ")

            vm = conn.lookupByName(self.vm_name)
            vm_status, vm_status_reason = get_domain_state_string(vm.state())
            click.echo(f"Current Status: {vm_status}\nCurrent Status Reason: {vm_status_reason}")

            if vm_status in ["Running"]:
                click.echo(f"Attempting to Shutdown {self.vm_name}")
                if vm.shutdown() > 0:
                    return False
                
                return True

            elif vm_status in ["Shut Off", "Crashed"]:
                click.echo(f"The virtual machine {self.vm_name} is not Running.")
                return False

        else:
            click.echo(f"The virtual machine {self.vm_name} does not exist in this Libvirt URI")

        conn.close()




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

    vir_domain_state = vir_domain_state.get(state[0], "Unknown State")
    vir_domain_state_reason = vir_domain_shutoff_reason.get(state[1], "Unkown Reason")

    return vir_domain_state, vir_domain_state_reason


def get_machine_by_vm_name(machines, vm_name):
    """Get a machine by vm_name from the machines list"""
    click.echo(f"Looking for machine {vm_name}")
    for machine in machines:
        if machine.get("vm_name", None) == vm_name:
            return machine
    return None
