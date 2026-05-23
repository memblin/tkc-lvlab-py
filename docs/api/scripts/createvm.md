# tkc_lvlab.scripts.createvm

Standalone `createvm` console script. Orchestrates dependency check →
image resolve → network validation → SSH-key discovery → password
generation → cloud-init render → disk create → `virt-install` for a
one-off libvirt VM. Does not read `Lvlab.yml`.

::: tkc_lvlab.scripts.createvm
