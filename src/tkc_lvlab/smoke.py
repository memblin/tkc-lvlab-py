"""Manifest-driven smoke runner for the ``lvlab smoke`` subcommand.

This module reimplements the bash ``docs-extra/smoke/run-smoke.sh`` runner
as Python so it can be invoked as ``lvlab smoke``. It is **manifest-driven**:
it operates on whatever machines a manifest declares (default ``./Lvlab.yml``,
override with ``--config``). The reference smoke manifest at
``docs-extra/smoke/Lvlab.yml`` (8 distros x {static, dhcp}) is just one such
manifest.

For each machine the runner drives the full lifecycle:

1. ``lvlab up <vm_name>`` (via the same ``Machine`` deploy path the CLI uses).
2. Resolve the guest IP — a static address from the manifest, otherwise poll
   libvirt for the running domain's DHCP lease (``virsh domifaddr``).
3. SSH-verify as the catalog default user (``id -un`` / ``hostname``).
4. ``lvlab down`` then ``lvlab destroy --force`` to tear the VM back down.

Two layers, deliberately separated so the logic is unit-testable without
booting a single VM:

- **Pure logic** — preflight checks (:func:`run_preflight`), host-resource
  detection + the bin-packing scheduler (:func:`detect_host_resources`,
  :func:`plan_batches`), and structured/text emission
  (:func:`render_results`). None of these touch ``virsh``, ``virt-install``,
  or the network. They are what the unit tests exercise.
- **VM lifecycle** — :func:`run_smoke`, :func:`_run_case`, and the helpers
  that shell out to ``lvlab``/``virsh``/``ssh``. This path boots **real**
  ``qemu:///system`` VMs and is therefore **manual only** — it must never run
  under ``uv run pytest``. The Typer command (:func:`tkc_lvlab.cli.smoke`)
  is the only entrypoint into it.

The scheduler (issue #90) detects host memory + vCPUs at startup and bin-packs
the machines into concurrent batches under a memory budget, holding back a
configurable reserve. Per-VM memory comes from the parsed manifest — the
authoritative source — plus a per-distro overhead allowance from
:mod:`tkc_lvlab.footprints`.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Sequence

import yaml
from rich.live import Live
from rich.table import Table

from ._logging import get_logger
from .config import parse_config
from .footprints import overhead_mib_for_os
from .utils.catalog import derive_username
from .utils.images import CloudImage
from .utils.libvirt import Machine
from .utils.network import LibvirtNetworkInfo, get_network_info
from .utils.output import get_console, is_tty, styled_table
from .utils.virsh import VirshError, run_virsh

logger = get_logger(__name__)


DEFAULT_CONFIG = "Lvlab.yml"
DEFAULT_LIBVIRT_URI = "qemu:///system"

# Memory held back from the host's available RAM for the host OS, the harness,
# and qemu slack. The scheduler packs batches under (available - reserve).
DEFAULT_RESERVE_MIB = 2048

# Prompt for confirmation before booting when the peak batch would use at least
# this fraction of available host memory (issue #130). `--yes` bypasses it.
MEMORY_CONFIRM_FRACTION = 0.5

# Conservative fallbacks if `free`/`nproc` cannot be read on an unusual host —
# enough to still produce a (small) one-or-two-at-a-time plan.
_FALLBACK_MEMORY_MIB = 2048

# SSH probe tuning. Matches run-smoke.sh: a short connect timeout, retried for
# roughly the time first-boot cloud-init needs to add the key.
SSH_CONNECT_TIMEOUT = 8
SSH_PROBE_RETRIES = 30
SSH_PROBE_INTERVAL = 5

# DHCP lease poll: how long to wait for a lease to appear after boot.
DHCP_POLL_RETRIES = 30
DHCP_POLL_INTERVAL = 5

# Graceful-shutdown poll after ``lvlab down``.
SHUTDOWN_POLL_RETRIES = 12
SHUTDOWN_POLL_INTERVAL = 5


class OutputFormat(str, Enum):
    """Output format for ``lvlab smoke``."""

    TEXT = "text"
    JSON = "json"
    YAML = "yaml"


class SmokeError(Exception):
    """A smoke-run setup failure (preflight, manifest, missing tooling).

    Distinct from a per-machine *case* failure, which is recorded as a
    :class:`CaseResult` with ``result="fail"`` rather than raised. A
    ``SmokeError`` aborts the whole run before (or instead of) booting VMs.
    """


# ---------------------------------------------------------------------------
# Data carriers (pure; the unit tests build these directly)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SmokeCase:
    """One machine to exercise, resolved from a manifest entry.

    Attributes:
        vm_name: Manifest short name (e.g. ``deb12-static``).
        libvirt_domain: Namespaced libvirt domain (``<vm_name>_<env>``).
        os: The machine's ``os`` key (e.g. ``debian12``).
        mode: ``"static"`` when the first interface declares an ``ip4``,
            else ``"dhcp"``.
        static_ip: Bare static IP (CIDR stripped) for ``static`` mode, else
            ``None``.
        mac: The pinned MAC of the first interface (informational only; DHCP
            lease resolution looks the address up by the running domain via
            ``virsh domifaddr``, not by this MAC — see issue #125).
        ssh_user: The catalog default first-boot username to SSH in as.
        memory_mib: Per-VM memory from the manifest (drives the scheduler).
        vcpus: Per-VM vCPU count from the manifest.
    """

    vm_name: str
    libvirt_domain: str
    os: str
    mode: str
    static_ip: str | None
    mac: str | None
    ssh_user: str
    memory_mib: int
    vcpus: int


@dataclass
class CaseResult:
    """Outcome of exercising one :class:`SmokeCase`.

    Attributes:
        distro: The machine's ``os`` key.
        vm_name: Manifest short name.
        libvirt_domain: Namespaced libvirt domain.
        mode: ``"static"`` or ``"dhcp"``.
        resolved_ip: The IP the runner verified against, or ``None`` if it
            never resolved one.
        ssh_ok: ``True`` when the SSH probe succeeded.
        result: ``"pass"`` or ``"fail"``.
        boot_to_ssh_seconds: Wall-clock from ``up`` to the first successful
            SSH probe, or ``None`` if SSH never succeeded.
        total_seconds: Wall-clock for the whole case (up -> verify -> down ->
            destroy).
        detail: A short human note (failure reason, or the SSH banner).
    """

    distro: str
    vm_name: str
    libvirt_domain: str
    mode: str
    resolved_ip: str | None = None
    ssh_ok: bool = False
    result: str = "fail"
    boot_to_ssh_seconds: float | None = None
    total_seconds: float | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict for JSON/YAML emission."""
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class HostResources:
    """Detected host capacity used by the scheduler.

    Attributes:
        total_memory_mib: Total host RAM in MiB.
        available_memory_mib: Currently-available host RAM in MiB (``free``'s
            ``available`` column when present).
        vcpus: Host logical CPU count (``nproc``).
    """

    total_memory_mib: int
    available_memory_mib: int
    vcpus: int


@dataclass(frozen=True)
class Batch:
    """One concurrently-run group of cases plus its memory cost.

    Attributes:
        cases: The cases that run together in this batch.
        memory_mib: Sum of per-case (guest memory + overhead) for the batch.
    """

    cases: tuple[SmokeCase, ...]
    memory_mib: int = 0


@dataclass(frozen=True)
class SmokePlan:
    """The computed concurrency plan, printed before any VM boots.

    Attributes:
        batches: The ordered batches to run.
        resources: The detected host resources the plan was sized against.
        budget_mib: The memory budget batches were packed under.
        reserve_mib: The safety reserve held back from available memory.
        batch_size_override: The explicit ``--batch-size`` if one was given,
            else ``None`` (memory-driven packing was used).
    """

    batches: tuple[Batch, ...]
    resources: HostResources
    budget_mib: int
    reserve_mib: int
    batch_size_override: int | None = None


# ---------------------------------------------------------------------------
# Case construction from a parsed manifest (pure)
# ---------------------------------------------------------------------------


def build_cases(
    environment: dict[str, Any],
    images: dict[str, Any],
    config_defaults: dict[str, Any],
    machines: Sequence[dict[str, Any]],
) -> list[SmokeCase]:
    """Resolve every manifest machine into a :class:`SmokeCase`.

    Constructs a :class:`Machine` per entry (which applies ``config_defaults``
    and pins a per-interface MAC), then reads the resolved fields the runner
    needs. Pure: builds no VMs, touches no ``virsh``.

    Args:
        environment: ``environment[0]`` from the manifest.
        images: The manifest ``images`` map (used to resolve the default
            username override per image key).
        config_defaults: The manifest ``config_defaults`` block.
        machines: The manifest ``machines`` list.

    Returns:
        One :class:`SmokeCase` per machine, in manifest order.
    """
    cases: list[SmokeCase] = []
    for machine_config in machines:
        machine = Machine(machine_config, environment, config_defaults)
        first_iface = machine.interfaces[0] if machine.interfaces else {}
        ip4 = first_iface.get("ip4")
        mode = "static" if ip4 else "dhcp"
        static_ip = ip4.split("/")[0] if ip4 else None
        image_cfg = images.get(machine.os, {}) or {}
        ssh_user = machine.cloud_init_config.get("user") or derive_username(
            machine.os, image_cfg.get("username")
        )
        cases.append(
            SmokeCase(
                vm_name=machine.vm_name,
                libvirt_domain=machine.libvirt_vm_name,
                os=machine.os,
                mode=mode,
                static_ip=static_ip,
                mac=first_iface.get("macaddress"),
                ssh_user=ssh_user,
                memory_mib=int(machine.memory),
                vcpus=int(machine.cpu),
            )
        )
    return cases


# ---------------------------------------------------------------------------
# Preflight (pure logic + thin probes; unit-tested with mocked state)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightCheck:
    """One preflight check outcome.

    Attributes:
        name: Short check identifier.
        ok: Whether the check passed.
        message: An actionable description (what's wrong + how to fix).
    """

    name: str
    ok: bool
    message: str


def check_images_cached(
    images: dict[str, Any],
    cases: Sequence[SmokeCase],
    environment: dict[str, Any],
    config_defaults: dict[str, Any],
    *,
    exists=None,
) -> PreflightCheck:
    """Verify every image a case needs is present in the cloud-image cache.

    Args:
        images: The manifest ``images`` map.
        cases: The resolved cases (their ``os`` keys name the images used).
        environment: ``environment[0]`` (passed to :class:`CloudImage`).
        config_defaults: ``config_defaults`` (drives the cache dir).
        exists: Test seam — predicate over an image filepath. Defaults to
            :func:`os.path.isfile`.

    Returns:
        A passing :class:`PreflightCheck` when all needed images are cached;
        otherwise a failing one naming the missing image keys and suggesting
        ``lvlab init``.
    """
    isfile = exists if exists is not None else os.path.isfile
    needed = sorted({case.os for case in cases})
    missing: list[str] = []
    for key in needed:
        cfg = images.get(key)
        if cfg is None:
            missing.append(f"{key} (no images entry)")
            continue
        image = CloudImage(key, cfg, environment, config_defaults)
        if not isfile(image.image_fpath):
            missing.append(key)
    if missing:
        return PreflightCheck(
            name="images-cached",
            ok=False,
            message=(
                "Cloud images not cached for: "
                + ", ".join(missing)
                + ". Run `lvlab init` (from the manifest directory) to "
                "download and verify them first."
            ),
        )
    return PreflightCheck(
        name="images-cached",
        ok=True,
        message=f"All {len(needed)} required cloud image(s) are cached.",
    )


def check_static_ips_free(
    cases: Sequence[SmokeCase],
    network_info: LibvirtNetworkInfo | None,
) -> PreflightCheck:
    """Verify the manifest's static IPs sit outside the network's DHCP range.

    A static address inside the libvirt ``default`` DHCP pool races the DHCP
    server on every boot, so the runner refuses before booting.

    Args:
        cases: The resolved cases (static ones carry ``static_ip``).
        network_info: The resolved network metadata, or ``None`` when the
            network could not be inspected (the check then passes with a
            warning rather than blocking).

    Returns:
        A :class:`PreflightCheck`. Failing when any static IP falls in
        ``[dhcp_start, dhcp_end]``; the message lists the offending IPs.
    """
    static_ips = [c.static_ip for c in cases if c.mode == "static" and c.static_ip]
    if not static_ips:
        return PreflightCheck(
            name="static-ips-free",
            ok=True,
            message="No static-IP machines in manifest; nothing to check.",
        )
    if network_info is None or not (network_info.dhcp_start and network_info.dhcp_end):
        return PreflightCheck(
            name="static-ips-free",
            ok=True,
            message=(
                "Could not read the network's DHCP range; skipping the "
                "static-IP collision check."
            ),
        )

    import ipaddress

    dhcp_start = ipaddress.ip_address(network_info.dhcp_start)
    dhcp_end = ipaddress.ip_address(network_info.dhcp_end)
    clashes = [
        ip for ip in static_ips if dhcp_start <= ipaddress.ip_address(ip) <= dhcp_end
    ]
    if clashes:
        return PreflightCheck(
            name="static-ips-free",
            ok=False,
            message=(
                "Static IP(s) fall inside the DHCP range "
                f"[{dhcp_start}-{dhcp_end}] of network "
                f"'{network_info.name}': {', '.join(clashes)}. Narrow the "
                "network's DHCP range (see docs-extra/host-validation.md) so "
                "these addresses are free."
            ),
        )
    return PreflightCheck(
        name="static-ips-free",
        ok=True,
        message=(
            f"All {len(static_ips)} static IP(s) are outside the DHCP range "
            f"[{dhcp_start}-{dhcp_end}]."
        ),
    )


def check_ssh_key_present(
    config_defaults: dict[str, Any],
    *,
    exists=None,
) -> PreflightCheck:
    """Verify the SSH public key the manifest references exists on disk.

    Reads ``config_defaults.cloud_init.pubkey``. When it looks like a path
    (contains ``~`` or ``/``), the file must exist; a literal key string is
    accepted as-is. A missing ``pubkey`` is reported as a failure since the
    runner cannot SSH in without one.

    Args:
        config_defaults: The manifest ``config_defaults`` block.
        exists: Test seam — predicate over a path. Defaults to
            :func:`os.path.isfile`.

    Returns:
        A :class:`PreflightCheck`.
    """
    isfile = exists if exists is not None else os.path.isfile
    pubkey = (config_defaults.get("cloud_init", {}) or {}).get("pubkey")
    if not pubkey:
        return PreflightCheck(
            name="ssh-key-present",
            ok=False,
            message=(
                "No cloud_init.pubkey in config_defaults; the runner needs an "
                "SSH public key to verify guest login."
            ),
        )
    if "~" in pubkey or "/" in pubkey:
        path = os.path.expanduser(pubkey)
        if not isfile(path):
            return PreflightCheck(
                name="ssh-key-present",
                ok=False,
                message=(
                    f"SSH public key '{pubkey}' (resolved to '{path}') not "
                    "found. Generate one (ssh-keygen) or fix cloud_init.pubkey."
                ),
            )
        return PreflightCheck(
            name="ssh-key-present", ok=True, message=f"SSH public key present: {path}"
        )
    return PreflightCheck(
        name="ssh-key-present",
        ok=True,
        message="cloud_init.pubkey is a literal key string.",
    )


def run_preflight(
    images: dict[str, Any],
    cases: Sequence[SmokeCase],
    environment: dict[str, Any],
    config_defaults: dict[str, Any],
    network_info: LibvirtNetworkInfo | None,
) -> list[PreflightCheck]:
    """Run all preflight checks and return their outcomes.

    Args:
        images: The manifest ``images`` map.
        cases: The resolved cases.
        environment: ``environment[0]``.
        config_defaults: ``config_defaults``.
        network_info: Resolved network metadata, or ``None`` if unavailable.

    Returns:
        The list of :class:`PreflightCheck` outcomes (order: images, static
        IPs, SSH key).
    """
    return [
        check_images_cached(images, cases, environment, config_defaults),
        check_static_ips_free(cases, network_info),
        check_ssh_key_present(config_defaults),
    ]


# ---------------------------------------------------------------------------
# Resource detection + bin-packing scheduler (pure; issue #90)
# ---------------------------------------------------------------------------


def detect_host_resources() -> HostResources:
    """Detect host memory + vCPU capacity via ``free`` and ``nproc``.

    Falls back to conservative values when either tool is unavailable so the
    scheduler can still produce a (small) plan on an unusual host.

    Returns:
        A :class:`HostResources` snapshot.
    """
    total_mib = _FALLBACK_MEMORY_MIB
    available_mib = _FALLBACK_MEMORY_MIB
    try:
        out = subprocess.run(
            ["free", "-m"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        ).stdout
        total_mib, available_mib = _parse_free_m(out)
    except (OSError, ValueError):
        pass

    vcpus = os.cpu_count() or 1
    try:
        out = subprocess.run(
            ["nproc"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        ).stdout.strip()
        if out:
            vcpus = int(out)
    except (OSError, ValueError):
        pass

    return HostResources(
        total_memory_mib=total_mib,
        available_memory_mib=available_mib,
        vcpus=vcpus,
    )


def _parse_free_m(free_output: str) -> tuple[int, int]:
    """Parse ``free -m`` output into ``(total_mib, available_mib)``.

    Reads the ``Mem:`` line. ``available`` is the 7th column on modern
    ``procps`` (``total used free shared buff/cache available``); when that
    column is absent (very old ``free``), ``free`` (4th column) is used.

    Args:
        free_output: stdout of ``free -m`` (locale forced to ``C``).

    Returns:
        ``(total_mib, available_mib)``.

    Raises:
        ValueError: No parseable ``Mem:`` line was found.
    """
    for line in free_output.splitlines():
        if line.lower().startswith("mem:"):
            parts = line.split()
            total = int(parts[1])
            available = int(parts[6]) if len(parts) >= 7 else int(parts[3])
            return total, available
    raise ValueError("no 'Mem:' line in free output")


def case_cost_mib(case: SmokeCase) -> int:
    """Return the budgeted memory for one case (guest RAM + qemu overhead)."""
    return case.memory_mib + overhead_mib_for_os(case.os)


def plan_batches(
    cases: Sequence[SmokeCase],
    resources: HostResources,
    *,
    batch_size: int | None = None,
    max_memory_mib: int | None = None,
    reserve_mib: int = DEFAULT_RESERVE_MIB,
) -> SmokePlan:
    """Bin-pack cases into concurrent batches under a memory budget.

    The budget is ``min(available_memory, max_memory) - reserve``. Cases are
    packed first-fit-decreasing by per-case cost (guest RAM + per-distro
    overhead from :mod:`tkc_lvlab.footprints`), which naturally pairs a heavy
    guest with light ones in the same batch rather than clustering all the
    heavy guests — minimizing wall-clock.

    An explicit ``batch_size`` overrides the memory packing entirely (for CI
    pinning / debugging / tiny boxes): cases are chunked into fixed-size groups
    in manifest order.

    A single case heavier than the whole budget still gets its own batch (the
    runner must attempt every machine); that batch reports over budget.

    Args:
        cases: The resolved cases to schedule.
        resources: Detected host resources.
        batch_size: Explicit concurrent-count override, or ``None`` to pack by
            memory.
        max_memory_mib: Cap the budget at this many MiB, or ``None`` for no cap
            beyond available memory.
        reserve_mib: Memory held back for the host + harness + qemu slack.

    Returns:
        A :class:`SmokePlan` with the computed batches and the budget used.

    Raises:
        ValueError: ``batch_size`` is given and is < 1.
    """
    budget = resources.available_memory_mib - reserve_mib
    if max_memory_mib is not None:
        budget = min(budget, max_memory_mib - reserve_mib)
    budget = max(budget, 0)

    if batch_size is not None:
        if batch_size < 1:
            raise ValueError("--batch-size must be >= 1")
        batches = _chunk_fixed(cases, batch_size)
        return SmokePlan(
            batches=tuple(batches),
            resources=resources,
            budget_mib=budget,
            reserve_mib=reserve_mib,
            batch_size_override=batch_size,
        )

    ordered = sorted(cases, key=case_cost_mib, reverse=True)
    bins: list[list[SmokeCase]] = []
    bin_costs: list[int] = []
    for case in ordered:
        cost = case_cost_mib(case)
        placed = False
        for idx, used in enumerate(bin_costs):
            if used + cost <= budget:
                bins[idx].append(case)
                bin_costs[idx] = used + cost
                placed = True
                break
        if not placed:
            # New bin. A case larger than the whole budget still gets its own
            # bin so the runner attempts it (it'll report over budget).
            bins.append([case])
            bin_costs.append(cost)

    batches = tuple(
        Batch(cases=tuple(group), memory_mib=cost)
        for group, cost in zip(bins, bin_costs)
    )
    return SmokePlan(
        batches=batches,
        resources=resources,
        budget_mib=budget,
        reserve_mib=reserve_mib,
        batch_size_override=None,
    )


def _chunk_fixed(cases: Sequence[SmokeCase], size: int) -> list[Batch]:
    """Chunk cases into fixed-size batches in manifest order."""
    out: list[Batch] = []
    for i in range(0, len(cases), size):
        group = tuple(cases[i : i + size])
        out.append(Batch(cases=group, memory_mib=sum(case_cost_mib(c) for c in group)))
    return out


def format_plan(plan: SmokePlan) -> str:
    """Render the computed plan as a human-readable block.

    Args:
        plan: The plan to describe.

    Returns:
        A multi-line string: detected resources, the budget, and each batch's
        members + memory cost.
    """
    res = plan.resources
    lines = [
        f"Host: {res.vcpus} vCPU, {res.available_memory_mib} MiB available "
        f"(of {res.total_memory_mib} MiB total)"
    ]
    if plan.batch_size_override is not None:
        lines.append(
            f"Concurrency: fixed --batch-size={plan.batch_size_override} "
            f"(memory budget {plan.budget_mib} MiB, reserve {plan.reserve_mib} MiB)"
        )
    else:
        lines.append(
            f"Concurrency: memory-packed under {plan.budget_mib} MiB "
            f"(available - {plan.reserve_mib} MiB reserve)"
        )
    for i, batch in enumerate(plan.batches, start=1):
        members = ", ".join(c.vm_name for c in batch.cases)
        over = " [OVER BUDGET]" if batch.memory_mib > plan.budget_mib else ""
        lines.append(f"  Batch {i} ({batch.memory_mib} MiB){over}: {members}")
    total = sum(len(b.cases) for b in plan.batches)
    lines.append(f"Total: {total} VM(s) in {len(plan.batches)} batch(es)")
    return "\n".join(lines)


def _plan_concurrency_label(plan: SmokePlan) -> str:
    """Short label for how concurrency was chosen (for the plan title)."""
    if plan.batch_size_override is not None:
        return f"fixed --batch-size={plan.batch_size_override}"
    return f"pack <= {plan.budget_mib} MiB"


def render_plan_table(plan: SmokePlan) -> Table:
    """Render the concurrency plan as a Rich table (TEXT on a terminal).

    The same information as :func:`format_plan` (which stays the plain-text
    form for non-terminal / structured output), in the same box style as the
    live phase table so the plan and the run read as one UI (issue #126). Host
    capacity and the packing budget go in the title; one row per batch carries
    its memory cost and members.

    Args:
        plan: The computed plan to render.

    Returns:
        A Rich :class:`~rich.table.Table` ready to print to the console.
    """
    res = plan.resources
    total = sum(len(b.cases) for b in plan.batches)
    table = styled_table(
        title=(
            f"smoke plan · {total} VM(s) · {len(plan.batches)} batch(es) · "
            f"{res.vcpus} vCPU / {res.available_memory_mib} MiB avail · "
            f"{_plan_concurrency_label(plan)}"
        )
    )
    table.add_column("batch", justify="right")
    table.add_column("memory", justify="right")
    table.add_column("cases")
    for i, batch in enumerate(plan.batches, start=1):
        over = " [OVER]" if batch.memory_mib > plan.budget_mib else ""
        members = ", ".join(c.vm_name for c in batch.cases)
        table.add_row(str(i), f"{batch.memory_mib} MiB{over}", members)
    return table


def should_confirm_memory(
    plan: SmokePlan, fraction: float = MEMORY_CONFIRM_FRACTION
) -> bool:
    """Whether a run is memory-heavy enough to warrant a confirmation prompt.

    True when the peak batch's memory cost is at least ``fraction`` of the
    host's available memory (issue #130). Small runs (a VM or two) fall below
    the threshold and boot without prompting.

    Args:
        plan: The computed plan (carries per-batch memory + host resources).
        fraction: Threshold as a fraction of available memory (default
            :data:`MEMORY_CONFIRM_FRACTION`).

    Returns:
        ``True`` when a prompt is warranted, else ``False``.
    """
    available = plan.resources.available_memory_mib
    if not plan.batches or available <= 0:
        return False
    peak = max(b.memory_mib for b in plan.batches)
    return peak >= fraction * available


def memory_confirm_message(plan: SmokePlan) -> str:
    """Build the one-line memory-cost notice shown before the confirm prompt."""
    peak = max((b.memory_mib for b in plan.batches), default=0)
    res = plan.resources
    return (
        f"This run will use up to {peak} MiB of {res.available_memory_mib} MiB "
        f"available (host total {res.total_memory_mib} MiB)."
    )


# ---------------------------------------------------------------------------
# Result emission (pure)
# ---------------------------------------------------------------------------


def summarize(results: Sequence[CaseResult]) -> dict[str, Any]:
    """Build the top-level summary dict for structured output.

    Args:
        results: The per-case outcomes.

    Returns:
        A dict with counts, overall pass/fail, host/env identity, and the git
        SHA of the checkout.
    """
    passed = sum(1 for r in results if r.result == "pass")
    failed = len(results) - passed
    return {
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "overall": "pass" if failed == 0 else "fail",
        "host": platform.node(),
        "platform": platform.platform(),
        "git_sha": _git_sha(),
    }


def render_results(results: Sequence[CaseResult], fmt: OutputFormat) -> str:
    """Render case results in the requested format.

    Args:
        results: The per-case outcomes.
        fmt: ``text``, ``json``, or ``yaml``.

    Returns:
        The rendered string.
    """
    summary = summarize(results)
    if fmt is OutputFormat.JSON:
        return json.dumps(
            {"machines": [r.to_dict() for r in results], "summary": summary},
            indent=2,
        )
    if fmt is OutputFormat.YAML:
        return yaml.safe_dump(
            {"machines": [r.to_dict() for r in results], "summary": summary},
            sort_keys=False,
        )
    return _render_text(results, summary)


def _render_text(results: Sequence[CaseResult], summary: dict[str, Any]) -> str:
    """Render the concise PASS/FAIL-per-machine text report + summary.

    The full report (the ``===`` table plus the closing summary). Used for the
    non-TTY / piped TEXT path, where no live table was drawn. On a terminal the
    live phase table already shows the per-VM grid, so only the closing summary
    (:func:`_render_text_summary`) is printed under it (issue #126).
    """
    lines: list[str] = []
    lines.append("=" * 66)
    lines.append(f"  {'vm':<20} {'mode':<7} {'ip':<16} result")
    lines.append("=" * 66)
    for r in results:
        ip = r.resolved_ip or "<none>"
        verdict = "PASS" if r.result == "pass" else "FAIL"
        lines.append(f"  {r.vm_name:<20} {r.mode:<7} {ip:<16} {verdict}")
    lines.append("=" * 66)
    lines.append(_render_text_summary(results, summary))
    return "\n".join(lines)


def _render_text_summary(results: Sequence[CaseResult], summary: dict[str, Any]) -> str:
    """Render the closing lines: per-failure details + the SMOKE RESULT verdict.

    This is everything in the TEXT report *except* the per-VM ``===`` table,
    so it can be printed under the live phase table without duplicating the
    grid the live table already showed (issue #126).

    Args:
        results: The per-case outcomes.
        summary: The :func:`summarize` dict (for the total/failed counts).

    Returns:
        The failure-detail lines (if any) followed by the one-line verdict.
    """
    lines: list[str] = []
    for r in results:
        if r.result != "pass" and r.detail:
            lines.append(f"  {r.vm_name}: {r.detail}")
    if summary["failed"] == 0:
        lines.append(f"SMOKE RESULT: ALL {summary['total']} CASES PASSED")
    else:
        lines.append(f"SMOKE RESULT: {summary['failed']} of {summary['total']} FAILED")
    return "\n".join(lines)


def _git_sha() -> str:
    """Return the current git short SHA, or ``"unknown"`` if not resolvable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            cwd=os.getcwd(),
        )
        sha = out.stdout.strip()
        return sha or "unknown"
    except OSError:
        return "unknown"


# ===========================================================================
# VM LIFECYCLE — MANUAL ONLY. Never invoked under pytest.
# ===========================================================================
#
# Everything below boots real qemu:///system VMs by shelling out to the
# ``lvlab`` console script, ``virsh``, and ``ssh``. The unit suite exercises
# only the pure logic above; nothing in pytest calls run_smoke / _run_case.
# ===========================================================================


def _lvlab_bin() -> str:  # pragma: no cover - VM lifecycle
    """Resolve the ``lvlab`` executable to drive lifecycle subcommands.

    Prefers ``$LVLAB``, then ``lvlab`` on PATH, then a bare ``lvlab``.
    """
    explicit = os.environ.get("LVLAB")
    if explicit:
        return explicit
    return shutil.which("lvlab") or "lvlab"


def _ssh_private_key(config_defaults: dict[str, Any]) -> str | None:
    """Derive the private-key path from ``config_defaults.cloud_init.pubkey``.

    Args:
        config_defaults: The manifest ``config_defaults`` block.

    Returns:
        The private-key path (``.pub`` suffix stripped) when ``pubkey`` looks
        like a path, else ``None`` (a literal key has no on-disk private half
        to point ``ssh -i`` at).
    """
    pubkey = (config_defaults.get("cloud_init", {}) or {}).get("pubkey")
    if not pubkey or ("~" not in pubkey and "/" not in pubkey):
        return None
    path = os.path.expanduser(pubkey)
    return path[:-4] if path.endswith(".pub") else path


_DOMIFADDR_IPV4 = re.compile(r"\bipv4\s+(?P<ip>\d{1,3}(?:\.\d{1,3}){3})/\d+")


def _parse_domifaddr_lease(output: str) -> str | None:
    """Extract the first IPv4 address from ``virsh domifaddr --source lease`` output.

    ``virsh domifaddr <domain> --source lease`` prints one row per interface
    address::

         Name       MAC address          Protocol     Address
        -------------------------------------------------------------------
         vnet3      52:54:00:1a:2b:3c    ipv4         192.168.122.123/24

    Args:
        output: Raw stdout from the ``virsh domifaddr`` call.

    Returns:
        The dotted-quad IPv4 address of the first ``ipv4`` row, or ``None``
        when the output carries no IPv4 lease line (header-only, empty, or
        IPv6-only).
    """
    for line in output.splitlines():
        match = _DOMIFADDR_IPV4.search(line)
        if match:
            return match.group("ip")
    return None


def _resolve_dhcp_ip(
    case: SmokeCase, uri: str
) -> str | None:  # pragma: no cover - VM lifecycle
    """Poll libvirt for the running domain's DHCP-leased IPv4 address.

    Uses ``virsh domifaddr <domain> --source lease``, which resolves the
    address by the *running domain's* own interface. That is correct
    regardless of the per-interface MAC the launching ``lvlab up`` generated:
    ``generate_mac`` is random per ``Machine`` construction, so matching
    ``net-dhcp-leases`` against a MAC this process pinned independently never
    matched the VM's actual MAC and failed every DHCP case (issue #125).
    """
    for _ in range(DHCP_POLL_RETRIES):
        try:
            result = run_virsh(
                uri,
                ["domifaddr", case.libvirt_domain, "--source", "lease"],
                check=False,
            )
        except VirshError:
            result = None
        if result is not None and result.returncode == 0:
            ip = _parse_domifaddr_lease(result.stdout)
            if ip:
                return ip
        time.sleep(DHCP_POLL_INTERVAL)
    return None


def _ssh_probe(
    user: str, ip: str, key_path: str | None
) -> tuple[bool, str]:  # pragma: no cover - VM lifecycle
    """Retry an SSH login, running ``id -un``/``hostname`` once it connects."""
    opts = [
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
    ]
    if key_path:
        opts = ["-i", key_path, *opts]
    cmd = "echo OK:$(hostname):$(id -un)"
    last = ""
    for _ in range(SSH_PROBE_RETRIES):
        proc = subprocess.run(
            ["ssh", *opts, f"{user}@{ip}", cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        out = proc.stdout.strip()
        if out.startswith("OK:"):
            return True, out
        stderr_lines = (proc.stderr or "").strip().splitlines()
        last = stderr_lines[-1] if stderr_lines else ""
        time.sleep(SSH_PROBE_INTERVAL)
    return False, (
        f"no SSH after ~{SSH_PROBE_RETRIES * SSH_PROBE_INTERVAL}s; last: {last}"
    )


# ---------------------------------------------------------------------------
# Live status table (issue #101)
# ---------------------------------------------------------------------------


class SmokePhase:
    """The per-case phases shown in the live smoke table (issue #101)."""

    PENDING = "pending"
    UP = "up"
    BOOTING = "booting"
    VERIFYING = "verifying"
    TEARDOWN = "tearing down"
    PASS = "PASS"
    FAIL = "FAIL"

    #: Phases that mean the case is finished (for the running/done footer tally).
    TERMINAL = frozenset({PASS, FAIL})


@dataclass
class _CaseProgress:
    """Mutable per-case row state for the live table."""

    vm_name: str
    mode: str
    ip: str | None = None
    phase: str = SmokePhase.PENDING


class SmokeProgress:
    """Thread-safe per-case phase tracker driving the live smoke table.

    Cases run in concurrent batches, so worker threads advance phases
    through the locked setters while the main thread reads consistent
    snapshots to render (issue #101) — every Rich call stays on the main
    thread.
    """

    def __init__(self, cases: Sequence[SmokeCase]) -> None:
        self._lock = threading.Lock()
        self._order = [c.vm_name for c in cases]
        self._states = {
            c.vm_name: _CaseProgress(c.vm_name, c.mode, c.static_ip) for c in cases
        }

    def set_phase(self, vm_name: str, phase: str) -> None:
        """Advance a case to ``phase``."""
        with self._lock:
            self._states[vm_name].phase = phase

    def set_ip(self, vm_name: str, ip: str | None) -> None:
        """Record a case's resolved IP (a DHCP lease, once it appears)."""
        with self._lock:
            self._states[vm_name].ip = ip

    def snapshot(self) -> list["_CaseProgress"]:
        """Return a consistent copy of every case's state, in declared order."""
        with self._lock:
            return [dataclasses.replace(self._states[n]) for n in self._order]


def render_smoke_table(states: list["_CaseProgress"], *, pool_size: int):
    """Build the live smoke status table from a state snapshot (issue #101).

    Args:
        states: Per-case snapshot from :meth:`SmokeProgress.snapshot`.
        pool_size: The concurrent batch width, shown in the title.

    Returns:
        A populated :class:`rich.table.Table` with a running/pending/
        passed/failed caption.
    """
    table = styled_table(
        title=f"lvlab smoke — {len(states)} cases, pool of {pool_size}"
    )
    table.add_column("vm", style="bold")
    table.add_column("mode")
    table.add_column("ip")
    table.add_column("phase")

    passed = failed = pending = running = 0
    for state in states:
        table.add_row(
            state.vm_name,
            state.mode,
            state.ip or ("—" if state.mode == "dhcp" else ""),
            state.phase,
        )
        if state.phase == SmokePhase.PASS:
            passed += 1
        elif state.phase == SmokePhase.FAIL:
            failed += 1
        elif state.phase == SmokePhase.PENDING:
            pending += 1
        else:
            running += 1

    table.caption = (
        f"running {running} · pending {pending} · passed {passed} · failed {failed}"
    )
    return table


def _teardown(
    case: SmokeCase, *, lvlab: str, uri: str
) -> None:  # pragma: no cover - VM lifecycle
    """Shut down + destroy a case's VM regardless of verify outcome."""
    from .utils.virsh import virsh_domstate

    subprocess.run(
        [lvlab, "down", case.vm_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    for _ in range(SHUTDOWN_POLL_RETRIES):
        try:
            if virsh_domstate(uri, case.libvirt_domain) == "shut off":
                break
        except VirshError:
            break
        time.sleep(SHUTDOWN_POLL_INTERVAL)
    subprocess.run(
        [lvlab, "destroy", case.vm_name, "--force"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _run_case(
    case: SmokeCase,
    *,
    lvlab: str,
    uri: str,
    key_path: str | None,
    progress: "SmokeProgress | None" = None,
) -> CaseResult:  # pragma: no cover - VM lifecycle
    """Drive the full up -> verify -> down -> destroy lifecycle for one case.

    When ``progress`` is supplied, advances the case's phase through it at
    each step so the live table reflects the run as it proceeds (issue
    #101).
    """

    def phase(name: str) -> None:
        if progress is not None:
            progress.set_phase(case.vm_name, name)

    result = CaseResult(
        distro=case.os,
        vm_name=case.vm_name,
        libvirt_domain=case.libvirt_domain,
        mode=case.mode,
    )
    start = time.monotonic()

    phase(SmokePhase.UP)
    up = subprocess.run(
        [lvlab, "up", case.vm_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if up.returncode != 0:
        result.detail = f"`lvlab up` failed (rc={up.returncode})"
        result.total_seconds = round(time.monotonic() - start, 1)
        phase(SmokePhase.TEARDOWN)
        _teardown(case, lvlab=lvlab, uri=uri)
        phase(SmokePhase.FAIL)
        return result

    phase(SmokePhase.BOOTING)
    ip = case.static_ip if case.mode == "static" else _resolve_dhcp_ip(case, uri)
    result.resolved_ip = ip
    if progress is not None and ip:
        progress.set_ip(case.vm_name, ip)

    if not ip:
        result.detail = "no IP resolved (DHCP lease never appeared)"
    else:
        phase(SmokePhase.VERIFYING)
        ok, detail = _ssh_probe(case.ssh_user, ip, key_path)
        result.ssh_ok = ok
        result.detail = detail
        if ok:
            result.boot_to_ssh_seconds = round(time.monotonic() - start, 1)
            result.result = "pass"

    phase(SmokePhase.TEARDOWN)
    _teardown(case, lvlab=lvlab, uri=uri)
    result.total_seconds = round(time.monotonic() - start, 1)
    phase(SmokePhase.PASS if result.result == "pass" else SmokePhase.FAIL)
    return result


def _run_batches(
    plan: "SmokePlan",
    progress: "SmokeProgress",
    *,
    lvlab: str,
    uri: str,
    key_path: str | None,
    live: bool,
) -> list[CaseResult]:  # pragma: no cover - VM lifecycle
    """Run every batch's cases concurrently, optionally rendering a live table.

    When ``live`` is true (TEXT format on a terminal), a Rich ``Live`` table
    is refreshed from the main thread off the shared (locked)
    :class:`SmokeProgress` while the batch's workers advance their phases
    (issue #101). Otherwise the batches run exactly as before with no live
    rendering. Batches run sequentially; cases within a batch run in a
    thread pool.

    Returns:
        The collected :class:`CaseResult` list (unordered; the caller sorts).
    """
    pool_size = max((len(b.cases) for b in plan.batches), default=1)
    results: list[CaseResult] = []
    live_view = Live(console=get_console(), refresh_per_second=8) if live else None
    if live_view is not None:
        live_view.start()
    try:
        for batch in plan.batches:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(batch.cases)
            ) as pool:
                futures = [
                    pool.submit(
                        _run_case,
                        case,
                        lvlab=lvlab,
                        uri=uri,
                        key_path=key_path,
                        progress=progress,
                    )
                    for case in batch.cases
                ]
                pending = set(futures)
                while pending:
                    _done, pending = concurrent.futures.wait(pending, timeout=0.25)
                    if live_view is not None:
                        live_view.update(
                            render_smoke_table(progress.snapshot(), pool_size=pool_size)
                        )
                results.extend(f.result() for f in futures)
        if live_view is not None:
            live_view.update(
                render_smoke_table(progress.snapshot(), pool_size=pool_size)
            )
    finally:
        if live_view is not None:
            live_view.stop()
    return results


def smoke_env_dir(config_defaults: dict[str, Any], environment: dict[str, Any]) -> str:
    """Return the environment's storage directory the smoke run uses.

    Mirrors :class:`~tkc_lvlab.utils.libvirt.Machine`'s layout: per-VM
    artifacts live under ``<disk_image_basedir>/<env>/<vm>/``, so the
    environment directory is ``<disk_image_basedir>/<env>/``.

    Args:
        config_defaults: The manifest's ``config_defaults`` (supplies
            ``disk_image_basedir``).
        environment: The manifest's ``environment[0]`` (supplies ``name``).

    Returns:
        The absolute environment-directory path (``~`` expanded).
    """
    basedir = os.path.expanduser(
        config_defaults.get("disk_image_basedir", "/var/lib/libvirt/images/lvlab")
    )
    return os.path.join(basedir, environment.get("name", "LvLabEnvironment"))


def cleanup_empty_env_dir(
    config_defaults: dict[str, Any], environment: dict[str, Any]
) -> bool:
    """Remove the smoke environment directory iff it's empty.

    ``lvlab destroy`` removes each VM's per-VM directory during teardown,
    but leaves the parent environment directory behind empty (issue #100).
    Smoke owns its teardown end-to-end, so it reaps its own env dir here.
    ``os.rmdir`` only removes an *empty* directory, so an env dir that
    still holds files (e.g. a VM whose teardown failed) is never touched —
    that's the safety guarantee that keeps this from being a destructive
    change to the shared ``destroy`` semantics.

    Args:
        config_defaults: The manifest's ``config_defaults``.
        environment: The manifest's ``environment[0]``.

    Returns:
        ``True`` when the directory was removed, ``False`` when it was
        absent or left in place (not empty).
    """
    env_dir = smoke_env_dir(config_defaults, environment)
    try:
        os.rmdir(env_dir)
        return True
    except OSError:
        # Missing, or not empty (a teardown left files) — leave it alone.
        return False


def run_smoke(
    config_path: str,
    *,
    fmt: OutputFormat = OutputFormat.TEXT,
    batch_size: int | None = None,
    max_memory_mib: int | None = None,
    reserve_mib: int = DEFAULT_RESERVE_MIB,
    skip_preflight: bool = False,
    assume_yes: bool = False,
) -> int:  # pragma: no cover - VM lifecycle
    """Run the manifest-driven smoke suite. **Boots real VMs.**

    This is the manual-only entrypoint behind ``lvlab smoke``. It parses the
    manifest, runs preflight, detects host resources, prints the computed
    concurrency plan, drives every case through the lifecycle in resource-aware
    concurrent batches, then emits results in the requested format.

    Args:
        config_path: Path to the manifest (``Lvlab.yml``).
        fmt: Output format.
        batch_size: Explicit concurrency override (else memory-packed).
        max_memory_mib: Cap the memory budget at this many MiB.
        reserve_mib: Safety reserve held back from available memory.
        skip_preflight: Skip the preflight gate (debugging only).
        assume_yes: Skip the memory-heavy confirmation prompt (issue #130).

    Returns:
        Process exit code: ``0`` if every case passed, ``1`` otherwise.

    Raises:
        SmokeError: Manifest missing/empty, or preflight failed.
    """
    parsed = parse_config(config_path)
    if parsed is None:
        raise SmokeError(f"No manifest found at '{config_path}'.")
    environment, images, config_defaults, machines = parsed
    if not machines:
        raise SmokeError(f"Manifest '{config_path}' declares no machines.")

    uri = environment.get("libvirt_uri", DEFAULT_LIBVIRT_URI)
    cases = build_cases(environment, images, config_defaults, machines)

    network_name = (config_defaults.get("interfaces", {}) or {}).get(
        "network", "default"
    )
    try:
        network_info: LibvirtNetworkInfo | None = get_network_info(uri, network_name)
    except Exception:  # noqa: BLE001 - any network read failure -> soft skip
        network_info = None

    # Diagnostic / human-facing output (preflight, plan, status) goes to stdout
    # for TEXT, but to stderr for the machine formats so a `--json`/`--yaml`
    # stdout stays pure and pipeable (e.g. `lvlab smoke --json | jq`). Only the
    # final render_results(json|yaml) is written to stdout in those modes
    # (issue #126).
    diag = sys.stdout if fmt is OutputFormat.TEXT else sys.stderr

    if not skip_preflight:
        checks = run_preflight(
            images, cases, environment, config_defaults, network_info
        )
        for check in checks:
            mark = "ok  " if check.ok else "FAIL"
            print(f"[preflight {mark}] {check.name}: {check.message}", file=diag)
        if any(not c.ok for c in checks):
            raise SmokeError("Preflight failed; refusing to boot VMs.")

    resources = detect_host_resources()
    plan = plan_batches(
        cases,
        resources,
        batch_size=batch_size,
        max_memory_mib=max_memory_mib,
        reserve_mib=reserve_mib,
    )
    # On a terminal with TEXT output, render the plan as a Rich table in the
    # phase-table style (issue #126); otherwise emit the plain-text plan on the
    # diagnostic stream (stderr for the machine formats).
    if fmt is OutputFormat.TEXT and is_tty():
        get_console().print(render_plan_table(plan))
    else:
        print(format_plan(plan), file=diag)
        print(file=diag)

    # Confirm before committing a memory-heavy run, unless --yes (issue #130).
    # Interactive: prompt; non-interactive stdin: refuse and point at --yes so
    # the run never blocks on input that won't come (and CI must opt in).
    if not assume_yes and should_confirm_memory(plan):
        notice = memory_confirm_message(plan)
        if sys.stdin.isatty():
            print(f"{notice} Proceed? [y/N] ", end="", file=diag, flush=True)
            if input().strip().lower() not in ("y", "yes"):
                raise SmokeError("Aborted: memory-heavy run declined.")
        else:
            raise SmokeError(f"{notice} Non-interactive; pass --yes to proceed.")

    lvlab = _lvlab_bin()
    key_path = _ssh_private_key(config_defaults)
    progress = SmokeProgress(cases)
    # Live phase table only for the human-facing TEXT format on a terminal
    # (issue #101); JSON/YAML and piped output stay exactly as before. For the
    # machine formats there is no live table, so leave a one-line "in progress"
    # notice on stderr — visible to the human without touching the stdout pipe.
    live = fmt is OutputFormat.TEXT and is_tty()
    if fmt is not OutputFormat.TEXT:
        print(
            f"smoke: running {len(cases)} case(s) across {len(plan.batches)} "
            f"batch(es); {fmt.value} results print on completion…",
            file=sys.stderr,
        )
    results = _run_batches(
        plan,
        progress,
        lvlab=lvlab,
        uri=uri,
        key_path=key_path,
        live=live,
    )

    order = {c.vm_name: i for i, c in enumerate(cases)}
    results.sort(key=lambda r: order.get(r.vm_name, 0))

    # Every case tore its VM down; reap the now-empty environment storage
    # directory smoke created (issue #100). No-op if a teardown left files.
    if cleanup_empty_env_dir(config_defaults, environment):
        print(
            f"Removed empty environment directory {smoke_env_dir(config_defaults, environment)}",
            file=diag,
        )

    # Final output. The machine formats write only their payload to stdout. For
    # TEXT on a terminal the live phase table already rendered the per-VM grid,
    # so print just the closing summary (no redundant ``===`` re-print, issue
    # #126); piped TEXT (no live table) still gets the full report.
    if fmt is OutputFormat.TEXT and live:
        print(_render_text_summary(results, summarize(results)))
    else:
        print(render_results(results, fmt))
    return 0 if all(r.result == "pass" for r in results) else 1
