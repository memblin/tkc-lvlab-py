# tkc_lvlab.scripts.deletevm

Standalone `deletevm` console script — companion to `createvm` and a
faithful port of the `lvscripts-py` `deletevm` command. Acts on the raw
libvirt domain name: it looks the domain up by the exact name passed,
destroys and undefines it, and removes the one-off storage directory if
one exists. It does no `Lvlab.yml` translation, so a short manifest name
won't resolve, but a manifest VM's full `<vm_name>_<env>` domain name is
removed if passed. `lvlab destroy` is the manifest-scoped counterpart.

::: tkc_lvlab.scripts.deletevm
