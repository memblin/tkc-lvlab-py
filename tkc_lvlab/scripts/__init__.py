"""Standalone console scripts that ship alongside ``lvlab`` in the same wheel.

Each module in this package exposes a ``run`` callable wired up via
``[project.scripts]`` in ``pyproject.toml``. The scripts share the
library surface under :mod:`tkc_lvlab.utils` but do NOT read
``Lvlab.yml`` and do NOT touch lvlab-managed VMs — see the Phase 6
architecture lock in ``TODO.md``.
"""
