# Libvirt Notes

## virt-install

Getting help:

```bash
# Show --network parameter options
virt-install --network=?

# Set extra params
virt-install ... --network bridge=br0,model=virtio,mac=52:54:00:c2:de:ce,address.type=pci,address.domain=0,address.bus=1,address.slot=0,address.function=0 ...
```

## qemu-guest-agent

The qemu-guest-agent is similar to VMware Tools installed on a VMware VMs.

The QEMU Guest Agent provides the hypervisor a way to communicate with the
VM for providing statuses and things like interface IP and MAC addresses
which are normally dynamically generates.

### Fedora / RHEL

```bash
# Usually already installed and active on the Fedora public cloud images
dnf install qemu-guest-agent
systemctl enable --now qemu-guest-agent.service
```

### Debian / Ubuntu

```bash
# Is not installed by default on Debian 12
apt update
apt install -y qemu-guest-agent
systemctl enable --now qemu-guest-agent.service
```
