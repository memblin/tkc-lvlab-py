# tkc_lvlab.utils.virsh

Subprocess wrapper around the `virsh` binary. All Phase 2+ libvirt
operations in the project go through `run_virsh` so they share locale
locking (`LC_ALL=C`/`LANG=C`), timeout handling, and the `VirshError`
boundary type.

::: tkc_lvlab.utils.virsh
