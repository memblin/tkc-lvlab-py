"""Declarative, async CLI-conformance harness for ``tkc-lvlab``.

This package drives the **installed** ``lvlab`` / ``createvm`` / ``deletevm``
binaries (the built artifact) through a declarative :mod:`registry` of
scenarios, schedules them across a cheap lane (no VM) and a memory-budgeted
stateful lane (real VMs), and emits a JSON report plus a human summary that a
GitHub issue write-up projects directly from.

It is a developer/maintainer validation tool — **not** shipped in the wheel.
Like the integration suite, every libvirt domain, qcow2 file, and network it
creates carries the per-run :data:`validate.safety.LVLAB_VALIDATE_PREFIX`, and
teardown only ever touches prefixed resources. See ``scripts/validate/README.md``.
"""
