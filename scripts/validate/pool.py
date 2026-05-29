"""Memory-budgeted VM-lease pool for the stateful scenario lane.

Cheap scenarios fan out under a wide semaphore; stateful scenarios each boot a
real guest, so they must not collectively exceed the host's spare RAM. This
pool admits a scenario only when its guest memory fits the remaining budget,
reusing the same ``available - reserve`` math as ``lvlab smoke`` (the harness
imports :func:`tkc_lvlab.smoke.detect_host_resources` so the two agree).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from tkc_lvlab.smoke import DEFAULT_RESERVE_MIB, detect_host_resources

# Per-guest overhead beyond the configured guest RAM (qemu, firmware, page
# tables). Matches the conservative slack smoke.py packs with.
PER_GUEST_OVERHEAD_MIB = 256


def compute_budget_mib(reserve_mib: int = DEFAULT_RESERVE_MIB) -> int:
    """Return the memory budget (MiB) the stateful lane may pack guests into.

    Args:
        reserve_mib: RAM held back for the host OS, the harness, and qemu slack.

    Returns:
        ``available_memory - reserve``, floored at 0.
    """
    resources = detect_host_resources()
    return max(0, resources.available_memory_mib - reserve_mib)


def guest_cost_mib(memory_mib: int) -> int:
    """Return the budget cost of a guest configured for ``memory_mib`` of RAM."""
    return memory_mib + PER_GUEST_OVERHEAD_MIB


class VmPool:
    """Async admission controller bounding concurrent guest memory.

    A scenario calls :meth:`lease` (an async context manager) with its guest
    cost; the pool blocks until that cost fits the remaining budget, then
    deducts it for the lease's duration. A single guest larger than the whole
    budget is still admitted alone (so the run can't deadlock) — the caller is
    responsible for not configuring a guest larger than the host.
    """

    def __init__(self, budget_mib: int) -> None:
        """Initialize the pool.

        Args:
            budget_mib: Total guest memory (MiB) that may be in flight at once.
        """
        self.budget_mib = budget_mib
        self._remaining = budget_mib
        self._cond = asyncio.Condition()

    @asynccontextmanager
    async def lease(self, cost_mib: int) -> AsyncIterator[None]:
        """Acquire ``cost_mib`` of budget for the duration of the ``with`` block.

        Args:
            cost_mib: Memory cost of the guest about to boot.

        Yields:
            None, once the budget is reserved.
        """
        # Clamp so an oversized guest still runs (alone) instead of deadlocking.
        effective = min(cost_mib, self.budget_mib) if self.budget_mib > 0 else cost_mib
        async with self._cond:
            await self._cond.wait_for(lambda: self._remaining >= effective)
            self._remaining -= effective
        try:
            yield
        finally:
            async with self._cond:
                self._remaining += effective
                self._cond.notify_all()
