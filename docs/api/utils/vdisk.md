# tkc_lvlab.utils.vdisk

Per-VM qcow2 disk creation and lifecycle for the manifest workflow.
Every disk references the verified cloud image as its `qemu-img -b`
backing file. The standalone `createvm` workflow has its own disk
creation logic (see [`tkc_lvlab.scripts.createvm`](../scripts/createvm.md)).

::: tkc_lvlab.utils.vdisk
