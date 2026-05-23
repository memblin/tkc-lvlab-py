# tkc_lvlab.utils.snapshot_cleanup

Snapshot deletion and `virsh undefine` with the lvscripts-style
fallback for the backing-chain `--children` → `--metadata` case.
Used by `destroyvm` and (in a follow-up) `Machine.destroy`.

::: tkc_lvlab.utils.snapshot_cleanup
