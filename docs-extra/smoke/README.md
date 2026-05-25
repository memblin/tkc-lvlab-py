# Manifest-path smoke test

A reusable, multi-distro smoke test for the **`lvlab up` / `down` /
`destroy`** lifecycle. It boots one **static** and one **DHCP** machine
for every cloud image lvlab ships by default, verifies SSH login as the
distro's default user, then gracefully shuts down and destroys each —
two VMs at a time, so it stays within a modest host's memory.

This complements the `createvm`/`deletevm` integration suite
(`tests/test_integration_createvm.py`): that one covers the standalone
scripts; this one covers the declarative manifest path and the shared
cloud-init network-config (MAC-matched NIC selection across distros).

> Manual only. Like `scripts/run-validation.sh`, this is never wired
> into CI — it creates real domains on `qemu:///system`.

## Prerequisites

- A libvirt host with the cloud images cached (run `lvlab init` from
    this directory once, or pre-seed
    `/var/lib/libvirt/images/lvlab/cloud-images/`).
- The `default` NAT network's DHCP range narrowed to free the static
    addresses `192.168.122.190-193` (e.g. range `.200-.254`). See
    `docs-extra/host-validation.md`.
- An SSH keypair at `~/.ssh/id_ed25519` (override with `SMOKE_SSH_KEY`);
    its public key is referenced by `Lvlab.yml`'s `cloud_init.pubkey`.

## Run

```bash
cd docs-extra/smoke
./run-smoke.sh            # uses lvlab from PATH, or ../../.venv/bin/lvlab
# or: LVLAB=/path/to/lvlab ./run-smoke.sh
```

It prints a `PASS`/`FAIL` line per machine and a final
`SMOKE RESULT:` summary (exit 0 = all passed).

## Keeping it current

`Lvlab.yml`'s `images:` section tracks lvlab's default image catalog and
is kept in lockstep by the `refresh-cloud-images` skill (alongside
`BUILTIN_IMAGES`, the repo `Lvlab.yml`, and `docs/Lvlab.example.yml`).
When a default distro is **added or removed**, also add/remove its
`*-static` + `*-dhcp` machine pair here and the matching row in
`run-smoke.sh`'s `PAIRS` table (pick a free static IP outside the DHCP
range).
