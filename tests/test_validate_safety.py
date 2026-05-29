"""Unit tests for the validation harness's prefix/reaper safety model.

These guard the one invariant that makes the harness safe to run on a host
with real VMs: it only ever names and reaps prefix-owned resources.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from validate import safety


def test_prefix_shape() -> None:
    """The prefix is ``lvlab-validate-<digits>-<hex4>-`` so the reaper can match it."""
    assert safety.LVLAB_VALIDATE_PREFIX.startswith("lvlab-validate-")
    assert safety.LVLAB_VALIDATE_PREFIX.endswith("-")


def test_make_name_carries_prefix() -> None:
    """make_name is the only sanctioned namer; output is reap-matchable."""
    name = safety.make_name("deb13-dhcp")
    assert name.startswith(safety.LVLAB_VALIDATE_PREFIX)
    assert name.endswith("deb13-dhcp")
    assert safety.is_owned(name)


def test_is_owned_rejects_foreign_names() -> None:
    """A developer VM name must never read as harness-owned."""
    assert not safety.is_owned("web01_prod")
    assert not safety.is_owned("lvlab-test-123-")  # the *integration* prefix, not ours
    assert not safety.is_owned("")


def test_assert_owned_raises_on_foreign_name() -> None:
    """The destructive-op guard refuses anything without the prefix."""
    with pytest.raises(AssertionError, match="harness prefix"):
        safety.assert_owned("important_dev_vm")


def test_assert_owned_passes_for_owned_name() -> None:
    """A prefixed name passes the guard silently."""
    safety.assert_owned(safety.make_name("snap-cycle"))


def test_list_prefixed_domains_filters_to_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list_prefixed_domains returns ONLY prefixed names from the full domain list."""
    owned = safety.make_name("alive")
    monkeypatch.setattr(
        safety,
        "virsh_list_all_names",
        lambda uri: ["web01_prod", "another-real-vm", owned, "default-vm"],
    )
    assert safety.list_prefixed_domains("qemu:///system") == [owned]


def test_reap_domain_guards_foreign_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """reap_domain must refuse a non-owned name BEFORE shelling out to virsh."""
    calls: list[list[str]] = []
    monkeypatch.setattr(safety, "run_virsh", lambda uri, args, **kw: calls.append(args))
    with pytest.raises(AssertionError):
        safety.reap_domain("qemu:///system", "web01_prod")
    assert calls == []  # no virsh destroy/undefine attempted


def test_reap_domain_destroys_then_undefines_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An owned domain is destroyed then undefined."""
    name = safety.make_name("doomed")
    verbs: list[str] = []
    monkeypatch.setattr(
        safety, "run_virsh", lambda uri, args, **kw: verbs.append(args[0])
    )
    safety.reap_domain("qemu:///system", name)
    assert verbs[0] == "destroy"
    assert "undefine" in verbs


def test_reap_prefixed_storage_only_removes_owned(tmp_path: Path) -> None:
    """Storage reaping removes prefixed dirs and leaves foreign ones untouched."""
    owned = tmp_path / safety.make_name("disk-dir")
    foreign = tmp_path / "real-developer-vm"
    owned.mkdir()
    foreign.mkdir()

    removed = safety.reap_prefixed_storage(roots=(tmp_path,))

    assert owned in removed
    assert not owned.exists()
    assert foreign.exists()  # never touched
