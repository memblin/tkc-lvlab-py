# tkc_lvlab.utils.catalog

The built-in cloud-image catalog (`BUILTIN_IMAGES`) and the shared
image-entry resolution used by **both** deploy paths — the standalone
[`createvm`](../scripts/createvm.md) script and the manifest `Machine`
flow. `os_variant` and the first-boot username are derived from the
image key unless the entry overrides them, so a custom or oddly-keyed
image pins those once and both paths honour it.

::: tkc_lvlab.utils.catalog
