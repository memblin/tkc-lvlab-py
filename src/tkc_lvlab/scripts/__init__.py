"""Standalone console scripts that ship alongside ``lvlab`` in the same wheel.

``createvm`` and ``deletevm`` are faithful ports of the ``lvscripts-py``
reference commands — same positional arguments, colored output, options
(``--ip4`` / ``--netmask``, ``--init-cloud-images``, ``--config``,
``--version``), and operations — adapted for lvlab in two ways:

- **Image storage** — cloud images cache under
  ``/var/lib/libvirt/images/lvlab/cloud-images`` (shared with ``lvlab up``)
  and per-VM state lands under ``/var/lib/libvirt/images/lvlab/oneoff/<vm_name>/``.
- **Config source** — ``createvm`` resolves ``VM_DISTRO`` against its
  built-in catalog merged with the ``images:`` section of an ``Lvlab.yml``
  in the current directory (or ``--config``); ``os_variant`` and the
  first-boot username are derived from the image key and overridable.

Both scripts target ``qemu:///system`` and operate on **raw libvirt domain
names** (the name you pass is the domain name, no prefixing). ``deletevm``
looks the domain up by that exact name and removes it, cleaning the one-off
storage directory if present. It does no ``Lvlab.yml`` translation, so a
short manifest name like ``web01`` won't resolve — but a manifest VM's full
``<vm_name>_<env>`` domain name will be removed (its disks, nested
elsewhere, are left behind). ``lvlab destroy`` is the manifest-scoped
counterpart.

Each module exposes a ``run`` callable wired up via ``[project.scripts]`` in
``pyproject.toml`` and shares the library surface under
:mod:`tkc_lvlab.utils`.
"""
