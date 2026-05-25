# Cloud-init examples

`lvlab` renders three cloud-init files per machine and packs them
into a NoCloud `cidata.iso` that the VM consumes on first boot:

- `meta-data` — instance ID + hostname.
- `user-data` — users, SSH keys, sudo policy, `runcmd` payload.
- `network-config` — interface addressing and DNS.

These rendered files are useful both as a debugging aid
(`lvlab cloudinit <vm_name>` prints what would be written without
deploying) and as a reference for hand-rolling cloud-init payloads
outside of `lvlab`. The examples below are the minimum-viable shapes
each file takes.

## meta-data

```yaml
instance-id: iid-{hostname-or-other-instance-id}
local-hostname: {hostname}
```

## user-data

```yaml
#cloud-config

# manage_etc_hosts defaults to true on many OSes but recent Debian 12
# cloud images have it disabled — add it explicitly.
manage_etc_hosts: true
hostname: "{hostname}"
fqdn: "{hostname}{domain}"

users:
  - name: root
    ssh_authorized_keys:
      - ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... throwaway@example
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
    shell: /bin/bash

runcmd:
  - curl -s -o /root/bootstrap-salt.sh -L https://bootstrap.saltproject.io
  - chmod 0755 /root/bootstrap-salt.sh
  - /root/bootstrap-salt.sh -X -M -A 127.0.0.1 stable 3007
  - systemctl start salt-master
  - systemctl start salt-minion
```

The `root` user is used here on purpose for local lab VMs: it avoids
bumping the UID/GID count, which some configuration-management
automation relies on (a service-user-creation sequence that's
sensitive to the starting UID). For non-lab use, add a normal user
instead — that's the username you'd SSH in as with the matching
private key.

## network-config (v1, ENI-style)

The v1 template selects when an image entry sets
`network_version: 1`. It uses the classic ENI vocabulary and binds
to a specific kernel device name, which is fine for distros that
emit predictable names matching your manifest.

```yaml
network:
  version: 1
  config:
    - type: physical
      name: enp1s0
      subnets:
         - type: static
           address: 192.168.122.10/24
           gateway: 192.168.122.1
    - type: nameserver
      address:
        - 192.168.122.1
      search:
        - local
        - example.lab
```

## network-config (v2, netplan)

The v2 template selects when an image entry sets
`network_version: 2`. It matches each NIC by
`match.driver: virtio_net` (every lvlab NIC is `model=virtio`) and
configures it under whatever name the distro assigns — it does **not**
rename the interface. (`set-name` was removed: netplan renaming breaks
interface bring-up under systemd-networkd on Debian/Ubuntu, so the guest
never gets a DHCP lease.) The manifest's `iface.name` is just the
netplan stanza key (a label); the in-guest device keeps its kernel name
(`enp1s0` / `ens3` / `eth0`).

```yaml
network:
  version: 2
  ethernets:
    eth0:
      match:
        driver: virtio_net
      dhcp4: false
      dhcp6: false
      addresses:
        - 192.168.122.10/24
      nameservers:
        search: [local, example.lab]
        addresses: [192.168.122.1]
      routes:
        - to: 0.0.0.0/0
          via: 192.168.122.1
```

`match.driver: virtio_net` is reliable for a single NIC per VM.
Multi-NIC manifests are a documented limitation — a driver match
selects *every* virtio NIC, so it can't disambiguate more than one.
Multi-NIC needs per-interface MAC matching, which is not yet
supported in `lvlab`.

## See also

- [`network-config.v1.j2`](https://github.com/memblin/tkc-lvlab-py/blob/main/src/tkc_lvlab/templates/network-config.v1.j2)
    and
    [`network-config.v2.j2`](https://github.com/memblin/tkc-lvlab-py/blob/main/src/tkc_lvlab/templates/network-config.v2.j2)
    — the Jinja sources `lvlab` actually renders.
- [Walkthrough](walkthrough.md) — what `lvlab cloudinit <vm_name>`
    does, and where the rendered files end up on disk.
