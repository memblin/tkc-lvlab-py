"""Scenario types — the behavior behind each declarative registry entry.

Two kinds:

* :class:`CheapScenario` — runs one binary invocation (no VM) and evaluates a
  list of predicates. Used for CLI-contract checks (``--version``, help,
  error panels, the no-manifest landing).
* :class:`CreateVmScenario` — provisions a real guest with ``createvm``,
  verifies it reached ``running`` and acquired its address, optionally peeks
  inside over SSH, then always tears the guest down. Used for the createvm
  matrix that ``lvlab smoke`` does not cover.

Each scenario owns its ``execute`` coroutine; the scheduler only decides which
concurrency primitive (cheap semaphore vs memory pool) wraps it. ``execute``
never raises — failures become :attr:`Status.FAIL`/:attr:`Status.ERROR` on the
returned :class:`ScenarioResult` so one bad scenario can't sink the run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from validate import safety
from validate.context import RunContext
from validate.model import AssertionOutcome, ScenarioResult, Status
from validate.pool import guest_cost_mib
from validate.predicates import ExitCode
from validate.runner import (
    domain_state,
    ping_reachable,
    resolve_dhcp_ip,
    run_binary,
    ssh_capture,
)


class Predicate(Protocol):
    """Anything with a ``check(RunResult) -> AssertionOutcome`` method."""

    def check(self, result):  # noqa: ANN001, D102  (Protocol; see predicates.py)
        ...


@dataclass
class CheapScenario:
    """A no-VM CLI-contract scenario.

    Attributes:
        name: Unique scenario name.
        binary: Which CLI to run (``lvlab``/``createvm``/``deletevm``).
        args: Arguments after the binary (e.g. ``["status"]``).
        asserts: Predicates evaluated against the single invocation.
        cwd_kind: Working-directory shape — ``None`` (a generic empty dir),
            ``"empty"`` (guaranteed no ``Lvlab.yml``), or ``"bad-manifest"``
            (a dir seeded with a structurally invalid ``Lvlab.yml``).
        tags: Issue references / labels for the report.
        needs: Resource lane (always ``"none"`` for cheap scenarios).
    """

    name: str
    binary: str
    args: list[str]
    asserts: list[Predicate]
    cwd_kind: str | None = None
    tags: list[str] = field(default_factory=list)
    needs: str = "none"
    cost_mib: int = 0

    def _prepare_cwd(self, ctx: RunContext) -> str:
        """Create and return the working directory this scenario should run in."""
        cwd = ctx.workdir / "cheap" / self.name
        cwd.mkdir(parents=True, exist_ok=True)
        manifest = cwd / "Lvlab.yml"
        if self.cwd_kind == "bad-manifest":
            # Parses as YAML but is structurally invalid -> ConfigError/TypeError
            # -> _load_config exit 1 (the strict path that must NOT be the
            # friendly no-manifest landing).
            manifest.write_text("environment: not-a-list\n")
        elif manifest.exists():
            manifest.unlink()  # guarantee the no-manifest path for "empty"
        return str(cwd)

    async def execute(self, ctx: RunContext) -> ScenarioResult:
        """Run the binary once and evaluate every predicate.

        Args:
            ctx: The run context.

        Returns:
            The :class:`ScenarioResult`.
        """
        result = ScenarioResult(name=self.name, needs=self.needs, tags=list(self.tags))
        start = time.monotonic()
        try:
            argv = [ctx.binary(self.binary), *self.args]
            run = await run_binary(
                argv, timeout_s=ctx.cmd_timeout_s, cwd=self._prepare_cwd(ctx)
            )
            result.runs.append(run)
            for predicate in self.asserts:
                result.record(predicate.check(run))
        except Exception as exc:  # noqa: BLE001 - scenario isolation boundary
            result.status = Status.ERROR
            result.error = f"{type(exc).__name__}: {exc}"
        result.duration_s = time.monotonic() - start
        return result


@dataclass
class CreateVmScenario:
    """A createvm provisioning scenario against the NAT ``default`` network.

    Attributes:
        name: Unique scenario name (also the suffix of the prefixed domain).
        image: ``VM_DISTRO`` catalog key (e.g. ``"debian13"``).
        user: Guest login user for optional SSH checks.
        ip_mode: ``"dhcp"`` | ``"static"`` | ``"dualstack"`` | ``"nat-flags"``.
        ip4: Static IPv4 (``NETWORK,IP`` or ``IP``) for non-DHCP modes.
        ip6: Static IPv6 for the dual-stack mode.
        extra_args: Additional createvm flags (e.g. the NAT-ignored gateway/dns).
        memory_mib: Guest RAM; also drives the pool's admission cost.
        cpu: vCPU count.
        tags: Issue references / labels.
        observe_v6: When True (dual-stack), record the in-guest v6 picture as a
            soft observation (the #148 extra DHCPv6 ``/128``), never a failure.
        needs: Resource lane (``"exclusive-vm"``).
    """

    name: str
    image: str
    user: str
    ip_mode: str
    ip4: str | None = None
    ip6: str | None = None
    extra_args: list[str] = field(default_factory=list)
    memory_mib: int = 1024
    cpu: int = 1
    tags: list[str] = field(default_factory=list)
    observe_v6: bool = False
    needs: str = "exclusive-vm"

    @property
    def domain(self) -> str:
        """The prefixed libvirt domain name createvm will define."""
        return safety.make_name(self.name)

    @property
    def cost_mib(self) -> int:
        """Budget cost for the pool (guest RAM + overhead)."""
        return guest_cost_mib(self.memory_mib)

    def _createvm_argv(self, ctx: RunContext) -> list[str]:
        """Assemble the createvm argument vector for this scenario."""
        argv = [
            ctx.binary("createvm"),
            self.domain,
            self.image,
            "--memory",
            str(self.memory_mib),
            "--cpu",
            str(self.cpu),
            "--disk-size",
            "10G",
        ]
        if self.ip4:
            argv += ["--ip4", self.ip4]
        if self.ip6:
            argv += ["--ip6", self.ip6]
        argv += self.extra_args
        return argv

    async def _verify(self, ctx: RunContext, result: ScenarioResult) -> None:
        """Assert the guest reached ``running`` and acquired its address."""
        state = domain_state(ctx, self.domain)
        result.record(
            AssertionOutcome(
                description="domain state == running",
                passed=state == "running",
                detail=f"state={state}",
            )
        )
        if state != "running":
            return

        if self.ip_mode == "dhcp":
            ip = await resolve_dhcp_ip(ctx, self.domain)
            result.record(
                AssertionOutcome(
                    description="DHCP lease resolved",
                    passed=ip is not None,
                    detail=f"ip={ip}",
                )
            )
        else:
            ip = self.ip4.split(",")[-1] if self.ip4 else None
            await self._verify_static(ctx, ip, result)

        if ip is not None:
            await self._peek_guest(ctx, ip, result)

    async def _verify_static(
        self, ctx: RunContext, ip: str | None, result: ScenarioResult
    ) -> None:
        """Confirm the configured static IPv4 is live by pinging it from the host.

        The host owns the NAT gateway on the same subnet, so a successful ICMP
        probe proves the guest applied the static address and brought the NIC up
        — no SSH required. Polls generously (the DHCP path shows ~30s boots), so
        a sustained failure is a real finding, not boot-time slowness.
        """
        result.observations.append(f"configured static IPv4: {ip}")
        if ip is None:
            return
        reachable = await ping_reachable(ctx, ip, retries=ctx.dhcp_poll_retries)
        result.record(
            AssertionOutcome(
                description="static IPv4 reachable (host ICMP)",
                passed=reachable,
                detail=f"ip={ip} reachable={reachable}",
            )
        )

    async def _peek_guest(
        self, ctx: RunContext, ip: str, result: ScenarioResult
    ) -> None:
        """Optional in-guest SSH checks (connectivity + the #148 v6 observation)."""
        ssh = await ssh_capture(
            ctx, self.user, ip, ["ip", "-o", "addr", "show", "scope", "global"]
        )
        if ssh is None:
            result.observations.append("in-guest checks skipped (no --ssh-key)")
            return
        result.record(
            AssertionOutcome(
                description="SSH reachable + addresses readable",
                passed=ssh.returncode == 0,
                detail=ssh.stderr.strip()[:120] if ssh.returncode != 0 else "ok",
            )
        )
        if self.observe_v6 and ssh.returncode == 0:
            v6 = [
                ln
                for ln in ssh.stdout.splitlines()
                if "inet6" in ln and "scope global" in ln
            ]
            dynamic = [ln for ln in v6 if "dynamic" in ln or "/128" in ln]
            if dynamic:
                result.observations.append(
                    "issue #148: extra DHCPv6-style /128 alongside the static v6 — "
                    + "; ".join(s.strip() for s in dynamic)
                )
            else:
                result.observations.append(
                    "issue #148: no extra DHCPv6 /128 observed (only the static v6)"
                )

    async def _teardown(self, ctx: RunContext, result: ScenarioResult) -> None:
        """Always-run cleanup: deletevm, then the prefix reaper as a backstop."""
        safety.assert_owned(self.domain)
        run = await run_binary(
            [ctx.binary("deletevm"), self.domain, "--force", "--snapshots-too"],
            timeout_s=ctx.cmd_timeout_s,
        )
        result.runs.append(run)
        # Backstop: if deletevm left the domain (or never ran), reap by prefix.
        if domain_state(ctx, self.domain) != "absent":
            safety.reap_domain(ctx.uri, self.domain)

    async def execute(self, ctx: RunContext) -> ScenarioResult:
        """Provision -> verify -> (peek) -> teardown for one guest.

        Args:
            ctx: The run context.

        Returns:
            The :class:`ScenarioResult`. Skipped (no VM) under ``ctx.dry_run``.
        """
        result = ScenarioResult(name=self.name, needs=self.needs, tags=list(self.tags))
        if ctx.dry_run:
            result.status = Status.SKIP
            result.observations.append("dry-run: guest not provisioned")
            return result

        start = time.monotonic()
        try:
            run = await run_binary(
                self._createvm_argv(ctx), timeout_s=ctx.boot_timeout_s
            )
            result.runs.append(run)
            result.record(ExitCode(0).check(run))
            if run.returncode == 0:
                await self._verify(ctx, result)
        except Exception as exc:  # noqa: BLE001 - scenario isolation boundary
            result.status = Status.ERROR
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                await self._teardown(ctx, result)
            except Exception as exc:  # noqa: BLE001 - teardown must not mask the result
                result.observations.append(
                    f"teardown error: {type(exc).__name__}: {exc}"
                )
        result.duration_s = time.monotonic() - start
        return result
