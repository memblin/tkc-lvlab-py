"""Unit tests for the harness engine: pool admission, scheduler, report, argv.

The real VM provisioning can't be unit-tested, but everything that decides
*what* runs, *whether* it fits memory, and *how* results are projected can —
and a bug in any of those silently corrupts a run.
"""

from __future__ import annotations

import asyncio

import pytest

from validate import pool as pool_mod
from validate import registry, report
from validate.context import RunContext
from validate.model import AssertionOutcome, ScenarioResult, Status
from validate.pool import VmPool, compute_budget_mib, guest_cost_mib
from validate.scenarios import CheapScenario, CreateVmScenario
from validate.scheduler import run_all

# --- pool ------------------------------------------------------------------


def test_guest_cost_adds_overhead() -> None:
    assert guest_cost_mib(1024) == 1024 + pool_mod.PER_GUEST_OVERHEAD_MIB


def test_compute_budget_subtracts_reserve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pool_mod,
        "detect_host_resources",
        lambda: type(
            "R",
            (),
            {"available_memory_mib": 8192, "total_memory_mib": 16384, "vcpus": 8},
        )(),
    )
    assert compute_budget_mib(reserve_mib=2048) == 6144


def test_compute_budget_floors_at_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pool_mod,
        "detect_host_resources",
        lambda: type(
            "R",
            (),
            {"available_memory_mib": 1024, "total_memory_mib": 2048, "vcpus": 2},
        )(),
    )
    assert compute_budget_mib(reserve_mib=2048) == 0


def test_pool_serializes_when_budget_too_small_for_two() -> None:
    """Two 1 GiB guests against a 1.5 GiB budget must NOT run concurrently."""
    pool = VmPool(budget_mib=1536)
    peak = 0
    current = 0

    async def guest() -> None:
        nonlocal peak, current
        async with pool.lease(1024):
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.02)
            current -= 1

    async def both() -> None:
        await asyncio.wait_for(asyncio.gather(guest(), guest()), timeout=5)

    asyncio.run(both())
    assert peak == 1  # second waited for the first to release


def test_pool_admits_oversized_guest_alone() -> None:
    """A guest larger than the whole budget still runs (clamped), no deadlock."""
    pool = VmPool(budget_mib=512)

    async def guest() -> str:
        async with pool.lease(4096):
            return "ran"

    assert asyncio.run(asyncio.wait_for(guest(), timeout=5)) == "ran"


# --- scheduler -------------------------------------------------------------


class _FakeScenario:
    def __init__(self, name: str, needs: str) -> None:
        self.name, self.needs, self.cost_mib = name, needs, 1024
        self.ran = False

    async def execute(self, ctx: RunContext) -> ScenarioResult:
        self.ran = True
        return ScenarioResult(name=self.name, needs=self.needs)


def test_scheduler_runs_both_lanes() -> None:
    scenarios = [_FakeScenario("cheap1", "none"), _FakeScenario("vm1", "exclusive-vm")]
    results = asyncio.run(
        run_all(scenarios, RunContext(), cheap_concurrency=4, pool=VmPool(4096))
    )
    assert {r.name for r in results} == {"cheap1", "vm1"}
    assert all(s.ran for s in scenarios)


# --- report projection -----------------------------------------------------


def _sample_results() -> list[ScenarioResult]:
    ok = ScenarioResult(name="lvlab-version", needs="none", tags=["#138"])
    ok.record(AssertionOutcome("exit code == 0", True, "got 0"))
    bad = ScenarioResult(name="cvm-x", needs="exclusive-vm", tags=["#137"])
    bad.record(AssertionOutcome("domain running", False, "state=shut off"))
    bad.observations.append("issue #148: extra DHCPv6 /128 ...")
    skipped = ScenarioResult(name="cvm-y", needs="exclusive-vm", status=Status.SKIP)
    return [ok, bad, skipped]


def test_status_counts() -> None:
    counts = report.status_counts(_sample_results())
    assert counts == {"pass": 1, "fail": 1, "error": 0, "skip": 1}


def test_to_json_roundtrips() -> None:
    import json

    doc = json.loads(report.to_json(_sample_results(), meta={"prefix": "p-"}))
    assert doc["summary"]["fail"] == 1
    assert doc["scenarios"][0]["name"] == "lvlab-version"
    assert doc["meta"]["prefix"] == "p-"


def test_issue_markdown_lists_failures_and_observations() -> None:
    md = report.issue_markdown(_sample_results(), meta={"uri": "qemu:///system"})
    assert "Proposed sub-issues" in md
    assert "cvm-x" in md
    assert "issue #148" in md  # observation surfaced


# --- scenario argv / identity ----------------------------------------------


def test_createvm_argv_is_prefixed_and_well_formed() -> None:
    s = CreateVmScenario(
        name="cvm-deb13-dualstack",
        image="debian13",
        user="debian",
        ip_mode="dualstack",
        ip4="default,192.168.122.51",
        ip6="default,fdfa:cade::51",
        memory_mib=1024,
    )
    argv = s._createvm_argv(RunContext())
    assert s.domain.startswith("lvlab-validate-")
    assert argv[1] == s.domain and argv[2] == "debian13"
    assert "--ip4" in argv and "default,192.168.122.51" in argv
    assert "--ip6" in argv and "default,fdfa:cade::51" in argv


def test_registry_names_are_unique() -> None:
    names = [s.name for s in registry.all_scenarios()]
    assert len(names) == len(set(names))


def test_dualstack_scenario_observes_v6() -> None:
    """The dual-stack entry must be flagged to capture the #148 observation."""
    dual = next(s for s in registry.CREATEVM_SCENARIOS if s.ip_mode == "dualstack")
    assert dual.observe_v6 is True
    assert "#148" in dual.tags
