"""Command-line entry point for the lvlab CLI conformance harness.

Run from the repo root::

    uv run python -m validate --dry-run          # cheap lane only, no VMs
    uv run python -m validate --yes               # full run, provisions guests
    uv run python -m validate --only cvm-deb13-dhcp

It drives the installed ``lvlab``/``createvm``/``deletevm`` binaries, schedules
scenarios across the cheap and stateful lanes, writes a JSON + markdown report
under ``scripts/results/``, and exits non-zero if any scenario failed.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time
from pathlib import Path

# Allow ``python scripts/validate/__main__.py`` (no package context) to import siblings.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from validate import registry, report, safety  # noqa: E402
from validate.context import RunContext  # noqa: E402
from validate.model import Status  # noqa: E402
from validate.pool import VmPool, compute_budget_mib  # noqa: E402
from validate.scheduler import run_all  # noqa: E402

_RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


def _git_describe() -> str:
    """Best-effort ``git describe`` of the checkout under test."""
    try:
        out = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="validate", description="lvlab CLI conformance harness"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="cheap lane only; provision no VMs"
    )
    p.add_argument(
        "--yes", "-y", action="store_true", help="skip the stateful-lane confirmation"
    )
    p.add_argument("--only", default="", help="comma-separated scenario names to run")
    p.add_argument("--lane", choices=("all", "cheap", "stateful"), default="all")
    p.add_argument("--uri", default="qemu:///system")
    p.add_argument(
        "--bin-dir", type=Path, default=None, help="dir holding lvlab/createvm/deletevm"
    )
    p.add_argument(
        "--ssh-key",
        type=Path,
        default=None,
        help="key for in-guest checks (else skipped)",
    )
    p.add_argument("--workdir", type=Path, default=Path("/tmp/lvlab-validate"))
    p.add_argument("--cheap-concurrency", type=int, default=8)
    p.add_argument("--reserve-mib", type=int, default=2048)
    p.add_argument("--out", type=Path, default=_RESULTS_DIR)
    return p.parse_args(argv)


def _select(args: argparse.Namespace) -> list:
    scenarios = registry.all_scenarios()
    if args.lane == "cheap":
        scenarios = [s for s in scenarios if s.needs == "none"]
    elif args.lane == "stateful":
        scenarios = [s for s in scenarios if s.needs != "none"]
    if args.only:
        wanted = {n.strip() for n in args.only.split(",")}
        scenarios = [s for s in scenarios if s.name in wanted]
    return scenarios


def _confirm_stateful(scenarios: list, budget_mib: int, uri: str) -> bool:
    """Show the host's current domains + the plan, and gate the stateful lane."""
    stateful = [s for s in scenarios if s.needs != "none"]
    if not stateful:
        return True
    existing = safety.virsh_list_all_names(uri)
    leftovers = safety.list_prefixed_domains(uri)
    print(
        f"\nStateful lane: {len(stateful)} guest(s), pool budget {budget_mib} MiB on {uri}"
    )
    print(
        f"Existing domains on host ({len(existing)}): {', '.join(existing) or '(none)'}"
    )
    print(
        "Only prefixed domains are ever reaped; existing domains above are untouched."
    )
    if leftovers:
        print(
            f"Reaping {len(leftovers)} leftover harness domain(s) from a prior run: {leftovers}"
        )
        safety.reap_prefixed_domains(uri)
    if not sys.stdin.isatty():
        print("Non-interactive and --yes not given: skipping the stateful lane.")
        return False
    return input("Proceed with provisioning these guests? [y/N] ").strip().lower() in (
        "y",
        "yes",
    )


async def _run(args: argparse.Namespace) -> int:
    ctx = RunContext(
        uri=args.uri,
        bin_dir=args.bin_dir,
        workdir=args.workdir,
        ssh_key=args.ssh_key,
        dry_run=args.dry_run,
    )
    ctx.workdir.mkdir(parents=True, exist_ok=True)
    budget = compute_budget_mib(args.reserve_mib)
    scenarios = _select(args)

    run_stateful = True
    if not args.dry_run and not args.yes:
        run_stateful = _confirm_stateful(scenarios, budget, args.uri)
    if not run_stateful:
        scenarios = [s for s in scenarios if s.needs == "none"]

    print(
        f"\nRunning {len(scenarios)} scenario(s)…  prefix={safety.LVLAB_VALIDATE_PREFIX}"
    )
    results = await run_all(
        scenarios, ctx, cheap_concurrency=args.cheap_concurrency, pool=VmPool(budget)
    )

    meta = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "git_describe": _git_describe(),
        "uri": args.uri,
        "prefix": safety.LVLAB_VALIDATE_PREFIX,
        "budget_mib": budget,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    stamp = f"validate-{meta['git_describe']}-{int(time.time())}"
    (args.out / f"{stamp}.json").write_text(report.to_json(results, meta=meta))
    (args.out / f"{stamp}.issue.md").write_text(
        report.issue_markdown(results, meta=meta)
    )

    print("\n" + report.human_summary(results))
    print(f"\nWrote {args.out / (stamp + '.json')}")

    failed = sum(1 for r in results if r.status in (Status.FAIL, Status.ERROR))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    """Parse args, run the harness, return a process exit code."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    # Final backstop: reap any prefixed leftovers even if the run crashes.
    try:
        return asyncio.run(_run(args))
    finally:
        if not args.dry_run:
            safety.reap_prefixed_domains(args.uri)
            safety.reap_prefixed_storage(
                (
                    safety.VALIDATE_STORAGE_ROOT,
                    Path("/var/lib/libvirt/images/lvlab/oneoff"),
                )
            )


if __name__ == "__main__":
    raise SystemExit(main())
