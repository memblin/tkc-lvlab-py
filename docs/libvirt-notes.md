# Libvirt notes

A short hypervisor-side reference for the bits you reach for when a guest
`lvlab` created misbehaves. `lvlab` itself shells out to these tools.

## Common virsh one-liners

`lvlab` namespaces domains as `<vm_name>_<environment_name>`, so use that
full name with `virsh`. Add `-c qemu:///session` for rootless VMs.

```bash
# List every domain and its state
virsh -c qemu:///system list --all

# State of one domain
virsh -c qemu:///system domstate salt_libvirt-salt-dev

# Find a guest's IP from its DHCP lease (needs a managed network)
virsh -c qemu:///system domifaddr salt_libvirt-salt-dev

# Attach to the serial console — watch cloud-init / boot output live.
# Exit with Ctrl-].
virsh -c qemu:///system console salt_libvirt-salt-dev

# Dump the domain XML (NIC model, MAC, disk paths, ...)
virsh -c qemu:///system dumpxml salt_libvirt-salt-dev
```

Libvirt's per-domain log (boot/QEMU stderr) lives at
`/var/log/libvirt/qemu/<domain>.log` for `qemu:///system`.

## virt-install

```bash
# Show --network parameter options
virt-install --network=?

# Set extra params
virt-install ... --network bridge=br0,model=virtio,mac=52:54:00:c2:de:ce,address.type=pci,address.domain=0,address.bus=1,address.slot=0,address.function=0 ...
```

## qemu-guest-agent

The QEMU Guest Agent is similar to VMware Tools on a VMware VM: it gives the
hypervisor a channel to the guest for status and for dynamically-assigned
details like interface IP and MAC addresses.

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
