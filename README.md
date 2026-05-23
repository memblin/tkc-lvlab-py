# TKC Labs : Libvirt Labs

The Libvirt Labs project provides the `lvlab` Python application which can be
used to manage Libvirt based development environments in a familiar way.

If you are wondering why I would write this, the long [answer is here](docs/Why.md)?

> [!WARNING]
> Very much a minimum viable product, it barely works. Do not use this
> if you have important VMs in libvirt on your dev machine. Don't let
> a bug ruin your day.

## Usage

With `lvlab` one can create VMs automatically from a YAML syntax manifest
configuration that defines the environment, the virtual machines, and
the base image information.

- [Example YAML config file](docs/Lvlab.example.yml)
- [In-depth Example Repo](https://github.com/memblin/lvlab-examples)

## Installation

- Install the latest release wheel as an isolated tool with [uv](https://docs.astral.sh/uv/):

```bash
# Install uv if you don't already have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install the release wheel directly
uv tool install https://github.com/memblin/tkc-lvlab-py/releases/download/0.2.4/tkc_lvlab-0.2.4-py3-none-any.whl

# lvlab should be ready for use
lvlab up salt.local

# Show the VM w/ virsh
virsh -c qemu:///system list
```

- Option B: Clone this repo, build a wheel with `uv build`, and `uv tool install ./dist/tkc_lvlab-*.whl`.

- Option C (for active development): Clone, then `uv sync` to create a dev env and `uv run lvlab --help` to invoke the CLI from a checkout.

The same wheel installs three console scripts on your PATH:

- `lvlab` — manifest workflow (drives `Lvlab.yml`).
- `createvm` — one-off VM creation. Does not read `Lvlab.yml`.
- `destroyvm` — one-off VM removal (matches `createvm`). Does not see manifest VMs.

## One-off VMs

When you want a single VM without writing an `Lvlab.yml` manifest, use
`createvm` / `destroyvm`. They share the same library code as `lvlab`
but are intentionally separate at the command-line surface — they will
not read your manifest and will not touch any VM `lvlab` manages.

```bash
# Create a one-off VM. Domain name on libvirt is "oneoff-testvm.local"
# (the oneoff- prefix is what keeps it distinguishable from lvlab's
# <vm_name>_<env> manifest VMs).
sudo createvm testvm.local --distro debian12

# Same, but with a static IP (validated against the network's
# subnet + DHCP range before any VM state is written).
sudo createvm testvm.local --distro debian13 --ip4 192.168.122.50

# Same, but with a standalone qcow2 disk (cp + qemu-img resize) so
# you can wipe and re-init the cloud-images dir later without
# breaking this VM.
sudo createvm testvm.local --distro debian12 --copy

# Destroy a one-off VM. Errors out if the libvirt domain doesn't
# carry the oneoff- prefix, so you can't accidentally remove a
# manifest VM by typing the short name.
sudo destroyvm testvm.local --force
```

See `createvm --help` and `destroyvm --help` for the full flag list.

## Help Output

```console
Usage: lvlab [OPTIONS] COMMAND [ARGS]...

  A command-line tool for managing VMs.

Options:
  -h, --help  Show this message and exit.

Commands:
  capabilities  Hypervisor Capabilities
  cloudinit     Render the cloud-init template for a machine defined in...
  destroy       Destroy a Virtual machine listed in the LvLab manifest
  down          Shutdown a machine defined in the Lvlab.yml manifest.
  hosts         Provide /etc/hosts support
  init          Initialize the environment.
  snapshot      Snapshot management commands.
  status        Show the status of the environment.
  up            Start a machine defined in the Lvlab.yml manifest.
```

## Requirements

- Libvirt w/ QEMU configured and functional
- The user should be a member of the `libvirt` group on the system
- Utilities: `virsh`, `virt-install`, `qemu-img`. `lvlab` shells out to all
    three; there is no longer a `libvirt-python` C-extension dependency, so
    `libvirt-dev` / `pkg-config` are not needed at build time.
- `cloud_image_basedir` and `disk_image_basedir` configuration paths need
    to be writable by the user to run w/o sudo.
    - I usually create these directories in advance and chown them for my
        user.

### Ubuntu 22.04 / 24.04

These packages are required to install and use the lvlab application.

```bash
# qemu-kvm, libvirt daemon + client tools (provides `virsh`), virt-install,
# Python 3, and git. No libvirt-dev / pkg-config since the libvirt-python
# build-time dep was dropped in 0.2.x.
apt install qemu-system-x86 libvirt-daemon-system libvirt-clients virtinst python3 python3-venv git
```
