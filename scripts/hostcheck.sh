#!/usr/bin/env bash
# Per-host release smoke: install a released tkc-lvlab wheel into a fresh venv
# and run `lvlab smoke` for a single guest of the requested distro over
# qemu:///system. Proves the *released artifact* drives the full lab lifecycle
# (init -> up -> DHCP-lease -> SSH-verify -> down -> destroy) on this host OS.
#
# Lighter than scripts/run-validation.sh (which runs the unit + integration
# suites from a checkout) — this exercises the installable wheel end-to-end on
# the host, one guest, and is what we use for a quick per-host release check
# across the supported host matrix (see docs-extra/host-validation.md).
#
# Run as a regular user already in the libvirt group, on a host where libvirt
# is installed and the qemu:///system 'default' network exists.
#
# Usage:
#   scripts/hostcheck.sh <distro-key> [version]
#     distro-key : debian12 | debian13 | almalinux10 | fedora44
#     version    : released tag to install (default: 0.5.0)
set -uo pipefail

DISTRO="${1:?usage: hostcheck.sh <debian12|debian13|almalinux10|fedora44> [version]}"
VERSION="${2:-0.5.0}"
WHEEL_URL="https://github.com/memblin/tkc-lvlab-py/releases/download/${VERSION}/tkc_lvlab-${VERSION}-py3-none-any.whl"
WORK="${HOME}/hostcheck"

# Per-distro image entry + memory floor (mirrors docs-extra/smoke/Lvlab.yml and
# footprints.MEMORY_FLOOR_MIB_BY_FAMILY). network_version 2 for all four here.
case "${DISTRO}" in
  debian12)
    IMG_URL="https://cloud.debian.org/images/cloud/bookworm/20260518-2482/debian-12-generic-amd64-20260518-2482.qcow2"
    CK_URL="https://cloud.debian.org/images/cloud/bookworm/20260518-2482/SHA512SUMS"; CK_TYPE="sha512"; CK_GPG=""; MEM=512 ;;
  debian13)
    IMG_URL="https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2"
    CK_URL="https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS"; CK_TYPE="sha512"; CK_GPG=""; MEM=512 ;;
  almalinux10)
    IMG_URL="https://repo.almalinux.org/almalinux/10/cloud/x86_64/images/AlmaLinux-10-GenericCloud-latest.x86_64.qcow2"
    CK_URL="https://repo.almalinux.org/almalinux/10/cloud/x86_64/images/CHECKSUM"; CK_TYPE="sha256"; CK_GPG=""; MEM=1536 ;;
  fedora44)
    IMG_URL="https://download.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2"
    CK_URL="https://download.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/x86_64/images/Fedora-Cloud-44-1.7-x86_64-CHECKSUM"
    CK_GPG="https://fedoraproject.org/fedora.gpg"; CK_TYPE="sha256"; MEM=2048 ;;
  *) echo "RESULT: UNKNOWN_DISTRO ${DISTRO}"; exit 2 ;;
esac

# shellcheck disable=SC1091  # /etc/os-release is a standard runtime file
echo "=== hostcheck ${DISTRO} (lvlab ${VERSION}) on $(. /etc/os-release; echo "${PRETTY_NAME}") as $(id -un) (groups: $(id -nG)) ==="

if [[ ! -f "${HOME}/.ssh/id_ed25519" ]]; then
    mkdir -p "${HOME}/.ssh"; ssh-keygen -t ed25519 -N '' -f "${HOME}/.ssh/id_ed25519" -q; echo "generated ssh key"
fi

rm -rf "${WORK}"; mkdir -p "${WORK}"; cd "${WORK}" || exit 2

{
  echo "---"
  echo "environment:"
  echo "  - name: hostcheck"
  echo "    libvirt_uri: qemu:///system"
  echo "    config_defaults:"
  echo "      domain: local"
  echo "      cpu: 1"
  echo "      memory: ${MEM}"
  echo "      cloud_image_basedir: /var/lib/libvirt/images/lvlab"
  echo "      disk_image_basedir: /var/lib/libvirt/images/lvlab"
  echo "      disks:"
  echo "        - name: primary"
  echo "          size: 12G"
  echo "      interfaces:"
  echo "        network: default"
  echo "        network_type: network"
  echo "        nameservers:"
  echo "          search: [local]"
  echo "          addresses: [192.168.122.1]"
  echo "      cloud_init:"
  echo "        pubkey: ~/.ssh/id_ed25519.pub"
  echo "        sudo: [\"ALL=(ALL) NOPASSWD:ALL\"]"
  echo "        shell: /bin/bash"
  echo "    machines:"
  echo "      - vm_name: ${DISTRO}check"
  echo "        hostname: ${DISTRO}check"
  echo "        os: ${DISTRO}"
  echo "        interfaces:"
  echo "          - name: eth0"
  echo "images:"
  echo "  ${DISTRO}:"
  echo "    image_url: ${IMG_URL}"
  echo "    checksum_url: ${CK_URL}"
  [[ -n "${CK_GPG}" ]] && echo "    checksum_url_gpg: ${CK_GPG}"
  echo "    checksum_type: ${CK_TYPE}"
  echo "    network_version: 2"
} > Lvlab.yml

python3 -m venv .venv || { echo "RESULT: VENV_FAIL"; exit 1; }
./.venv/bin/pip install --quiet --upgrade pip >/dev/null 2>&1
if ! ./.venv/bin/pip install --quiet "${WHEEL_URL}" >/tmp/hostcheck-pip.log 2>&1; then
    echo "RESULT: PIP_FAIL"; tail -5 /tmp/hostcheck-pip.log; exit 1
fi
echo "installed: $(./.venv/bin/python -c 'import importlib.metadata as m; print(m.version("tkc-lvlab"))')"

# venv on PATH so `lvlab smoke` can re-invoke `lvlab up` (it resolves the
# binary via shutil.which("lvlab") — the normal activated-venv usage).
export PATH="${WORK}/.venv/bin:${PATH}"

sudo virsh -c qemu:///system net-start default >/dev/null 2>&1 || true
sudo virsh -c qemu:///system net-autostart default >/dev/null 2>&1 || true

echo "--- lvlab init (cache ${DISTRO}) ---"
if ! lvlab init; then echo "RESULT: INIT_FAIL"; exit 1; fi

echo "--- lvlab smoke (1 ${DISTRO} guest) ---"
lvlab smoke --yes --reserve 512
rc=$?
echo "RESULT: ${DISTRO} ${VERSION} smoke exit ${rc}"
exit "${rc}"
