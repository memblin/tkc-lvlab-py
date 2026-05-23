# `tkc_lvlab.utils.libvirt`

Manifest-side `Machine` class and lookup helper — every `lvlab` command
constructs a `Machine` from the parsed-manifest tuple and dispatches
operations against `self.libvirt_vm_name`. The hypervisor side is
invoked via the `virsh` subprocess wrapper in
`tkc_lvlab.utils.virsh`; no `libvirt-python` C extension is required.

The libvirt domain name is **not** `vm_name` — it is
`f"{vm_name}_{environment_name}"` (see `libvirt_vm_name`). This
namespacing is what lets multiple lvlab environments coexist on one
hypervisor.

::: tkc_lvlab.utils.libvirt
