"""Standalone console scripts that ship alongside ``lvlab`` in the same wheel.

Each module in this package exposes a ``run`` callable wired up via
``[project.scripts]`` in ``pyproject.toml`` and shares the library surface
under :mod:`tkc_lvlab.utils`.

The scripts operate on **raw libvirt domain names** (the name you pass is
the domain name, no prefixing). ``createvm`` reads the ``images:`` section
of an ``Lvlab.yml`` in the current directory, if present, and merges it
over its built-in catalog to resolve ``--distro``; it shares the cloud-image
cache (``/var/lib/libvirt/images/lvlab/cloud-images``) with ``lvlab up``.
``destroyvm`` does not read ``Lvlab.yml`` for name resolution, but since it
acts on raw names it can remove a manifest VM if you pass that VM's actual
``<vm_name>_<env_name>`` domain name.
"""
