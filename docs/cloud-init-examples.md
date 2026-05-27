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

## Cross-distro `runcmd`: trusting a custom CA

`runcmd` is delivered to the guest verbatim and runs late in first
boot. cloud-init does **not** run it under `set -e`: a failing command
is logged and the rest of the list keeps going. That is convenient, but
it means a command that only exists on one distro family fails
*silently* on the others — the SSH key and other steps still land, so it
looks like "most of it worked" while one step quietly did nothing.

Installing a private CA into the system trust store is the classic case,
because the tool and the anchor directory differ by family:

| Family                                 | Anchor directory                                                  | Refresh command          |
| -------------------------------------- | ----------------------------------------------------------------- | ------------------------ |
| Debian, Ubuntu                         | `/usr/local/share/ca-certificates/` (file **must** end in `.crt`) | `update-ca-certificates` |
| Fedora, AlmaLinux, Rocky, CentOS, RHEL | `/etc/pki/ca-trust/source/anchors/`                               | `update-ca-trust`        |

A Debian-style CA install (`update-ca-certificates` writing to
`/usr/local/share/ca-certificates/`) therefore does nothing on a RHEL
guest: the anchor directory is absent (so `curl -o` can't even write the
file) and the command does not exist. Branch on which refresh tool is
present so one `runcmd` covers both families:

```yaml
runcmd:
  # Install a private root CA, choosing the anchor dir + refresh tool for
  # the running distro family. -k is needed only here because the CA is
  # not yet trusted at the moment we download it (chicken-and-egg).
  - |
    if command -v update-ca-trust >/dev/null 2>&1; then
      # RHEL family: Fedora, AlmaLinux, Rocky, CentOS, RHEL
      curl -fsSk -L -o /etc/pki/ca-trust/source/anchors/root-ca.crt https://ca.example.lab/root-ca.pem
      update-ca-trust
    else
      # Debian family: Debian, Ubuntu (filename must end in .crt)
      curl -fsSk -L -o /usr/local/share/ca-certificates/root-ca.crt https://ca.example.lab/root-ca.pem
      update-ca-certificates
    fi
```

Probing for `update-ca-trust` cleanly separates the two toolchains for
every distro in `lvlab`'s built-in image catalog. You would only need a
third branch if you add SUSE, Arch, or Alpine images: those use
different anchor directories (and SUSE even reuses the
`update-ca-certificates` *name* for a different layout), so key the
branch off `/etc/os-release` `ID`/`ID_LIKE` instead of a command probe in
that case.

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
`network_version: 2`. It matches each NIC by `match.macaddress` — `lvlab`
pins a deterministic MAC per interface and passes the same address to
both `virt-install` and this config — and configures it under whatever
name the distro assigns. It does **not** rename the interface.
(`set-name` was removed: netplan renaming breaks interface bring-up under
systemd-networkd on Debian/Ubuntu, so the guest never gets a DHCP lease.)
The manifest's `iface.name` is just the netplan stanza key (a label); the
in-guest device keeps its kernel name (`enp1s0` / `ens3` / `eth0`).

MAC matching (rather than matching by driver) is what makes this work on
**every** distro. cloud-init renders this v2 document to the guest's
native format: a verbatim netplan file on Debian/Ubuntu, but a
NetworkManager keyfile on Fedora/RHEL — and the NetworkManager renderer
ignores a `match: driver`, binding the profile to a literal interface
name taken from the stanza label instead. That silently left Fedora's
`enp1s0` static config unbound (it fell back to DHCP). A MAC match binds
the right NIC under both renderers.

```yaml
network:
  version: 2
  ethernets:
    eth0:
      match:
        macaddress: "52:54:00:1a:2b:3c"
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

The MAC is quoted because an all-numeric MAC would otherwise be misread
as a YAML base-60 integer. Because each NIC matches its own MAC, this
also disambiguates multiple NICs in principle (the old driver match
could not) — though multi-NIC manifests are not yet exercised
end-to-end in `lvlab`.

## See also

- [`network-config.v1.j2`](https://github.com/memblin/tkc-lvlab-py/blob/main/src/tkc_lvlab/templates/network-config.v1.j2)
    and
    [`network-config.v2.j2`](https://github.com/memblin/tkc-lvlab-py/blob/main/src/tkc_lvlab/templates/network-config.v2.j2)
    — the Jinja sources `lvlab` actually renders.
- [Walkthrough](walkthrough.md) — what `lvlab cloudinit <vm_name>`
    does, and where the rendered files end up on disk.
