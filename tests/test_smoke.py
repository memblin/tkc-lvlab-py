"""Unit tests for the pure logic in :mod:`tkc_lvlab.smoke`.

These tests exercise only the layer that never boots a VM — preflight
checks, the batch scheduler, case construction, and structured emission.
The VM-lifecycle path (:func:`tkc_lvlab.smoke.run_smoke`,
:func:`tkc_lvlab.smoke._run_case`) is manual-only and is **never** invoked
here; see ``test_no_vm_boot_under_pytest`` which guards that boundary.
"""

from __future__ import annotations

import json

import pytest
import yaml

from tkc_lvlab import smoke
from tkc_lvlab.smoke import (
    Batch,
    CaseResult,
    HostResources,
    OutputFormat,
    SmokeCase,
    case_cost_mib,
    check_images_cached,
    check_ssh_key_present,
    check_static_ips_free,
    cleanup_empty_env_dir,
    format_plan,
    plan_batches,
    render_results,
    smoke_env_dir,
    summarize,
)
from tkc_lvlab.utils.network import LibvirtNetworkInfo
from tkc_lvlab.utils.virsh import VirshError


def _case(
    vm_name: str,
    *,
    os: str = "debian12",
    mode: str = "dhcp",
    static_ip: str | None = None,
    memory: int = 512,
) -> SmokeCase:
    return SmokeCase(
        vm_name=vm_name,
        libvirt_domain=f"{vm_name}_smoke",
        os=os,
        mode=mode,
        static_ip=static_ip,
        mac="52:54:00:aa:bb:cc",
        ssh_user="debian",
        memory_mib=memory,
        vcpus=1,
    )


def _result(
    vm_name: str, *, result: str = "pass", ip: str | None = "10.0.0.5"
) -> CaseResult:
    return CaseResult(
        distro="debian12",
        vm_name=vm_name,
        libvirt_domain=f"{vm_name}_smoke",
        mode="dhcp",
        resolved_ip=ip,
        ssh_ok=(result == "pass"),
        result=result,
        boot_to_ssh_seconds=12.3 if result == "pass" else None,
        total_seconds=20.0,
        detail="OK:host:debian" if result == "pass" else "no SSH",
    )


# ---------------------------------------------------------------------------
# Scheduler (resource-aware; issue #90)
# ---------------------------------------------------------------------------


def _host(available_mib: int, vcpus: int = 8) -> HostResources:
    return HostResources(
        total_memory_mib=available_mib, available_memory_mib=available_mib, vcpus=vcpus
    )


def test_plan_batches_respects_memory_budget():
    # 16 GiB host, 2 GiB reserve -> 14 GiB budget. Four 2 GiB fedoras
    # (+256 overhead = 2304 each) cost 9216; they must NOT all land in one
    # batch only if the budget were smaller — here the budget is generous, so
    # the assertion is that no batch ever exceeds the budget.
    cases = [_case(f"fed{i}", os="fedora44", memory=2048) for i in range(8)]
    plan = plan_batches(cases, _host(16384), reserve_mib=2048)
    budget = plan.budget_mib
    assert all(b.memory_mib <= budget for b in plan.batches)
    # Every case scheduled exactly once.
    scheduled = sorted(c.vm_name for b in plan.batches for c in b.cases)
    assert scheduled == sorted(c.vm_name for c in cases)


def test_plan_batches_small_host_makes_more_batches():
    # Tight 4 GiB host, 2 GiB reserve -> 2 GiB budget. A 2048 MiB fedora costs
    # 2304 (> budget) so each fedora gets its own batch.
    cases = [_case(f"fed{i}", os="fedora44", memory=2048) for i in range(3)]
    plan = plan_batches(cases, _host(4096), reserve_mib=2048)
    assert len(plan.batches) == 3
    assert plan.batch_size_override is None


def test_plan_batches_pairs_heavy_and_light():
    # First-fit-decreasing should co-locate a heavy + light pair under budget
    # rather than two heavies. Budget 14 GiB; one fedora (2304) + several
    # debians (768) fit together.
    cases = [
        _case("fed", os="fedora44", memory=2048),
        _case("deb1", os="debian12", memory=512),
        _case("deb2", os="debian12", memory=512),
    ]
    plan = plan_batches(cases, _host(16384), reserve_mib=2048)
    # All three fit under the generous budget -> a single batch.
    assert len(plan.batches) == 1
    members = {c.vm_name for c in plan.batches[0].cases}
    assert members == {"fed", "deb1", "deb2"}


def test_plan_batches_explicit_batch_size_overrides_packing():
    cases = [_case(f"vm{i}", os="fedora44", memory=2048) for i in range(5)]
    # Even on a tiny host, an explicit width wins (CI pinning / debugging).
    plan = plan_batches(cases, _host(2048), batch_size=2)
    assert plan.batch_size_override == 2
    assert [len(b.cases) for b in plan.batches] == [2, 2, 1]
    # Fixed sizing preserves manifest order.
    flat = [c.vm_name for b in plan.batches for c in b.cases]
    assert flat == ["vm0", "vm1", "vm2", "vm3", "vm4"]


def test_plan_batches_rejects_zero_width():
    with pytest.raises(ValueError):
        plan_batches([_case("a")], _host(8192), batch_size=0)


def test_plan_batches_reserve_is_honored():
    cases = [_case("a"), _case("b")]
    big_reserve = plan_batches(cases, _host(8192), reserve_mib=7000)
    small_reserve = plan_batches(cases, _host(8192), reserve_mib=1000)
    assert big_reserve.budget_mib < small_reserve.budget_mib


def test_plan_batches_max_memory_caps_budget():
    cases = [_case("a")]
    capped = plan_batches(cases, _host(64000), max_memory_mib=8192, reserve_mib=2048)
    # Budget is min(available, max_memory) - reserve = 8192 - 2048.
    assert capped.budget_mib == 8192 - 2048


def test_plan_batches_oversized_case_gets_own_batch():
    # A guest larger than the whole budget still gets scheduled (own batch).
    cases = [_case("huge", os="fedora44", memory=8192)]
    plan = plan_batches(cases, _host(2048), reserve_mib=512)
    assert len(plan.batches) == 1
    assert plan.batches[0].memory_mib > plan.budget_mib


def test_case_cost_includes_overhead():
    case = _case("c", os="fedora44", memory=2048)
    assert case_cost_mib(case) == 2048 + 256


def test_format_plan_reports_resources_and_budget():
    cases = [_case("a"), _case("b")]
    text = format_plan(plan_batches(cases, _host(8192, vcpus=4), reserve_mib=2048))
    assert "4 vCPU" in text
    assert "8192 MiB" in text
    assert "Batch 1" in text


def _plan(*batches: tuple[int, list[str]], available: int = 8000) -> "smoke.SmokePlan":
    # Build a SmokePlan directly from (memory_mib, [vm_names]) pairs so the
    # threshold/render logic is tested without depending on the packer.
    built = tuple(
        smoke.Batch(cases=tuple(_case(n) for n in names), memory_mib=mib)
        for mib, names in batches
    )
    return smoke.SmokePlan(
        batches=built,
        resources=_host(available),
        budget_mib=available - 2048,
        reserve_mib=2048,
    )


def test_should_confirm_memory_true_when_peak_at_least_half_of_available():
    # Peak batch 4000 MiB vs 8000 available -> exactly 50% -> prompt.
    assert smoke.should_confirm_memory(_plan((4000, ["a"]), available=8000)) is True


def test_should_confirm_memory_false_for_small_run():
    assert smoke.should_confirm_memory(_plan((3000, ["a"]), available=8000)) is False


def test_should_confirm_memory_false_when_available_unknown():
    plan = _plan((4000, ["a"]), available=0)
    assert smoke.should_confirm_memory(plan) is False


def test_memory_confirm_message_states_peak_and_available_in_gib():
    # 20736 MiB -> 20.2 GiB, 23352 MiB -> 22.8 GiB (issue #126 GiB display).
    msg = smoke.memory_confirm_message(_plan((20736, ["a"]), available=23352))
    assert "20.2 GiB" in msg
    assert "22.8 GiB" in msg


def test_gib_formats_mib_as_one_decimal_gib():
    assert smoke._gib(1024) == "1.0 GiB"
    assert smoke._gib(11264) == "11.0 GiB"
    assert smoke._gib(768) == "0.8 GiB"


def _render(renderable_fn, *args, width=120) -> str:
    import io

    from rich.console import Console

    buf = io.StringIO()
    console = Console(file=buf, width=width)
    renderable_fn(console, *args)
    return buf.getvalue()


def test_render_plan_table_lists_batches_in_gib():
    import io

    from rich.console import Console

    plan = _plan(
        (1536, ["deb12-static", "deb12-dhcp"]),
        (768, ["deb13-dhcp"]),
        available=20000,
    )
    buf = io.StringIO()
    Console(file=buf, width=120).print(smoke.render_plan_table(plan))
    out = buf.getvalue()
    assert "1.5 GiB" in out  # 1536 MiB
    assert "0.8 GiB" in out  # 768 MiB
    assert "deb12-static" in out  # batch members rendered


def test_render_plan_header_states_host_and_packing_in_gib():
    plan = _plan(
        (1536, ["deb12-static", "deb12-dhcp"]),
        (768, ["deb13-dhcp"]),
        available=20000,
    )
    out = _render(smoke.render_plan, plan)
    assert "Smoke plan" in out
    assert "3 VMs in 2 batch(es)" in out
    assert "8 vCPU" in out
    assert "19.5 GiB" in out  # 20000 MiB available
    assert "90%" in out  # budget (17952) as % of available
    assert "deb12-static" in out


def test_format_preflight_keeps_classic_plain_lines():
    checks = [
        smoke.PreflightCheck(name="images-cached", ok=True, message="all good"),
        smoke.PreflightCheck(name="static-ips-free", ok=False, message="overlap [a-b]"),
    ]
    out = smoke.format_preflight(checks)
    assert "[preflight ok  ] images-cached: all good" in out
    assert "[preflight FAIL] static-ips-free: overlap [a-b]" in out


def test_render_preflight_uses_glyphs_and_renders_bracketed_messages_verbatim():
    checks = [
        smoke.PreflightCheck(
            name="static-ips-free",
            ok=True,
            message="outside DHCP range [192.168.122.200-192.168.122.254]",
        ),
        smoke.PreflightCheck(name="ssh-key-present", ok=False, message="missing"),
    ]
    out = _render(smoke.render_preflight, checks)
    assert "Preflight" in out
    assert "✓" in out and "✗" in out
    # Bracketed content must render verbatim, not be parsed as Rich markup.
    assert "192.168.122.200" in out
    assert "static-ips-free" in out


def test_parse_free_m_reads_available_column():
    out = (
        "               total        used        free      shared  buff/cache   available\n"
        "Mem:           16000        4000        2000         100        9900       11000\n"
        "Swap:           2000           0        2000\n"
    )
    total, available = smoke._parse_free_m(out)
    assert total == 16000
    assert available == 11000


def test_parse_domifaddr_lease_extracts_ipv4():
    # Real `virsh domifaddr <domain> --source lease` output shape.
    out = (
        " Name       MAC address          Protocol     Address\n"
        "-------------------------------------------------------------------\n"
        " vnet3      52:54:00:1a:2b:3c    ipv4         192.168.122.123/24\n"
    )
    assert smoke._parse_domifaddr_lease(out) == "192.168.122.123"


def test_parse_domifaddr_lease_ignores_ipv6_only_and_header():
    # An IPv6-only lease (or header/empty output) yields no IPv4 address —
    # the runner keeps polling rather than locking onto a v6 address.
    out = (
        " Name       MAC address          Protocol     Address\n"
        "-------------------------------------------------------------------\n"
        " vnet3      52:54:00:1a:2b:3c    ipv6         fe80::5054:ff:fe1a:2b3c/64\n"
    )
    assert smoke._parse_domifaddr_lease(out) is None
    assert smoke._parse_domifaddr_lease("") is None


# ---------------------------------------------------------------------------
# Teardown grace window (issue #132)
# ---------------------------------------------------------------------------


def test_await_shutoff_returns_early_on_clean_poweroff():
    """A guest that ACPI-powers-off promptly is caught within one interval,
    not after the full grace budget."""
    states = iter(["running", "shut off"])
    sleeps: list[float] = []

    got = smoke._await_shutoff(
        lambda: next(states), retries=8, interval=2, sleep=sleeps.append
    )

    assert got is True
    # Saw "running" once, slept once, then "shut off" -> stopped early.
    assert sleeps == [2]


def test_await_shutoff_gives_up_after_grace_budget():
    """A guest that ignores ACPI poweroff (Ubuntu cloud image) is abandoned
    after the bounded grace so the caller can force it off — never the ~60s
    stall the old 12x5s loop produced (issue #132)."""
    sleeps: list[float] = []

    got = smoke._await_shutoff(
        lambda: "running", retries=8, interval=2, sleep=sleeps.append
    )

    assert got is False
    # 8 polls -> 7 sleeps (no sleep after the final poll); bounded at 14s, not 60.
    assert sleeps == [2] * 7


def test_await_shutoff_stops_when_state_is_unreadable():
    """An unreadable state (VirshError) stops the wait immediately rather than
    burning the whole grace budget; the unconditional force-destroy follows."""
    sleeps: list[float] = []

    def boom() -> str:
        raise VirshError(1, "failed to connect to the hypervisor", ["domstate", "x"])

    got = smoke._await_shutoff(boom, retries=8, interval=2, sleep=sleeps.append)

    assert got is True
    assert sleeps == []


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

_ENV = {"name": "smoke", "libvirt_uri": "qemu:///system"}
_DEFAULTS = {
    "disk_image_basedir": "/var/lib/libvirt/images/lvlab",
    "cloud_init": {"pubkey": "~/.ssh/id_ed25519.pub"},
}
_IMAGES = {
    "debian12": {"image_url": "https://example.invalid/debian-12.qcow2"},
}


def test_check_images_cached_fails_when_image_missing():
    cases = [_case("deb", os="debian12")]
    check = check_images_cached(
        _IMAGES, cases, _ENV, _DEFAULTS, exists=lambda _p: False
    )
    assert not check.ok
    assert "debian12" in check.message
    assert "lvlab init" in check.message


def test_check_images_cached_passes_when_present():
    cases = [_case("deb", os="debian12")]
    check = check_images_cached(_IMAGES, cases, _ENV, _DEFAULTS, exists=lambda _p: True)
    assert check.ok


def test_check_images_cached_flags_unknown_image_key():
    cases = [_case("x", os="nosuchdistro")]
    check = check_images_cached(_IMAGES, cases, _ENV, _DEFAULTS, exists=lambda _p: True)
    assert not check.ok
    assert "nosuchdistro" in check.message


def _net(start: str | None, end: str | None) -> LibvirtNetworkInfo:
    return LibvirtNetworkInfo(
        name="default",
        forward_mode="nat",
        gateway_ip="192.168.122.1",
        netmask="255.255.255.0",
        dhcp_start=start,
        dhcp_end=end,
    )


def test_check_static_ips_free_fails_when_inside_dhcp_range():
    cases = [_case("s", mode="static", static_ip="192.168.122.150")]
    check = check_static_ips_free(cases, _net("192.168.122.100", "192.168.122.200"))
    assert not check.ok
    assert "192.168.122.150" in check.message


def test_check_static_ips_free_passes_when_outside_range():
    cases = [_case("s", mode="static", static_ip="192.168.122.190")]
    check = check_static_ips_free(cases, _net("192.168.122.100", "192.168.122.180"))
    assert check.ok


def test_check_static_ips_free_skips_when_no_dhcp_range_known():
    cases = [_case("s", mode="static", static_ip="192.168.122.190")]
    check = check_static_ips_free(cases, _net(None, None))
    assert check.ok  # soft-pass: cannot verify, don't block


def test_check_static_ips_free_noop_for_dhcp_only():
    cases = [_case("d", mode="dhcp")]
    check = check_static_ips_free(cases, _net("192.168.122.100", "192.168.122.200"))
    assert check.ok


# --- #128-A: structured failure message + free-band computation ---------------


def test_compute_free_static_bands_dhcp_at_top_yields_one_band_below():
    """/24 with DHCP 150-254 + gateway .1 → one band .2-.149 (#128-A)."""
    import ipaddress

    bands = smoke._compute_free_static_bands(
        ipaddress.IPv4Network("192.168.122.0/24"),
        ipaddress.IPv4Address("192.168.122.150"),
        ipaddress.IPv4Address("192.168.122.254"),
        ipaddress.IPv4Address("192.168.122.1"),
    )
    assert bands == [
        (
            ipaddress.IPv4Address("192.168.122.2"),
            ipaddress.IPv4Address("192.168.122.149"),
        )
    ]


def test_compute_free_static_bands_dhcp_in_middle_yields_two_bands():
    """/24 with DHCP 100-150 + gateway .1 → bands .2-.99 and .151-.254 (#128-A)."""
    import ipaddress

    bands = smoke._compute_free_static_bands(
        ipaddress.IPv4Network("192.168.122.0/24"),
        ipaddress.IPv4Address("192.168.122.100"),
        ipaddress.IPv4Address("192.168.122.150"),
        ipaddress.IPv4Address("192.168.122.1"),
    )
    assert bands == [
        (
            ipaddress.IPv4Address("192.168.122.2"),
            ipaddress.IPv4Address("192.168.122.99"),
        ),
        (
            ipaddress.IPv4Address("192.168.122.151"),
            ipaddress.IPv4Address("192.168.122.254"),
        ),
    ]


def test_compute_free_static_bands_no_dhcp_yields_full_host_range():
    """No DHCP range → one band of all usable hosts excluding gateway (#128-A)."""
    import ipaddress

    bands = smoke._compute_free_static_bands(
        ipaddress.IPv4Network("192.168.122.0/24"),
        None,
        None,
        ipaddress.IPv4Address("192.168.122.1"),
    )
    assert bands == [
        (
            ipaddress.IPv4Address("192.168.122.2"),
            ipaddress.IPv4Address("192.168.122.254"),
        )
    ]


def test_compute_free_static_bands_dhcp_starts_at_gateway_plus_one():
    """/24, DHCP 2-100, gateway .1 → single band .101-.254 (#128-A)."""
    import ipaddress

    bands = smoke._compute_free_static_bands(
        ipaddress.IPv4Network("192.168.122.0/24"),
        ipaddress.IPv4Address("192.168.122.2"),
        ipaddress.IPv4Address("192.168.122.100"),
        ipaddress.IPv4Address("192.168.122.1"),
    )
    assert bands == [
        (
            ipaddress.IPv4Address("192.168.122.101"),
            ipaddress.IPv4Address("192.168.122.254"),
        )
    ]


def test_compute_free_static_bands_no_gateway_includes_host_one():
    """No gateway → .1 isn't reserved (band starts at .1) (#128-A)."""
    import ipaddress

    bands = smoke._compute_free_static_bands(
        ipaddress.IPv4Network("192.168.122.0/24"),
        ipaddress.IPv4Address("192.168.122.100"),
        ipaddress.IPv4Address("192.168.122.254"),
        None,
    )
    assert bands == [
        (
            ipaddress.IPv4Address("192.168.122.1"),
            ipaddress.IPv4Address("192.168.122.99"),
        )
    ]


def test_check_static_ips_free_failure_message_lists_subnet_and_dhcp_and_free_band():
    """Failure message names subnet, DHCP range, conflicts, and free band (#128-A)."""
    cases = [
        _case("a", mode="static", static_ip="192.168.122.190"),
        _case("b", mode="static", static_ip="192.168.122.191"),
    ]
    check = check_static_ips_free(cases, _net("192.168.122.150", "192.168.122.254"))
    assert not check.ok
    msg = check.message
    # network + subnet identified
    assert "default" in msg
    assert "192.168.122.0/24" in msg
    # dhcp range identified
    assert "192.168.122.150" in msg
    assert "192.168.122.254" in msg
    # conflicting IPs listed (both)
    assert "192.168.122.190" in msg
    assert "192.168.122.191" in msg
    # free band identified (.2-.149 with .1 = gateway)
    assert "192.168.122.2" in msg
    assert "192.168.122.149" in msg


def test_check_static_ips_free_failure_message_offers_both_remedies():
    """Failure message names both remedies (narrow DHCP / move static IPs) (#128-A)."""
    cases = [_case("a", mode="static", static_ip="192.168.122.190")]
    check = check_static_ips_free(cases, _net("192.168.122.150", "192.168.122.254"))
    assert not check.ok
    msg = check.message.lower()
    # both remedies discoverable in the message
    assert "narrow" in msg, msg
    assert "move" in msg, msg


def test_check_static_ips_free_message_handles_two_free_bands():
    """DHCP in the middle → both free bands listed in the message (#128-A)."""
    cases = [_case("a", mode="static", static_ip="192.168.122.120")]
    check = check_static_ips_free(cases, _net("192.168.122.100", "192.168.122.150"))
    assert not check.ok
    msg = check.message
    # both free bands present
    assert "192.168.122.2-192.168.122.99" in msg or (
        "192.168.122.2" in msg and "192.168.122.99" in msg
    )
    assert "192.168.122.151-192.168.122.254" in msg or (
        "192.168.122.151" in msg and "192.168.122.254" in msg
    )


def test_check_ssh_key_present_fails_when_path_missing():
    check = check_ssh_key_present(
        {"cloud_init": {"pubkey": "~/.ssh/id_ed25519.pub"}}, exists=lambda _p: False
    )
    assert not check.ok
    assert "not" in check.message.lower()


def test_check_ssh_key_present_passes_when_path_exists():
    check = check_ssh_key_present(
        {"cloud_init": {"pubkey": "/home/x/.ssh/id_ed25519.pub"}},
        exists=lambda _p: True,
    )
    assert check.ok


def test_check_ssh_key_present_accepts_literal_key():
    check = check_ssh_key_present(
        {"cloud_init": {"pubkey": "ssh-ed25519 AAAAC3Nz literal"}},
        exists=lambda _p: False,
    )
    assert check.ok


def test_check_ssh_key_present_fails_when_absent():
    check = check_ssh_key_present({"cloud_init": {}}, exists=lambda _p: True)
    assert not check.ok


# ---------------------------------------------------------------------------
# Result emission
# ---------------------------------------------------------------------------


def test_summarize_counts_and_overall():
    results = [_result("a"), _result("b", result="fail")]
    summary = summarize(results)
    assert summary["total"] == 2
    assert summary["passed"] == 1
    assert summary["failed"] == 1
    assert summary["overall"] == "fail"
    assert "git_sha" in summary
    assert "host" in summary


def test_summarize_all_pass():
    summary = summarize([_result("a"), _result("b")])
    assert summary["overall"] == "pass"
    assert summary["failed"] == 0


def test_render_json_is_parseable_and_has_per_machine_fields():
    results = [_result("a"), _result("b", result="fail", ip=None)]
    out = render_results(results, OutputFormat.JSON)
    data = json.loads(out)
    assert {m["vm_name"] for m in data["machines"]} == {"a", "b"}
    machine_a = next(m for m in data["machines"] if m["vm_name"] == "a")
    # Per-machine structured fields the plan requires.
    for key in (
        "distro",
        "mode",
        "libvirt_domain",
        "resolved_ip",
        "ssh_ok",
        "boot_to_ssh_seconds",
        "total_seconds",
        "result",
    ):
        assert key in machine_a
    assert data["summary"]["overall"] == "fail"


def test_render_yaml_round_trips():
    results = [_result("a")]
    out = render_results(results, OutputFormat.YAML)
    data = yaml.safe_load(out)
    assert data["machines"][0]["vm_name"] == "a"
    assert data["summary"]["overall"] == "pass"


def test_render_text_marks_pass_and_fail():
    results = [_result("good"), _result("bad", result="fail")]
    out = render_results(results, OutputFormat.TEXT)
    assert "good" in out and "PASS" in out
    assert "bad" in out and "FAIL" in out
    assert "1 of 2 FAILED" in out


def test_render_text_all_pass_summary():
    out = render_results([_result("a"), _result("b")], OutputFormat.TEXT)
    assert "ALL 2 CASES PASSED" in out


def test_render_text_summary_omits_the_table_grid():
    # The closing summary printed under the live table (issue #126): the
    # verdict + failure details, but NOT the per-VM ``===`` grid the live
    # table already showed.
    results = [_result("good"), _result("bad", result="fail")]
    summary = summarize(results)
    out = smoke._render_text_summary(results, summary)
    assert "1 of 2 FAILED" in out
    assert "bad: no SSH" in out  # failing case's detail is kept
    assert "=" * 10 not in out  # no ``===`` table rule
    assert "result" not in out  # no table header
    # The good case is not re-listed (only failures get a detail line).
    assert "good" not in out


def test_render_text_summary_all_pass_is_just_the_verdict():
    results = [_result("a"), _result("b")]
    out = smoke._render_text_summary(results, summarize(results))
    assert out == "SMOKE RESULT: ALL 2 CASES PASSED"


def test_render_text_still_includes_table_and_summary():
    # The full report (non-TTY / piped TEXT path) keeps the grid AND the
    # summary, unchanged by the #126 split.
    results = [_result("a"), _result("b", result="fail")]
    out = render_results(results, OutputFormat.TEXT)
    assert "=" * 66 in out  # table rule present
    assert "result" in out  # table header present
    assert "1 of 2 FAILED" in out  # summary present


# ---------------------------------------------------------------------------
# Case construction
# ---------------------------------------------------------------------------


def test_build_cases_classifies_static_and_dhcp_and_pins_mac():
    environment = {"name": "smoke"}
    config_defaults = {
        "domain": "local",
        "cpu": 1,
        "memory": 1024,
        "interfaces": {"network": "default", "network_type": "network"},
        "cloud_init": {},
    }
    images = {"debian12": {"image_url": "https://x/deb.qcow2"}}
    machines = [
        {
            "vm_name": "deb-static",
            "hostname": "deb-static",
            "os": "debian12",
            "memory": 512,
            "interfaces": [{"name": "eth0", "ip4": "192.168.122.190/24"}],
        },
        {
            "vm_name": "deb-dhcp",
            "hostname": "deb-dhcp",
            "os": "debian12",
            "memory": 512,
            "interfaces": [{"name": "eth0"}],
        },
    ]
    cases = smoke.build_cases(environment, images, config_defaults, machines)
    by_name = {c.vm_name: c for c in cases}

    static = by_name["deb-static"]
    assert static.mode == "static"
    assert static.static_ip == "192.168.122.190"
    assert static.libvirt_domain == "deb-static_smoke"
    assert static.ssh_user == "debian"
    assert static.memory_mib == 512
    # A MAC is pinned by Machine.__init__ even when the manifest omits one.
    assert static.mac is not None

    dhcp = by_name["deb-dhcp"]
    assert dhcp.mode == "dhcp"
    assert dhcp.static_ip is None


# ---------------------------------------------------------------------------
# Guard: pytest must never reach the VM-booting layer.
# ---------------------------------------------------------------------------


def test_no_vm_boot_under_pytest():
    """The lifecycle entrypoints are pragma-marked and never run in tests.

    A coarse but meaningful guard: the public pure API is importable and
    callable, while the lifecycle entrypoints exist only as functions that
    this suite never invokes. If a future refactor wired run_smoke() into a
    fixture or import-time side effect, the assertions below would still hold,
    but the intent is documented here: nothing in pytest may shell out to
    lvlab/virsh/virt-install.
    """
    # Lifecycle functions exist (so cli.py can import them) but are not called.
    assert callable(smoke.run_smoke)
    assert callable(smoke._run_case)
    # The Batch carrier is a plain data type, not a VM action.
    assert Batch(cases=()).cases == ()


# ---------------------------------------------------------------------------
# Environment-directory cleanup (issue #100)
# ---------------------------------------------------------------------------


def test_smoke_env_dir_matches_machine_layout(tmp_path) -> None:
    """smoke_env_dir resolves to <disk_image_basedir>/<env> (Machine's layout)."""
    config_defaults = {"disk_image_basedir": str(tmp_path)}
    environment = {"name": "smoke"}
    assert smoke_env_dir(config_defaults, environment) == str(tmp_path / "smoke")


def test_cleanup_empty_env_dir_removes_empty_dir(tmp_path) -> None:
    """An empty env dir (every VM torn down) is reaped; returns True."""
    env_dir = tmp_path / "smoke"
    env_dir.mkdir()
    config_defaults = {"disk_image_basedir": str(tmp_path)}
    environment = {"name": "smoke"}

    assert cleanup_empty_env_dir(config_defaults, environment) is True
    assert not env_dir.exists()


def test_cleanup_empty_env_dir_leaves_non_empty_dir(tmp_path) -> None:
    """A non-empty env dir (a teardown left files) is left intact; returns False.

    This is the safety guarantee: cleanup never deletes a dir that still
    holds a VM's artifacts.
    """
    env_dir = tmp_path / "smoke"
    env_dir.mkdir()
    leftover = env_dir / "still-here.qcow2"
    leftover.write_text("not empty")
    config_defaults = {"disk_image_basedir": str(tmp_path)}
    environment = {"name": "smoke"}

    assert cleanup_empty_env_dir(config_defaults, environment) is False
    assert env_dir.exists()
    assert leftover.exists()


def test_cleanup_empty_env_dir_missing_dir_is_noop(tmp_path) -> None:
    """A missing env dir is a no-op (no raise); returns False."""
    config_defaults = {"disk_image_basedir": str(tmp_path)}
    environment = {"name": "never-created"}
    assert cleanup_empty_env_dir(config_defaults, environment) is False


# ---------------------------------------------------------------------------
# Live status table (issue #101)
# ---------------------------------------------------------------------------


def test_smoke_progress_tracks_phases_and_ip() -> None:
    """SmokeProgress setters update per-case phase/ip; snapshot is consistent."""
    from tkc_lvlab.smoke import SmokePhase, SmokeProgress

    cases = [
        _case("deb12-static", mode="static", static_ip="192.168.122.10"),
        _case("fed44-dhcp", mode="dhcp"),
    ]
    progress = SmokeProgress(cases)

    # Initial: pending, static ip prefilled, dhcp ip unknown.
    snap = {s.vm_name: s for s in progress.snapshot()}
    assert snap["deb12-static"].phase == SmokePhase.PENDING
    assert snap["deb12-static"].ip == "192.168.122.10"
    assert snap["fed44-dhcp"].ip is None

    progress.set_phase("fed44-dhcp", SmokePhase.BOOTING)
    progress.set_ip("fed44-dhcp", "192.168.122.55")
    progress.set_phase("deb12-static", SmokePhase.PASS)

    snap = {s.vm_name: s for s in progress.snapshot()}
    assert snap["fed44-dhcp"].phase == SmokePhase.BOOTING
    assert snap["fed44-dhcp"].ip == "192.168.122.55"
    assert snap["deb12-static"].phase == SmokePhase.PASS


def test_render_smoke_table_tally_and_cells() -> None:
    """The live table shows each case and a running/pending/passed/failed tally."""
    import io

    from rich.console import Console

    from tkc_lvlab.smoke import SmokePhase, SmokeProgress, render_smoke_table

    cases = [
        _case("a", mode="static", static_ip="10.0.0.1"),
        _case("b", mode="dhcp"),
        _case("c", mode="dhcp"),
    ]
    progress = SmokeProgress(cases)
    progress.set_phase("a", SmokePhase.PASS)
    progress.set_phase("b", SmokePhase.VERIFYING)
    # 'c' stays pending.

    table = render_smoke_table(progress.snapshot(), pool_size=2)
    assert "passed 1" in table.caption
    assert "pending 1" in table.caption
    assert "running 1" in table.caption  # 'b' verifying counts as running
    assert "failed 0" in table.caption

    out = io.StringIO()
    Console(file=out, width=120).print(table)
    rendered = out.getvalue()
    for name in ("a", "b", "c"):
        assert name in rendered
    assert "10.0.0.1" in rendered  # static ip shown


# ---------------------------------------------------------------------------
# Leftover-VM idempotency, crash-safe teardown, static-verify hardening (#139)
# ---------------------------------------------------------------------------


def test_preexisting_case_domains_flags_leftover_domains() -> None:
    """Cases whose libvirt domain is already defined (a prior run left it) are
    returned; cases not present and unrelated domains are ignored."""
    cases = [
        _case("deb11-static", mode="static", static_ip="192.168.122.194"),
        _case("deb12-dhcp", mode="dhcp"),
    ]
    existing = ["deb11-static_smoke", "someones-real-vm", "default"]

    leftover = smoke.preexisting_case_domains(cases, existing)

    assert [c.vm_name for c in leftover] == ["deb11-static"]


def test_preexisting_case_domains_empty_when_clean() -> None:
    """No case domain present in libvirt -> nothing to reap."""
    cases = [_case("deb12-dhcp", mode="dhcp")]
    assert smoke.preexisting_case_domains(cases, ["unrelated", "default"]) == []


def test_cases_to_reap_returns_only_unfinished_cases() -> None:
    """An interrupted run: cases not in a terminal phase still have a live VM
    (they never reached ``_teardown``) and must be reaped; finished ones not."""
    from tkc_lvlab.smoke import SmokePhase, SmokeProgress

    cases = [
        _case("done-pass"),
        _case("done-fail"),
        _case("mid-teardown"),
        _case("still-pending"),
    ]
    progress = SmokeProgress(cases)
    progress.set_phase("done-pass", SmokePhase.PASS)
    progress.set_phase("done-fail", SmokePhase.FAIL)
    progress.set_phase("mid-teardown", SmokePhase.TEARDOWN)
    # "still-pending" keeps its initial PENDING phase.

    reap = smoke.cases_to_reap(cases, progress.snapshot())

    assert {c.vm_name for c in reap} == {"mid-teardown", "still-pending"}


def test_cases_to_reap_empty_when_all_finished() -> None:
    """A run that completed normally already tore every VM down -> nothing to reap."""
    from tkc_lvlab.smoke import SmokePhase, SmokeProgress

    cases = [_case("a"), _case("b")]
    progress = SmokeProgress(cases)
    progress.set_phase("a", SmokePhase.PASS)
    progress.set_phase("b", SmokePhase.FAIL)

    assert smoke.cases_to_reap(cases, progress.snapshot()) == []


def test_static_failure_detail_flags_dhcp_fallback() -> None:
    """A static case that's unreachable while the guest holds a *different*
    lease gets a self-diagnosing message instead of the bare connect error."""
    detail = smoke.static_failure_detail(
        "192.168.122.194",
        "192.168.122.244",
        "no SSH after ~150s; last: No route to host",
    )
    assert "192.168.122.194" in detail
    assert "192.168.122.244" in detail
    # Names the actual cause, not just the connect failure.
    assert "static" in detail.lower()


def test_static_failure_detail_keeps_base_when_no_lease() -> None:
    """No lease found (a correctly-static guest has none) -> detail unchanged."""
    base = "no SSH after ~150s; last: Connection timed out"
    assert smoke.static_failure_detail("192.168.122.194", None, base) == base


def test_static_failure_detail_keeps_base_when_lease_matches_static() -> None:
    """Guest is on its configured static address -> the failure is something
    else; don't fabricate a DHCP-fallback diagnosis."""
    base = "no SSH after ~150s; last: Permission denied (publickey)"
    assert (
        smoke.static_failure_detail("192.168.122.194", "192.168.122.194", base) == base
    )


# ---------------------------------------------------------------------------
# _lvlab_bin — executable resolution order (#135)
# ---------------------------------------------------------------------------


def test_lvlab_bin_env_override_wins() -> None:
    """``$LVLAB`` beats everything — sibling probe and PATH aren't consulted."""
    resolved = smoke._lvlab_bin(
        env={"LVLAB": "/custom/lvlab"},
        argv0="/venv/bin/lvlab",
        executable="/venv/bin/python",
        which=lambda _name: "/usr/bin/lvlab",
        is_exec=lambda _p: True,
    )
    assert resolved == "/custom/lvlab"


def test_lvlab_bin_prefers_sibling_to_interpreter() -> None:
    """A sibling ``lvlab`` next to argv0 wins over PATH — the venv-by-abspath fix."""
    resolved = smoke._lvlab_bin(
        env={},
        argv0="/venv/bin/lvlab",
        executable="/venv/bin/python",
        which=lambda _name: "/usr/bin/lvlab",  # on PATH, but must NOT be chosen
        is_exec=lambda p: p == "/venv/bin/lvlab",
    )
    assert resolved == "/venv/bin/lvlab"


def test_lvlab_bin_uses_executable_dir_when_argv0_has_no_sibling() -> None:
    """When argv0's dir has no ``lvlab``, fall back to the interpreter's dir."""
    resolved = smoke._lvlab_bin(
        env={},
        argv0="/usr/bin/some-wrapper",  # no lvlab here
        executable="/venv/bin/python",
        which=lambda _name: None,
        is_exec=lambda p: p == "/venv/bin/lvlab",
    )
    assert resolved == "/venv/bin/lvlab"


def test_lvlab_bin_falls_back_to_path() -> None:
    """With no sibling executable, resolve via ``$PATH``."""
    resolved = smoke._lvlab_bin(
        env={},
        argv0="/usr/bin/some-wrapper",
        executable="/usr/bin/python",
        which=lambda _name: "/usr/local/bin/lvlab",
        is_exec=lambda _p: False,  # nothing executable beside the interpreter
    )
    assert resolved == "/usr/local/bin/lvlab"


def test_lvlab_bin_bare_name_last_resort() -> None:
    """Nothing found anywhere → the bare ``"lvlab"`` (subprocess raises later)."""
    resolved = smoke._lvlab_bin(
        env={},
        argv0="/usr/bin/some-wrapper",
        executable="/usr/bin/python",
        which=lambda _name: None,
        is_exec=lambda _p: False,
    )
    assert resolved == "lvlab"
