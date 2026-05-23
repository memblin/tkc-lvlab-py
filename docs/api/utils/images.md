# tkc_lvlab.utils.images

Cloud-image download + GPG-and-checksum verification. Used by both the
manifest workflow (`lvlab init`, `lvlab up`) and the standalone
`createvm` script — both pin verification through the same `CloudImage`
to avoid divergent trust paths.

::: tkc_lvlab.utils.images
