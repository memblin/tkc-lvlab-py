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

- Install the repo latest release into a native Python 3 venv

```bash
# Create a venv if you don't have one you already want to use
python -m venv ~/.venvs/lvlab

# Activate the venv
source ~/.venvs/lvlab/bin/activate

# Use pip to install the release wheel for the latest package
pip install https://github.com/memblin/tkc-lvlab-py/releases/download/0.2.1/tkc_lvlab-0.2.1-py3-none-any.whl

# lvlab should be ready for use
lvlab up salt.local

# Show the VM w/ virsh
virsh -c qemu:///system list
```

- Option B: Clone this repo local, build with `poetry build` and install the wheel from the ./dist directory.

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
- Utilities: virt-install, qemu-img (For now until we can implement these
              parts via libvirt-python)
- `cloud_image_basedir` and `disk_image_basedir` configuration paths need
  to be writable by the user to run w/o sudo.
  - I usually create these directories in advance and chown them for my
    user.

### Ubuntu 22.04

These packages are required to install and use the lvlab application.

```bash
# installs qemu-kvm, libvirt, python3, git, and 2 dependencies needed by python-libvirt
apt install qemu-system-x86 libvirt-daemon-system virt-manager python3 python3-venv python3-pip git pkg-config libvirt-dev
```
