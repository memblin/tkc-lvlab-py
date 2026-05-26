# Example manifest

A complete, annotated `Lvlab.yml`. Copy
[`Lvlab.example.yml`](Lvlab.example.yml) verbatim as a starting point
and edit in place — every section is annotated, and the comments call
out the gotchas that bite first-time users.

## What this manifest demonstrates

- **A single environment** (`libvirt-salt-dev`) with `qemu:///system`
    as the libvirt URI.
- **`config_defaults`** that apply to every machine — CPU, memory,
    one default disk, one default interface, cloud-init credentials.
- **Six machines** exercising a representative spread: Debian 12
    (static), Debian 13 (static and DHCP), AlmaLinux 10 (static),
    Fedora 44 (static), and one user-mode VM.
- **An `images:` block cataloguing all eight supported cloud images** —
    Debian 11/12/13, AlmaLinux 9/10, Ubuntu 22.04/24.04, and Fedora 44 —
    plus two custom intranet examples. A machine only needs an entry for
    the image its `os:` names; the rest are there to copy from.
- **Cross-distro interface matching.** Each interface is keyed `eth0` (a
    netplan label) and bound by MAC address, so the same manifest works on
    every distro without guessing `enp1s0` vs `eth0`. See
    [Cloud-init examples](cloud-init-examples.md#network-config-v2-netplan)
    for why MAC matching — not driver or device-name matching — is what
    makes this portable.
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
