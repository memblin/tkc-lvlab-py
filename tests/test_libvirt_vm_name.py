"""Unit tests for ``Machine.libvirt_vm_name`` namespacing.

The libvirt domain name is **not** ``vm_name``; it's
``f"{vm_name}_{environment_name}"``. That namespace is what lets multiple
lvlab environments coexist on one hypervisor — two environments each
declaring a ``web01`` machine produce domains ``web01_dev`` and
``web01_prod`` respectively, not a name collision.

These tests construct ``Machine`` through its real ``__init__`` (no
``object.__new__`` short-circuit) to catch regressions in the
construction path itself. Existing tests in ``test_libvirt_machine.py``
set ``libvirt_vm_name`` by hand and exercise downstream methods; this
file complements them by pinning the *construction* contract.
"""

from __future__ import annotations

from typing import Any

from tkc_lvlab.utils.libvirt import Machine


def _minimal_machine_dict(vm_name: str = "web01") -> dict[str, Any]:
    """Return a Machine-compatible dict with just the required keys.

    Args:
        vm_name: The bare VM name. Defaults to ``"web01"``.

    Returns:
        A dict the real ``Machine.__init__`` will accept without exploding.
    """
    return {
        "vm_name": vm_name,
        "hostname": vm_name,
        "interfaces": [],
        "disks": [],
        "shared_directories": [],
        "cloud_init": {"pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIaaa user@host"},
    }


def _minimal_defaults() -> dict[str, Any]:
    """Return a config_defaults dict carrying the keys ``Machine.__init__`` requires."""
    return {
        "interfaces": {"nameservers": {}},
        "shared_directories": [],
    }


def test_libvirt_vm_name_namespaces_with_env_name() -> None:
    """A machine in env ``lab`` named ``web01`` becomes domain ``web01_lab``."""
    machine = Machine(
        _minimal_machine_dict("web01"),
        {"name": "lab"},
        _minimal_defaults(),
    )
    assert machine.libvirt_vm_name == "web01_lab"


def test_libvirt_vm_name_falls_back_to_default_when_env_unnamed() -> None:
    """If ``environment.name`` is missing, ``LvLabEnvironment`` is the fallback.

    Locks the fallback string. Changing the fallback is user-visible —
    a machine that used to be ``web01_LvLabEnvironment`` would suddenly
    be a different libvirt domain, orphaning the old qcow2.
    """
    machine = Machine(
        _minimal_machine_dict("web01"),
        {},
        _minimal_defaults(),
    )
    assert machine.libvirt_vm_name == "web01_LvLabEnvironment"


def test_libvirt_vm_name_distinguishes_environments() -> None:
    """The same ``vm_name`` in two environments produces two distinct domains.

    This is the entire point of the namespacing. If this ever returns
    equal strings, ``lvlab up`` in env A would clobber the running VM
    of the same name in env B.
    """
    m_dev = Machine(
        _minimal_machine_dict("web01"),
        {"name": "dev"},
        _minimal_defaults(),
    )
    m_prod = Machine(
        _minimal_machine_dict("web01"),
        {"name": "prod"},
        _minimal_defaults(),
    )
    assert m_dev.libvirt_vm_name != m_prod.libvirt_vm_name
    assert m_dev.libvirt_vm_name == "web01_dev"
    assert m_prod.libvirt_vm_name == "web01_prod"


def test_libvirt_vm_name_passes_through_dots_in_vm_name() -> None:
    """``vm_name`` with dots (e.g. ``salt.local``) is preserved verbatim.

    The repo's own Lvlab.yml uses dotted VM names; the namespacing is
    a simple concat, not a sanitizer. Locking current behavior so a
    future "let's sanitize this" PR is deliberate.
    """
    machine = Machine(
        _minimal_machine_dict("salt.local"),
        {"name": "lvlab-dev"},
        _minimal_defaults(),
    )
    assert machine.libvirt_vm_name == "salt.local_lvlab-dev"


def test_libvirt_vm_name_uses_os_variant_split_safe_input() -> None:
    """``machine.os`` derivation is independent of ``libvirt_vm_name``.

    Construction works even when ``os`` is set (the ``--os-variant``
    derivation happens later in ``deploy()``, not in ``__init__``).
    """
    md = _minimal_machine_dict("web01")
    md["os"] = "fedora40"
    machine = Machine(md, {"name": "lab"}, _minimal_defaults())
    assert machine.libvirt_vm_name == "web01_lab"
    assert machine.os == "fedora40"
