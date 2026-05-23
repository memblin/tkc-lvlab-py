# tkc_lvlab.utils.ssh_keys

SSH public-key discovery and validation. Ports the lvscripts behavior
for the standalone `createvm` workflow: walks `~/.ssh/`, `$SUDO_USER`'s
home, and `$HOME` to find `id_ed25519.pub` / `id_rsa.pub`, validates
each (7-type whitelist), de-duplicates.

::: tkc_lvlab.utils.ssh_keys
