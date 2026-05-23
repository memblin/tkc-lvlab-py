# tkc_lvlab.scripts.destroyvm

Standalone `destroyvm` console script — companion to `createvm`.
Translates the user-supplied short name to `oneoff-<vm_name>` before
any libvirt lookup, so manifest VMs sharing the short name stay
invisible.

::: tkc_lvlab.scripts.destroyvm
