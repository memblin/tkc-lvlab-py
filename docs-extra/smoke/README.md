# Manifest-path smoke test

A reusable, multi-distro smoke test for the **`lvlab up` / `down` /
`destroy`** lifecycle. It boots one **static** and one **DHCP** machine
for every cloud image lvlab ships by default, verifies SSH login as the
distro's default user, then gracefully shuts down and destroys each.

Run it with the built-in **`lvlab smoke`** subcommand. (The older
`run-smoke.sh` bash runner did the same lifecycle; `lvlab smoke`
replaces it, adds a fail-fast preflight, prints its batch plan, and can
emit structured `--format json`/`yaml`.)

This complements the `createvm`/`deletevm` integration suite
(`tests/test_integration_createvm.py`): that one covers the standalone
scripts; this one covers the declarative manifest path and the shared
cloud-init network-config (MAC-matched NIC selection across distros).

> Manual only. Like `scripts/run-validation.sh`, this is never wired
> into CI — it creates real domains on `qemu:///system`.

## Prerequisites

- A libvirt host with the cloud images cached. Run `lvlab init` from
    this directory once (or pre-seed
    `/var/lib/libvirt/images/lvlab/cloud-images/`).
- The `default` NAT network's DHCP range narrowed to free the static
    addresses `192.168.122.190-197` (e.g. range `.200-.254`). See
    `docs-extra/host-validation.md`.
- An SSH keypair at `~/.ssh/id_ed25519` (its public key is referenced by
    `Lvlab.yml`'s `cloud_init.pubkey`).

The preflight gate checks all three and fails fast with an actionable
message before any VM boots.

## Run

```bash
cd docs-extra/smoke
lvlab init        # one-time: download + verify the cloud images
lvlab smoke       # boot -> SSH-verify -> down -> destroy, every machine
```

`lvlab smoke` defaults to `./Lvlab.yml`; point it elsewhere with
`--config <path>`. It prints a `PASS`/`FAIL` line per machine and a
final `SMOKE RESULT:` summary (exit 0 = all passed, 1 = any failure).

Useful options:

- `--format json` / `--format yaml` — structured output for CI/agentic
    consumption (see below).
- `--batch-size N` — how many VMs to boot concurrently (default 2).
- `--skip-preflight` — bypass the preflight gate (debugging only).

### Structured output

`lvlab smoke --format json` emits one object per machine plus a
top-level summary:

```json
{
  "machines": [
    {
      "distro": "debian12",
      "vm_name": "deb12-static",
      "libvirt_domain": "deb12-static_smoke",
      "mode": "static",
      "resolved_ip": "192.168.122.190",
      "ssh_ok": true,
      "result": "pass",
      "boot_to_ssh_seconds": 24.6,
      "total_seconds": 41.2,
      "detail": "OK:deb12-static:debian"
    }
  ],
  "summary": {
    "total": 16,
    "passed": 16,
    "failed": 0,
    "overall": "pass",
    "host": "lab-host",
    "platform": "Linux-...",
    "git_sha": "cd3a8ae"
  }
}
```

`--format yaml` carries the identical shape:

```yaml
machines:
  - distro: debian12
    vm_name: deb12-static
    libvirt_domain: deb12-static_smoke
    mode: static
    resolved_ip: 192.168.122.190
    ssh_ok: true
    result: pass
    boot_to_ssh_seconds: 24.6
    total_seconds: 41.2
    detail: 'OK:deb12-static:debian'
summary:
  total: 16
  passed: 16
  failed: 0
  overall: pass
  host: lab-host
  platform: Linux-...
  git_sha: cd3a8ae
```

## Keeping it current

`Lvlab.yml`'s `images:` section tracks lvlab's default image catalog and
is kept in lockstep by the `refresh-cloud-images` skill (alongside
`BUILTIN_IMAGES`, the repo `Lvlab.yml`, and `docs/Lvlab.example.yml`).
When a default distro is **added or removed**, add/remove its
`*-static` + `*-dhcp` machine pair here (pick a free static IP outside
the DHCP range). The per-machine `memory:` values are hand-maintained to
match the documented per-distro floors (Debian 512 / Ubuntu 1024 /
AlmaLinux 1536 / Fedora 2048 MiB); keep them in sync.
