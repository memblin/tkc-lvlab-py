"""Standalone console scripts that ship alongside ``lvlab`` in the same wheel.

Each module in this package exposes a ``run`` callable wired up via
``[project.scripts]`` in ``pyproject.toml``. The scripts share the
library surface under :mod:`tkc_lvlab.utils` but do NOT read
``Lvlab.yml`` and do NOT touch lvlab-managed VMs — they're separate
console scripts in the same wheel, intentionally invisible to each
other's lookups. Standalone-script domains are named
``oneoff-<vm_name>``; lvlab manifest domains are named
``<vm_name>_<env_name>``.
"""
