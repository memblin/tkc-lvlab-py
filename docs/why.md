# Why lvlab?

`lvlab` exists to make libvirt+QEMU-KVM a low-friction target for **local
integration testing of configuration-management code** — Salt, Ansible, and
the like — on a Linux workstation. It replaces a drawer full of ad-hoc
`qemu-img`, `genisoimage`, and cloud-init snippets with one declarative
`Lvlab.yml`.

## The workflow it replaces

Libvirt with QEMU-KVM is a fast, capable hypervisor for testing infrastructure
automation when containers (Podman, Docker, Kubernetes) don't fit the bill.
The cost is the on-ramp: once you've learned `virsh`, `qemu-img`, and
cloud-init well enough to script VM creation, you tend to accumulate a
collection of scripts, snippets, and gists to drive the toolkit — and managing
that collection gets cumbersome as it grows. This project started from a pile
of 20+ `qemu-img` / `genisoimage` / cloud-init template variations that were
mixed and matched by hand to stand up test VMs.

## Why not Vagrant?

Vagrant is an excellent, easy, multi-platform option — and on macOS/Windows
it's often the faster path. Two things push it out of the way here:

- **VirtualBox feels sluggish on Linux** compared to libvirt QEMU-KVM, and
    VirtualBox and libvirt can't run VMs at the same time — a problem when you
    need companion service VMs running alongside guests that don't test well
    under VirtualBox.
- **The libvirt provider needs libvirt-format boxes.** Most published Vagrant
    boxes target the VirtualBox provider only, so using Vagrant with libvirt
    usually means building and publishing your own box (or building for both
    providers to keep the image identical). That's friction `lvlab` avoids by
    provisioning directly from upstream cloud images.

## Scope

Libvirt is a Linux/FreeBSD (and, in theory, macOS-via-Homebrew) tool — if
that's not your platform, `lvlab` isn't for you, and Vagrant is the better
call. Where libvirt *is* available, `lvlab` aims to be the functional,
efficient front-end for spinning lab VMs up and down.
