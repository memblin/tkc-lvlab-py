"""Low-level execution primitives: run a binary, resolve a lease, peek a guest.

These are the only things in the harness that touch real processes / libvirt;
keeping them here lets the handlers stay readable and lets the pure parsing be
unit-tested in isolation.
"""

from __future__ import annotations

import asyncio
import time

from tkc_lvlab.smoke import _parse_domifaddr_lease
from tkc_lvlab.utils.virsh import VirshError, run_virsh, virsh_domstate

from validate.context import RunContext
from validate.model import RunResult


async def run_binary(
    argv: list[str], *, timeout_s: float, cwd: str | None = None
) -> RunResult:
    """Execute ``argv`` and capture its outcome.

    Args:
        argv: Full argument vector (``argv[0]`` is the resolved binary path).
        timeout_s: Kill the process and mark ``timed_out`` after this many seconds.
        cwd: Working directory for the child (``lvlab up`` reads ``./Lvlab.yml``).

    Returns:
        A :class:`RunResult`. A binary that never starts yields ``returncode=-1``.
    """
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    except OSError as exc:
        return RunResult(
            argv=argv, returncode=-1, stdout="", stderr=str(exc), duration_s=0.0
        )

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        return RunResult(
            argv=argv,
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=out.decode(errors="replace"),
            stderr=err.decode(errors="replace"),
            duration_s=time.monotonic() - start,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return RunResult(
            argv=argv,
            returncode=-1,
            stdout="",
            stderr=f"timed out after {timeout_s:.0f}s",
            duration_s=time.monotonic() - start,
            timed_out=True,
        )


def domain_state(ctx: RunContext, domain: str) -> str:
    """Return the libvirt run-state of ``domain`` (``"running"``, ``"shut off"``, …).

    Args:
        ctx: The run context (for the libvirt URI).
        domain: Domain name to query.

    Returns:
        The lowercase state string, or ``"absent"`` when the domain is undefined
        or libvirt can't be reached.
    """
    try:
        return virsh_domstate(ctx.uri, domain)
    except VirshError:
        return "absent"


async def resolve_ip(
    ctx: RunContext, domain: str, *, source: str, retries: int | None = None
) -> str | None:
    """Poll ``virsh domifaddr --source <source>`` until an IPv4 address appears.

    Reuses :func:`tkc_lvlab.smoke._parse_domifaddr_lease` so the harness reads
    addresses exactly as ``lvlab smoke`` does (by the running domain, not by a
    self-generated MAC — see issue #125).

    Args:
        ctx: The run context (URI + poll cadence).
        domain: The running domain to resolve.
        source: ``"lease"`` (dnsmasq DHCP) or ``"arp"`` (host neighbour table —
            populated once the guest emits traffic, so it surfaces a *static*
            address without DHCP).
        retries: Poll count override (defaults to ``ctx.dhcp_poll_retries``).

    Returns:
        The first dotted-quad IPv4 address seen, or ``None`` if none appeared.
    """
    for _ in range(retries if retries is not None else ctx.dhcp_poll_retries):
        try:
            result = run_virsh(
                ctx.uri, ["domifaddr", domain, "--source", source], check=False
            )
        except VirshError:
            result = None
        if result is not None and result.returncode == 0:
            ip = _parse_domifaddr_lease(result.stdout)
            if ip:
                return ip
        await asyncio.sleep(ctx.dhcp_poll_interval_s)
    return None


async def resolve_dhcp_ip(ctx: RunContext, domain: str) -> str | None:
    """Poll the dnsmasq lease table for the guest's IPv4 address (DHCP path)."""
    return await resolve_ip(ctx, domain, source="lease")


async def ping_reachable(
    ctx: RunContext, ip: str, *, retries: int, interval_s: float = 3.0
) -> bool:
    """Return True once ``ip`` answers ICMP from the host, polling up to ``retries`` times.

    The host shares the NAT subnet (it owns the ``virbr0`` gateway), so a static
    guest address is directly pingable once the guest finishes cloud-init and
    brings the NIC up — a host-side proof that static addressing applied, with
    no SSH required.

    Args:
        ctx: The run context (for command timeout).
        ip: The address to probe.
        retries: How many times to retry before giving up.
        interval_s: Seconds between attempts.

    Returns:
        True if a probe succeeded within the budget, else False.
    """
    for _ in range(retries):
        run = await run_binary(
            ["ping", "-c", "1", "-W", "2", ip], timeout_s=ctx.cmd_timeout_s
        )
        if run.returncode == 0:
            return True
        await asyncio.sleep(interval_s)
    return False


async def ssh_capture(
    ctx: RunContext, user: str, ip: str, remote_cmd: list[str]
) -> RunResult | None:
    """Run ``remote_cmd`` inside the guest over SSH, or return ``None`` if disabled.

    Used for connectivity proof and in-guest observation (e.g. reading ``ip
    addr`` for the #148 DHCPv6 check). No-op when ``ctx.ssh_key`` is unset.

    Args:
        ctx: The run context (SSH key + timeout).
        user: Guest login user (the image's default username).
        ip: Guest IP to connect to.
        remote_cmd: Command vector to run inside the guest.

    Returns:
        A :class:`RunResult` of the SSH invocation, or ``None`` if SSH is off.
    """
    if ctx.ssh_key is None:
        return None
    argv = [
        "ssh",
        "-i",
        str(ctx.ssh_key),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        f"{user}@{ip}",
        *remote_cmd,
    ]
    return await run_binary(argv, timeout_s=ctx.cmd_timeout_s)
