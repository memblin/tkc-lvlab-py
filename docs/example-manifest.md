# Example manifest

A complete `Lvlab.yml` covering each supported host OS. Copy
[`Lvlab.example.yml`](Lvlab.example.yml) verbatim as a starting point
and edit in place — every section is annotated, and the comments call
out the gotchas that bite first-time users.

## What this manifest demonstrates

- **A single environment** (`libvirt-salt-dev`) with `qemu:///system`
    as the libvirt URI.
- **`config_defaults`** that apply to every machine — CPU, memory,
    one default disk, one default interface, cloud-init credentials.
- **Five machines** spanning every supported guest OS in the
    validated matrix: Debian 12, Debian 13 (static + DHCP), AlmaLinux
    10, Fedora 44.
- **Cross-distro interface matching.** Each interface is keyed `eth0` in
    the manifest (a netplan label). The v2 (netplan) network-config
    template matches the NIC by its MAC address (`lvlab` pins one per
    interface and passes the same address to `virt-install`) and configures
    it under its distro-assigned name — no rename — so the same manifest
    works on every distro without guessing `enp1s0` vs `eth0`. MAC matching
    is the only selector cloud-init honours on both its netplan and
    NetworkManager renderers.
- **One user-mode-networking VM** (`rootless.local`) for use on
    `qemu:///session` where rootless libvirt can't manage a NAT
    network.
- **Custom intranet image entries** at the end of the `images` block —
    these illustrate the `{os-variant}-{anything}` naming requirement
    for custom images and point at a placeholder intranet server.
    Replace the URLs with your own if you keep a custom image
    library.

## File

```yaml
--8<-- "docs/Lvlab.example.yml"
```

## Next steps

- Walk through what each `lvlab` subcommand actually does to your
    hypervisor: [Walkthrough](walkthrough.md).
- Inspect the cloud-init payloads `lvlab` generates per machine:
    [Cloud-init examples](cloud-init-examples.md).
- For a richer end-to-end project that uses `lvlab` to provision a
    multi-VM Salt lab, see
    [memblin/lvlab-examples](https://github.com/memblin/lvlab-examples).
