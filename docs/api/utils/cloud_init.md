# tkc_lvlab.utils.cloud_init

Manifest-side cloud-init artifact builders and ISO writer. The
manifest workflow's `UserData` reads `cloud_init.pubkey` as a single
key (string or path-to-file). For the multi-key + password-hash
shape the standalone workflow needs, see
[`tkc_lvlab.utils.standalone_cloud_init`](standalone_cloud_init.md).

::: tkc_lvlab.utils.cloud_init
