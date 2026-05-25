# tkc_lvlab.scripts.createvm

Standalone `createvm` console script — a faithful port of the
`lvscripts-py` reference. Orchestrates dependency check → image resolve →
network validation → SSH-key discovery → password generation → cloud-init
render → disk copy → `virt-install` for a one-off libvirt VM. Resolves the
positional `VM_DISTRO` against its built-in catalog merged with the
`images:` section of an `Lvlab.yml` in the current directory (or
`--config`).

::: tkc_lvlab.scripts.createvm
