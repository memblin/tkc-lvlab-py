# `tkc_lvlab.cli`

The Typer-based `lvlab` console-script entry point. Each subcommand
parses `Lvlab.yml`, resolves a machine by name, builds a
`tkc_lvlab.utils.libvirt.Machine`, and dispatches against the libvirt
URI from the manifest.

The hypervisor side is invoked via `tkc_lvlab.utils.virsh` — a thin
`subprocess.run` wrapper around `virsh`. No `libvirt-python` C
extension is required.

The standalone one-off workflow (`createvm` / `deletevm` console
scripts in `tkc_lvlab.scripts`) does not flow through this module —
they have their own Typer entry points that talk to virsh directly.

::: tkc_lvlab.cli
