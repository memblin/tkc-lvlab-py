# TKC Labs : Libvirt Labs -local

Heavily opinianated intro...

Libvirt with QEMU-KVM has been my go-to for testing infrastructure automation
when Podman, Docker, or Kubernetes won't fit the bill for various reasons.
However, it can be cumbersome for new users to get familiar with the Libvirt
tools when options like Vagrant exist and can often get you testing faster.

Vagrant, generally easy to use and multi-platform, is an excellent option.
Most of the projects I see using Vagrant also use the VirtualBox provider. It
works well yet I dislike needing to use Virtualbox if I have a Linux
workstation or Development VM I'm working from. Primarily this is because
VirtualBox VMs, on Linux specifically, seem sluggish to me when compared to a
Libvirt QEMU-KVM VMs.

I've tried using Vagrant with the Libvirt provider and it does work but the
projects I've seen that use Vagrant already generally use an off-the-shelf
Vagrant Box that's only available for the VirtualBox provider. That means
building and publishing an identicle box of your own for the Libvirt provider
or possibly even building for both providers so the image is exactly the same.
That's an additional difficulty for me using Vagrant and VirtualBox. If those
things were easier or not applicable in-project then Vagrant is an awesome
solution.

## Hurdles with Libvirt and the goal of Libvirt Labs

The biggest hurdle; if you're not on Linux, FreeBSD, or MacOS I don't think
Libvirt is an option for you. I've never tried to run it on MacOS, on Mac I
normally go for Vagrant and VirtualBox.

If it is an option for you and you aquire the foundational knowledge of tools
such as `virsh`, `qemu-img`, and cloud-init configs you might find yourself
with a collection of scripts, snippets, and gists to facilitate automating
the toolkit. Managing that collection can become cumbersome and time consuming
as the collection grows.

This utility aims to create a more functional and efficient approach to using
Libvirt for local testing needs.

## Usage

With `lvlab` one can create VMs automatically from a YAML syntax manifest
configuration that defines the environment, the virtual machines, and
the base image information.

- Functionality TODO
  - Create a set of VMs from yaml def
    - Create XML definitions from templates
      - Include config syntax to locally mount shared dirs
      - Do we always provide a ${PWD} mount like /srv/lvlab to put the repo
        content into each VM similar to Vagrant /vargrant?
    - Create cloud-init configurations from templates
  - Create host file content from the yaml def
    - Will update /etc/cloud/templates/hosts.$OS.tmpl and /etc/hosts

- Commands to implement
  - lvlab init
    - create paths
    - get cloud-images
    - create VM qemu images
    - create VM cloud-init isos
 
  - lvlab up <vm> (define, start)
  - lvlab destroy <vm> (destroy, undefine)
  - lvlab shutdown <vm> (shutdown)
  - lvlab start <vm> (start)
  - lvlab reboot <vm> (reboot)
  - lvlab reset <vm> (reset)
  - lvlab list|status
    - list machines in the configured LvLab file with status info
  - Later on can we,
    - lvlab ssh <vm>
    - lvlab ssh-config <vm>

### LvLab file example

```yaml
environment:
  - name: libvirt-salt-dev
    config_defaults:
      domain: lo.local
      os: fedora40
      cpu: 2
      memory: 2048
      disk: 25G
      user: root
      privkey: path/to/ssh/privkey
      pubkey: path/to/ssh/pubkey
      interfaces:
        - enp1s0:
          network: default
        - eth0:
          network: default
      disk_image_dir: /var/lib/libvirt/images
      shared_directories:
        # requires mounting in guest like:
        # mount -t virtiofs gitrepos /srv/git
        - source: /home/crow/repos
          mount_tag: gitrepos
    machines:
      - hostname: salt
        os: fedora40
        interfaces:
          - enp1s0:
            ip4: 192.168.122.12/24
      - hostname: vault
        os: fedora40
        interfaces:
          - enp1s0:
            ip4: 192.168.122.13/24
      - hostanme: podman
        os: fedora40
        cpu: 4
        disk: 50G
        interfaces:
          - eth0:
            - ip4: 192.168.122.14/24

images:
  - name: fedora40
    url: https://to-image-download
    path: /var/lib/libvirt/images/cloud-images/image_name.qcow2
    cloud_init_network_version: 2
```
