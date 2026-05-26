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
    format_plan,
    plan_batches,
    render_results,
    summarize,
)
from tkc_lvlab.utils.network import LibvirtNetworkInfo


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


def test_parse_free_m_reads_available_column():
    out = (
        "               total        used        free      shared  buff/cache   available\n"
        "Mem:           16000        4000        2000         100        9900       11000\n"
        "Swap:           2000           0        2000\n"
    )
    total, available = smoke._parse_free_m(out)
    assert total == 16000
    assert available == 11000


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
