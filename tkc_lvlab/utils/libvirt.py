"""Module for libvirt related functions and classes"""

import libvirt


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
