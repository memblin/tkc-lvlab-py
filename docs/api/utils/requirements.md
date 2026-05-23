# tkc_lvlab.utils.requirements

Host-binary dependency check for the standalone `createvm` script.
Surfaces a single `DependencyError` with a package-manager-specific
install hint when any required binary (`virsh`, `qemu-img`,
`virt-install`, `openssl`) is missing.

::: tkc_lvlab.utils.requirements
