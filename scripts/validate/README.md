# `lvlab` CLI conformance harness

A declarative, async harness that drives the **installed** `lvlab` /
`createvm` / `deletevm` binaries (the built artifact) through a registry of
scenarios, schedules them across a cheap lane and a memory-budgeted stateful
lane, and emits a JSON + markdown report. It is a maintainer validation tool —
**not shipped in the wheel** (it lives under `scripts/`, like `hostcheck.sh`).

It complements, rather than replaces, the two existing layers:

- **pytest** (`tests/`) — pure unit + opt-in integration coverage of library code.
- **`lvlab smoke`** — the lvlab `up → verify → down → destroy` lifecycle.
- **this harness** — the `createvm` matrix (DHCP / static / dual-stack /
    NAT-flag) and the cross-binary CLI contracts (`--version`, help, the `#147`
    error panels, the `#149` no-manifest landing) that neither of the above covers.

## Run it

```bash
# Cheap lane only (no VMs) — safe anywhere:
uv run python -m validate --dry-run

# Full run (provisions real prefixed guests; prompts unless --yes):
uv run python -m validate --yes

# Subset / single scenario:
uv run python -m validate --only cvm-deb13-dhcp,status-no-manifest-landing
uv run python -m validate --lane cheap
```

`python -m validate` requires `scripts/` on `PYTHONPATH` (pytest gets this via
`pyproject.toml`; for an ad-hoc run use `PYTHONPATH=scripts` or `just validate`).
Reports land in `scripts/results/validate-<git-describe>-<epoch>.{json,issue.md}`.
The `.issue.md` is a ready-to-paste GitHub issue body with per-failure sub-issue
stubs — it is **never** auto-filed.

## Safety model (non-negotiable)

Identical in spirit to the integration suite's `LVLAB_TEST_PREFIX`:

- Every domain, disk, and ISO the harness creates carries a session-unique
    `lvlab-validate-<epoch_ms>-<rand>-` prefix (`safety.LVLAB_VALIDATE_PREFIX`).
- Teardown (`deletevm` per guest, plus a final reaper) **only ever touches
    prefixed resources** — it never lists or iterates all domains/disks.
    `safety.assert_owned()` guards every destructive call.
- The stateful lane prints the host's current domains and requires confirmation
    (or `--yes`); a non-TTY without `--yes` runs the cheap lane only.

This is what makes the harness safe to run on a host that also holds real VMs.

## What each lane proves (and what it doesn't)

| Scenario kind                              | Verified                                                               | Not verified            |
| ------------------------------------------ | ---------------------------------------------------------------------- | ----------------------- |
| Cheap (CLI contract)                       | exit code + stdout/stderr predicates                                   | —                       |
| `createvm` DHCP                            | provisions, reaches `running`, **DHCP lease resolves**                 | in-guest state          |
| `createvm` static / dual-stack / nat-flags | provisions, reaches `running`, **static IPv4 reachable via host ICMP** | in-guest address detail |

### Known gap → follow-up

The dual-stack scenario is meant to observe the issue **#148** extra DHCPv6
`/128`, which requires reading `ip addr` *inside* the guest. That needs an SSH
key createvm has injected; pass `--ssh-key <path>` to enable in-guest checks.
Without it, the harness records `in-guest checks skipped (no --ssh-key)` and the
#148 observation is not captured. Wiring reliable key injection for the one-off
`createvm` path (so `--ssh-key` works out of the box) is the natural next step.

## Architecture

`registry.py` is the catalog (data). `scenarios.py` holds the two scenario
types and their `execute` coroutines. `scheduler.py` runs the two lanes;
`pool.py` bounds concurrent guest memory (reusing `tkc_lvlab.smoke`'s
`available − reserve` math). `runner.py` is the only place that touches real
processes / libvirt. `report.py` projects results to JSON / text / issue
markdown. `safety.py` owns the prefix + reaper. Unit tests live in
`tests/test_validate_*.py`.
