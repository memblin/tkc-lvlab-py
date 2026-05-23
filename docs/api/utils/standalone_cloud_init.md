# tkc_lvlab.utils.standalone_cloud_init

Cloud-init artifact builders for the standalone `createvm` workflow.
Handles multi-key `ssh_authorized_keys` and the generated password
hash — fields that the manifest-shaped `UserData` in
`tkc_lvlab.utils.cloud_init` does not.

::: tkc_lvlab.utils.standalone_cloud_init
