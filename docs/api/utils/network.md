# tkc_lvlab.utils.network

Libvirt network introspection and static-IP validation. Parses
`virsh net-dumpxml` output into a typed `LibvirtNetworkInfo` and
exposes the NAT-vs-bridge forward-mode policy used by the standalone
`createvm` workflow.

::: tkc_lvlab.utils.network
