"""Two-lane async scheduler.

Cheap scenarios (``needs == "none"``) fan out under a wide semaphore; stateful
scenarios acquire a memory lease from the :class:`~validate.pool.VmPool` so the
host's spare RAM bounds how many guests boot at once. Both lanes run on the
same event loop, so a long-booting guest never blocks the cheap checks.
"""

from __future__ import annotations

import asyncio

from validate.context import RunContext
from validate.model import ScenarioResult
from validate.pool import VmPool


async def run_all(
    scenarios: list,
    ctx: RunContext,
    *,
    cheap_concurrency: int,
    pool: VmPool,
) -> list[ScenarioResult]:
    """Execute every scenario under its lane's concurrency primitive.

    Args:
        scenarios: Scenario instances (each exposing ``needs``, ``cost_mib``,
            and an ``execute`` coroutine).
        ctx: The shared run context.
        cheap_concurrency: Max concurrent no-VM scenarios.
        pool: The memory-budgeted pool gating stateful scenarios.

    Returns:
        Results in the same order as ``scenarios``.
    """
    cheap_sem = asyncio.Semaphore(cheap_concurrency)

    async def run_one(scenario) -> ScenarioResult:
        if scenario.needs == "none":
            async with cheap_sem:
                return await scenario.execute(ctx)
        async with pool.lease(scenario.cost_mib):
            return await scenario.execute(ctx)

    return await asyncio.gather(*(run_one(s) for s in scenarios))
