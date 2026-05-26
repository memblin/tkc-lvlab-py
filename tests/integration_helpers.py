"""Shared helpers for the integration test bodies.

Module-private to the test suite. Pytest does not collect anything from
this file because its filename does not match the ``test_*.py``
discovery pattern; the helpers are imported explicitly by the
``test_integration_*.py`` modules.

Helpers here are deliberately thin wrappers around ``subprocess.run``
and ``virsh`` — no fixtures, no pytest marks. Fixtures live in
``tests/conftest.py``; this module exists so the same subprocess +
polling shapes don't need to be duplicated across every integration
test file.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from textwrap import dedent

import pytest


VIRSH_TIMEOUT_SECONDS = 30
"""Default timeout for short virsh probes used in test helpers."""

DOMAIN_GONE_POLL_SECONDS = 0.5
"""Seconds between successive ``virsh list`` polls in :func:`wait_for_no_domain`."""

DOMAIN_GONE_TIMEOUT_SECONDS = 20
"""Total seconds :func:`wait_for_no_domain` will wait before raising."""


MANIFEST_TEMPLATE = dedent(
    """\
    ---
    environment:
      - name: {env_name}
        libvirt_uri: {uri}
        config_defaults:
          domain: local
          os: debian13
          cpu: 1
          memory: 1024
          disks:
            - name: primary
              size: 5G
          interfaces:
            network: default
            network_type: {network_type}
          cloud_image_basedir: /var/lib/libvirt/images
          disk_image_basedir: {storage_root}
          cloud_init:
            user: root
            pubkey: {pubkey_path}
            sudo:
              - ALL=(ALL) NOPASSWD:ALL
            shell: /bin/bash
        machines:
          - vm_name: {vm_name}
            hostname: testhost
            interfaces:
              - name: eth0

    images:
      debian13:
        image_url: https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2
        checksum_url: https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS
        checksum_type: sha512
        network_version: 2
    """
)
"""Minimal single-machine ``Lvlab.yml`` template for integration tests.

Placeholders: ``env_name``, ``uri``, ``storage_root``, ``vm_name``,
``pubkey_path``, ``network_type``. Tests render this with
:func:`render_manifest`. The template declares exactly one machine;
tests that need a different shape (e.g. multiple machines) should not
extend this helper — copy and modify in the test file instead.
"""


def network_type_for_uri(uri: str) -> str:
    """Return the manifest ``interfaces.network_type`` appropriate for ``uri``.

    Phase 12 invariant: ``qemu:///session`` uses user-mode networking
    (no libvirt network needed); ``qemu:///system`` uses the managed
    libvirt ``default`` network. Tests pick the value via the URI tag
    so the same test body runs on both URIs.

    Args:
        uri: libvirt URI string (typically from the ``integration_uri``
            fixture).

    Returns:
        ``"user"`` when the URI contains ``"session"``; ``"network"``
        otherwise.
    """
    return "user" if "session" in uri else "network"


def render_manifest(
    *,
    env_name: str,
    uri: str,
    storage_root: Path,
    vm_name: str,
    pubkey_path: Path,
    network_type: str | None = None,
) -> str:
    """Render the integration-test manifest YAML with per-run values filled in.

    Args:
        env_name: Prefixed environment name (lvlab uses it as the
            domain-name suffix and as a storage-path component).
        uri: libvirt URI selected by the ``integration_uri`` fixture.
        storage_root: ``disk_image_basedir`` — the test storage root
            exposed by ``lvlab_integration_storage_root``.
        vm_name: Prefixed VM name (becomes the domain-name prefix and
            the per-VM storage subdir).
        pubkey_path: Absolute path to an existing SSH public key on
            the test host.
        network_type: ``interfaces.network_type`` value. When ``None``
            (default) the value is derived from ``uri`` via
            :func:`network_type_for_uri` — session URIs get user-mode,
            system URIs get the managed libvirt network. Pass an
            explicit value to override.

    Returns:
        A complete ``Lvlab.yml`` YAML document declaring one machine.
    """
    return MANIFEST_TEMPLATE.format(
        env_name=env_name,
        uri=uri,
        storage_root=storage_root,
        vm_name=vm_name,
        pubkey_path=pubkey_path,
        network_type=network_type or network_type_for_uri(uri),
    )


def run_virsh(uri: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``virsh -c <uri> <args>`` with locale-stable env, returning the result.

    Args:
        uri: libvirt URI to operate against.
        *args: Remaining ``virsh`` arguments.

    Returns:
        The :class:`subprocess.CompletedProcess` — caller decides whether
        non-zero exit is fatal.
    """
    return subprocess.run(
        ["virsh", "-c", uri, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=VIRSH_TIMEOUT_SECONDS,
        env={**os.environ, "LC_ALL": "C"},
    )


def list_domains(uri: str) -> list[str]:
    """Return the list of every defined libvirt domain name on ``uri``.

    Args:
        uri: libvirt URI to query.

    Returns:
        Domain names (running and stopped). Empty list if the listing
        command failed; the caller should treat that as a test
        environment problem.
    """
    result = run_virsh(uri, "list", "--all", "--name")
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def wait_for_no_domain(uri: str, domain_name: str) -> None:
    """Poll ``virsh list --all --name`` until ``domain_name`` is absent.

    Args:
        uri: libvirt URI to query.
        domain_name: The fully-qualified domain name to wait for.

    Raises:
        AssertionError: ``domain_name`` is still present after
            :data:`DOMAIN_GONE_TIMEOUT_SECONDS`.
    """
    deadline = time.monotonic() + DOMAIN_GONE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if domain_name not in list_domains(uri):
            return
        time.sleep(DOMAIN_GONE_POLL_SECONDS)
    raise AssertionError(
        f"Domain {domain_name!r} still present on {uri} after "
        f"{DOMAIN_GONE_TIMEOUT_SECONDS}s — destroy did not converge"
    )


def find_entry_point(name: str) -> str:
    """Resolve a console-script entry point's absolute path.

    ``uv run pytest`` puts ``.venv/bin`` on PATH, so the
    ``lvlab`` / ``createvm`` / ``deletevm`` scripts installed by
    ``uv sync`` resolve via ``shutil.which``. Fall back to
    :func:`pytest.fail` (not :func:`pytest.skip`) if the entry point is
    missing — that's a broken test environment, not a runtime skip
    condition.

    Args:
        name: Console-script name (``"lvlab"``, ``"createvm"``, or
            ``"deletevm"``).

    Returns:
        Absolute path to the executable.
    """
    found = shutil.which(name)
    if found is None:
        pytest.fail(
            f"console script {name!r} not found on PATH — run "
            f"'uv sync' before invoking integration tests"
        )
    return found


# ---------------------------------------------------------------------------
# IP-address resolution (DHCP lease + static pick) and SSH connectivity
# ---------------------------------------------------------------------------

DHCP_LEASE_POLL_SECONDS = 2.0
"""Seconds between successive ``domifaddr`` polls in :func:`wait_for_dhcp_lease`."""

DHCP_LEASE_TIMEOUT_SECONDS = 120
"""Total seconds :func:`wait_for_dhcp_lease` waits for a NAT lease to appear."""

SSH_POLL_SECONDS = 3.0
"""Seconds between successive SSH attempts in :func:`wait_for_ssh`."""

SSH_READY_TIMEOUT_SECONDS = 240
"""Total seconds :func:`wait_for_ssh` waits for first-boot cloud-init + sshd."""


def network_ipv4_info(uri: str, network: str = "default") -> tuple[str, str, str, str]:
    """Return ``(gateway, netmask, dhcp_start, dhcp_end)`` for a libvirt network.

    Parses ``virsh net-dumpxml <network>`` rather than assuming the stock
    ``192.168.122.0/24`` so the static-IP pick adapts to whatever the host's
    ``default`` network actually is.

    Args:
        uri: libvirt URI (the createvm path is ``qemu:///system`` only).
        network: Network name to inspect.

    Returns:
        ``(gateway, netmask, dhcp_start, dhcp_end)``. ``dhcp_start`` /
        ``dhcp_end`` are empty strings when the network defines no DHCP
        range.

    Raises:
        AssertionError: ``net-dumpxml`` failed or declared no IPv4 block.
    """
    result = run_virsh(uri, "net-dumpxml", network)
    assert (
        result.returncode == 0
    ), f"net-dumpxml {network} failed on {uri}:\n{result.stderr}"
    root = ET.fromstring(result.stdout)
    for ip_el in root.findall("ip"):
        # The IPv4 block has either no family attr or family='ipv4'.
        if ip_el.get("family", "ipv4") != "ipv4":
            continue
        gateway = ip_el.get("address", "")
        netmask = ip_el.get("netmask", "")
        if not gateway or not netmask:
            continue
        dhcp = ip_el.find("dhcp")
        rng = dhcp.find("range") if dhcp is not None else None
        start = rng.get("start", "") if rng is not None else ""
        end = rng.get("end", "") if rng is not None else ""
        return gateway, netmask, start, end
    raise AssertionError(f"no IPv4 block in net-dumpxml for {network} on {uri}")


def pick_static_ip(uri: str, network: str = "default") -> tuple[str, str] | None:
    """Pick a static IPv4 + netmask **outside** the network's DHCP range.

    ``createvm`` refuses a ``--ip4`` that falls inside the network's DHCP
    range (it would risk a lease collision), so a valid static address must
    sit between the gateway and the pool, or above the pool. This returns
    the first such address, or ``None`` when the pool leaves no room.

    The stock libvirt ``default`` network has a DHCP range spanning the
    whole usable subnet (``.2``-``.254``), so on an unmodified host there is
    no valid static address and this returns ``None`` — the caller should
    skip rather than fail. To exercise static addressing, narrow the
    network's DHCP range (e.g. ``.2``-``.199``, freeing ``.200``-``.254``)
    or use a dedicated test network.

    Tests run serially and tear each VM down before the next, so every
    static-mode case can safely reuse the same returned address — only one
    test VM is ever live at a time.

    Args:
        uri: libvirt URI.
        network: Network name to derive the subnet + DHCP range from.

    Returns:
        ``(ip, prefix_length)``, where ``ip`` is a dotted-quad string and
        ``prefix_length`` is the CIDR prefix as a string (e.g. ``"24"``).
        ``None`` when no address exists outside the DHCP range.

        The prefix — not a dotted-quad netmask — is what ``createvm``'s
        ``--netmask`` option expects (its default is ``"24"``). ``createvm``
        appends it verbatim as ``f"{ip}/{netmask}"``; a dotted-quad here
        would render ``192.168.122.x/255.255.255.0`` into the netplan
        ``addresses`` list, which netplan silently rejects, leaving the
        guest with no address.
    """
    gateway, netmask, start, end = network_ipv4_info(uri, network)
    subnet = ipaddress.IPv4Network(f"{gateway}/{netmask}", strict=False)
    prefix = str(subnet.prefixlen)
    gw = ipaddress.IPv4Address(gateway)
    reserved = {subnet.network_address, subnet.broadcast_address, gw}

    if start and end:
        pool_lo, pool_hi = ipaddress.IPv4Address(start), ipaddress.IPv4Address(end)
        # Prefer just above the pool, then just below it (above the gateway).
        for candidate in (pool_hi + 1, pool_lo - 1):
            if (
                candidate not in reserved
                and candidate in subnet.hosts()
                and not (pool_lo <= candidate <= pool_hi)
            ):
                return str(candidate), prefix
        return None

    # No DHCP range declared: any host address is fine; pick a high one.
    candidate = subnet.broadcast_address - 1
    return (str(candidate), prefix) if candidate not in reserved else None


# ---------------------------------------------------------------------------
# Transient DHCP-range narrowing (opt-in; see conftest.py and CLAUDE.md)
# ---------------------------------------------------------------------------

# How many high host addresses the narrowed range frees for static-IP tests.
# pick_static_ip only needs ONE address above the pool, but freeing a small
# block (so the high end lands on a round number like .199, freeing
# .200-.254) keeps the narrowed range human-legible in net-dumpxml and
# leaves headroom for future multi-static cases.
NARROW_FREE_HIGH_ADDRESSES = 55
"""Default count of top-of-subnet host addresses freed by :func:`compute_narrowed_range`."""


def compute_narrowed_range(
    gateway: str,
    netmask: str,
    start: str,
    end: str,
    free_high: int = NARROW_FREE_HIGH_ADDRESSES,
) -> tuple[str, str] | None:
    """Compute a narrowed DHCP range that frees high addresses for static IPs.

    Pure function (no libvirt): given a network's current ``(start, end)``
    DHCP range, return a narrowed ``(start, new_end)`` whose high end is
    pulled down by ``free_high`` addresses so :func:`pick_static_ip` finds
    a usable address just above the pool. ``start`` is left untouched so
    existing low-address leases keep working.

    Returns ``None`` (no narrowing needed/possible) when:

    - The network declares no DHCP range (``start`` or ``end`` empty) —
      :func:`pick_static_ip` already finds a high address with no pool.
    - :func:`pick_static_ip`'s logic would already succeed (the pool does
      not reach the top of the usable subnet), so narrowing is unnecessary.
    - Narrowing by ``free_high`` would leave the pool empty or invert it
      (``new_end < start``) — the range is too small to narrow safely, so
      the caller must skip rather than narrow.

    Args:
        gateway: The network's IPv4 gateway address (from net-dumpxml).
        netmask: The network's dotted-quad netmask.
        start: Current DHCP range start (dotted-quad).
        end: Current DHCP range end (dotted-quad).
        free_high: How many top-of-subnet host addresses to free. The new
            end is ``end - free_high``.

    Returns:
        ``(start, new_end)`` dotted-quad tuple to install, or ``None`` when
        no narrowing is needed or it cannot be done safely.
    """
    if not start or not end:
        return None

    subnet = ipaddress.IPv4Network(f"{gateway}/{netmask}", strict=False)
    pool_lo = ipaddress.IPv4Address(start)
    pool_hi = ipaddress.IPv4Address(end)
    last_host = subnet.broadcast_address - 1  # highest usable host address

    # If the pool already stops short of the top usable host, pick_static_ip
    # can place an address above it without any change — don't narrow.
    if pool_hi < last_host:
        return None

    new_hi = pool_hi - free_high
    # Refuse to narrow into an empty or inverted range, or below the subnet.
    if new_hi < pool_lo or new_hi <= subnet.network_address:
        return None

    return start, str(new_hi)


def _dhcp_range_xml(start: str, end: str) -> str:
    """Render the ``<range>`` element net-update expects for ``ip-dhcp-range``.

    The XML is emitted as a literal element string (leading ``<``) so
    ``virsh net-update`` treats it as inline XML rather than a filename.
    Single-quoted attributes match libvirt's own serialization style;
    libvirt's net-update matcher keys on the ``start``/``end`` attribute
    values for the ``delete`` command, so the values must match the
    captured range exactly.

    Args:
        start: DHCP range start (dotted-quad).
        end: DHCP range end (dotted-quad).

    Returns:
        e.g. ``"<range start='192.168.122.2' end='192.168.122.254'/>"``.
    """
    return f"<range start='{start}' end='{end}'/>"


def set_dhcp_range_live(
    uri: str,
    network: str,
    old_start: str,
    old_end: str,
    new_start: str,
    new_end: str,
) -> None:
    """Replace a network's DHCP range LIVE (never ``--config``).

    Deletes the ``ip-dhcp-range`` matching ``(old_start, old_end)`` and
    adds ``(new_start, new_end)``, both with ``--live`` only so the
    persistent network definition is untouched. A ``--live`` range change
    does not require ``net-destroy``, so existing leases and connectivity
    for unrelated VMs on the network are preserved.

    Crash recovery: because only the live state is changed, a crash between
    delete and add (or between narrow and restore) is undone by
    ``virsh net-destroy <network> && virsh net-start <network>`` (or a host
    reboot), which reloads the untouched persistent definition.

    Args:
        uri: libvirt URI (system URI for the createvm path).
        network: Network name (``"default"`` for the createvm path).
        old_start: Current range start to delete (must match exactly).
        old_end: Current range end to delete (must match exactly).
        new_start: Range start to add.
        new_end: Range end to add.

    Raises:
        AssertionError: Either net-update step exited non-zero.
    """
    delete = run_virsh(
        uri,
        "net-update",
        network,
        "delete",
        "ip-dhcp-range",
        _dhcp_range_xml(old_start, old_end),
        "--live",
    )
    assert delete.returncode == 0, (
        f"net-update delete ip-dhcp-range failed on {network}@{uri} "
        f"(range {old_start}-{old_end}):\n{delete.stderr}"
    )
    add = run_virsh(
        uri,
        "net-update",
        network,
        "add-last",
        "ip-dhcp-range",
        _dhcp_range_xml(new_start, new_end),
        "--live",
    )
    if add.returncode != 0:
        # Best-effort: re-add the original so a failed narrow leaves the
        # network as we found it before surfacing the error.
        run_virsh(
            uri,
            "net-update",
            network,
            "add-last",
            "ip-dhcp-range",
            _dhcp_range_xml(old_start, old_end),
            "--live",
        )
        raise AssertionError(
            f"net-update add-last ip-dhcp-range failed on {network}@{uri} "
            f"(range {new_start}-{new_end}); restored original:\n{add.stderr}"
        )


def domain_lease_ipv4(uri: str, domain: str) -> str | None:
    """Return the NAT DHCP-leased IPv4 for ``domain``, or ``None`` if none yet.

    Reads ``virsh domifaddr <domain> --source lease`` — the same lease
    table createvm waits on. Returns the bare address (the ``/prefix`` is
    stripped).

    Args:
        uri: libvirt URI.
        domain: Exact libvirt domain name.

    Returns:
        The dotted-quad IPv4 string, or ``None`` when no lease is present
        yet or the lookup failed.
    """
    result = run_virsh(uri, "domifaddr", domain, "--source", "lease")
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        fields = line.split()
        # Row shape: <iface> <mac> ipv4 <addr>/<prefix>
        if "ipv4" in fields:
            addr = fields[fields.index("ipv4") + 1]
            return addr.split("/", 1)[0]
    return None


def wait_for_dhcp_lease(uri: str, domain: str) -> str:
    """Poll until ``domain`` has a NAT DHCP lease; return its IPv4.

    Args:
        uri: libvirt URI.
        domain: Exact libvirt domain name.

    Returns:
        The leased IPv4 address.

    Raises:
        AssertionError: No lease appeared within
            :data:`DHCP_LEASE_TIMEOUT_SECONDS`.
    """
    deadline = time.monotonic() + DHCP_LEASE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        ip = domain_lease_ipv4(uri, domain)
        if ip:
            return ip
        time.sleep(DHCP_LEASE_POLL_SECONDS)
    raise AssertionError(
        f"domain {domain!r} got no NAT DHCP lease within "
        f"{DHCP_LEASE_TIMEOUT_SECONDS}s on {uri}"
    )


def _ssh_argv(ip: str, user: str, key_path: Path) -> list[str]:
    """Build a non-interactive ``ssh`` argv for a throwaway test keypair.

    Host-key checking is disabled and known-hosts routed to ``/dev/null``
    because test guests are ephemeral and their host keys churn every run.
    """
    return [
        "ssh",
        "-i",
        str(key_path),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "LogLevel=ERROR",
        f"{user}@{ip}",
    ]


def wait_for_ssh(ip: str, user: str, key_path: Path) -> None:
    """Poll until ``user@ip`` accepts the test key, or fail.

    The guest may have a DHCP lease (or a configured static IP) well before
    cloud-init has created the default user and installed the key, so this
    retries rather than assuming sshd is immediately ready.

    Args:
        ip: Target IPv4 address.
        user: Expected cloud-init default user.
        key_path: Private key paired with the seeded public key.

    Raises:
        AssertionError: No successful login within
            :data:`SSH_READY_TIMEOUT_SECONDS`.
    """
    deadline = time.monotonic() + SSH_READY_TIMEOUT_SECONDS
    last = ""
    while time.monotonic() < deadline:
        proc = subprocess.run(
            [*_ssh_argv(ip, user, key_path), "true"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if proc.returncode == 0:
            return
        last = proc.stderr.strip()
        time.sleep(SSH_POLL_SECONDS)
    raise AssertionError(
        f"ssh {user}@{ip} not ready within {SSH_READY_TIMEOUT_SECONDS}s; "
        f"last stderr: {last!r}"
    )


def ssh_run(ip: str, user: str, key_path: Path, command: str) -> str:
    """Run a single command over SSH and return its stripped stdout.

    Args:
        ip: Target IPv4 address.
        user: SSH user.
        key_path: Private key path.
        command: Remote command to execute.

    Returns:
        The command's stdout, stripped.

    Raises:
        AssertionError: The remote command exited non-zero.
    """
    proc = subprocess.run(
        [*_ssh_argv(ip, user, key_path), command],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"ssh {user}@{ip} {command!r} failed (exit {proc.returncode}):\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return proc.stdout.strip()
