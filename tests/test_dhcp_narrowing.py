"""Unit tests for the pure DHCP-range-narrowing logic (issue #86).

The opt-in ``narrow_default_dhcp_range`` fixture (``tests/conftest.py``)
transiently shrinks the ``default`` network's DHCP range so the static-IP
integration cases find headroom via ``pick_static_ip``. The libvirt-touching
parts (``set_dhcp_range_live``, the fixture itself) can only be exercised
against a real ``virsh`` + ``qemu:///system`` host, but the range arithmetic
is pure and is where the off-by-one / "don't invert the pool" bugs would
live — so it gets locked in here.

These tests assert the contract that makes the fixture safe:

- A pool that already leaves headroom is NOT narrowed (no needless mutation
  of the operator's network).
- A pool spanning the top of the subnet IS narrowed, freeing high addresses.
- The narrowed range, fed back through ``pick_static_ip``'s arithmetic,
  actually yields a usable static address (the whole point of narrowing).
- A pool too small to narrow safely returns ``None`` rather than producing
  an empty/inverted range.
- The emitted ``<range>`` XML matches libvirt's attribute-keyed
  net-update matcher exactly.
"""

from __future__ import annotations

import ipaddress

from tests.integration_helpers import (
    NARROW_FREE_HIGH_ADDRESSES,
    _dhcp_range_xml,
    compute_narrowed_range,
)


_GATEWAY = "192.168.122.1"
_NETMASK = "255.255.255.0"


def _pick_static_from_range(start: str, end: str) -> str | None:
    """Reimplement ``pick_static_ip``'s pure pick over an explicit range.

    Mirrors the "prefer just above the pool, then just below it" arithmetic
    in :func:`tests.integration_helpers.pick_static_ip` without touching
    libvirt, so a unit test can assert that a narrowed range actually yields
    a usable static address. Returns the dotted-quad pick, or ``None``.
    """
    subnet = ipaddress.IPv4Network(f"{_GATEWAY}/{_NETMASK}", strict=False)
    gw = ipaddress.IPv4Address(_GATEWAY)
    reserved = {subnet.network_address, subnet.broadcast_address, gw}
    pool_lo, pool_hi = ipaddress.IPv4Address(start), ipaddress.IPv4Address(end)
    for candidate in (pool_hi + 1, pool_lo - 1):
        if (
            candidate not in reserved
            and candidate in subnet.hosts()
            and not (pool_lo <= candidate <= pool_hi)
        ):
            return str(candidate)
    return None


def test_full_span_pool_is_narrowed_and_frees_high_addresses() -> None:
    """A pool spanning the top of the subnet is narrowed by ``free_high``."""
    result = compute_narrowed_range(
        _GATEWAY, _NETMASK, "192.168.122.2", "192.168.122.254"
    )
    assert result is not None
    new_start, new_end = result
    # Start is preserved (low-address leases keep working).
    assert new_start == "192.168.122.2"
    # End pulled down by exactly NARROW_FREE_HIGH_ADDRESSES (.254 - 55 = .199).
    assert new_end == "192.168.122.199"
    expected_end = ipaddress.IPv4Address("192.168.122.254") - NARROW_FREE_HIGH_ADDRESSES
    assert new_end == str(expected_end)


def test_narrowed_range_yields_a_usable_static_address() -> None:
    """After narrowing, ``pick_static_ip``'s arithmetic finds an address.

    This is the behavioral point of the whole fixture: narrowing a
    full-span pool must leave room above the pool for a static IP.
    """
    new_start, new_end = compute_narrowed_range(
        _GATEWAY, _NETMASK, "192.168.122.2", "192.168.122.254"
    )
    # Before narrowing, the full-span pool leaves no headroom.
    assert _pick_static_from_range("192.168.122.2", "192.168.122.254") is None
    # After narrowing, an address just above the new pool is available.
    pick = _pick_static_from_range(new_start, new_end)
    assert pick == "192.168.122.200"


def test_pool_with_existing_headroom_is_not_narrowed() -> None:
    """A pool that already stops short of the top is left untouched."""
    # Stock-narrowed host: pool ends at .199, .200-.254 already free.
    result = compute_narrowed_range(
        _GATEWAY, _NETMASK, "192.168.122.2", "192.168.122.199"
    )
    assert result is None


def test_no_dhcp_range_returns_none() -> None:
    """An empty range (no DHCP declared) needs no narrowing."""
    assert compute_narrowed_range(_GATEWAY, _NETMASK, "", "") is None
    assert compute_narrowed_range(_GATEWAY, _NETMASK, "192.168.122.2", "") is None


def test_pool_too_small_to_narrow_returns_none() -> None:
    """Narrowing that would empty or invert the pool returns None, not garbage."""
    # A 3-address pool at the top of the subnet: narrowing by 55 would push
    # the new end below the start. Must refuse rather than invert.
    result = compute_narrowed_range(
        _GATEWAY, _NETMASK, "192.168.122.252", "192.168.122.254"
    )
    assert result is None


def test_narrow_respects_custom_free_high() -> None:
    """``free_high`` controls how many top addresses are freed."""
    new_start, new_end = compute_narrowed_range(
        _GATEWAY, _NETMASK, "192.168.122.2", "192.168.122.254", free_high=4
    )
    assert (new_start, new_end) == ("192.168.122.2", "192.168.122.250")
    assert _pick_static_from_range(new_start, new_end) == "192.168.122.251"


def test_narrow_on_non_122_subnet() -> None:
    """Narrowing adapts to a host whose default network is not 192.168.122/24."""
    result = compute_narrowed_range(
        "10.10.0.1", "255.255.255.0", "10.10.0.2", "10.10.0.254"
    )
    assert result == ("10.10.0.2", "10.10.0.199")


def test_dhcp_range_xml_matches_libvirt_attribute_form() -> None:
    """The emitted ``<range>`` XML is the literal, single-quoted form net-update matches.

    ``virsh net-update ... delete ip-dhcp-range`` keys on the start/end
    attributes; the string must start with ``<`` (so virsh treats it as
    inline XML, not a filename) and carry both attributes verbatim.
    """
    xml = _dhcp_range_xml("192.168.122.2", "192.168.122.254")
    assert xml.startswith("<range ")
    assert "start='192.168.122.2'" in xml
    assert "end='192.168.122.254'" in xml
    assert xml.endswith("/>")
