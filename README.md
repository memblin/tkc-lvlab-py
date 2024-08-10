# TKC Labs : Libvirt Labs

The Libvirt Labs project provides the `lvlab` Python application which can be
used to manage Libvirt based development environments in a familiar way.

If you are wondering why I would write this, the long [answer is here](docs/Why.md)?

## Usage

With `lvlab` one can create VMs automatically from a YAML syntax manifest
configuration that defines the environment, the virtual machines, and
the base image information.

- [Example YAML config file](docs/Lvlab.yml.example)

## Help Output

```console
Usage: lvlab [OPTIONS] COMMAND [ARGS]...

  A command-line tool for managing VMs.

Options:
  --help  Show this message and exit.

Commands:
  capabilities  Hypervisor Capabilities
  cloudinit     Render the cloud-init template for a machine defined in...
  destroy       Destroy a Virtual machine listed in the LvLab manifest
  down          Shutdown a machine defined in the Lvlab.yml manifest.
  init          Initialize the environment.
  status        Show the status of the environment.
  up            Start a machine defined in the Lvlab.yml manifest.
```

## Current Functionality is MVP

> [!WARNING]  
> Very much a minimum viable product, it barely works. Do not use this
> if you have important VMs in libvirt on your dev machine.

- Initializing the environment (init) is working
  - Downloads images defined in environment config
  - Validates checksum if URL provided
  - Validates checksum hash file of GPG fie provided (Fedora)
- Create (up), Destroy (destroy), Startup (up), and Shutdown (down) of VMs
  is working
- Re-rendering of cloud-init templates is functional (cloudinit)
- Status command output is very limited
- Cloud init templating is very limited
- Cleanup is still a manual thing and since Libvirt and QEMU sometimes
  adjust disk image permissions this can require sudo.
- Many many things have limited error checking support; expect crashes
  when permissions or config isn't just right.

## Requirements

- Libvirt w/ QEMU configured and functional
- The user should be a member of the `libvirt` group on the system
- Utilities: virt-install, qemu-img (For now until we can implement these
              parts via libvirt-python)
- `cloud_image_basedir` and `disk_image_basedir` configuration paths need
  to be writable by the user to run w/o sudo.
  - I usually create these directories in advance and chown them for my
    user.
