#!/usr/bin/env bash
# Manifest-path smoke test: exercises `lvlab up` -> SSH -> `lvlab down`
# -> `lvlab destroy` for every machine in this directory's Lvlab.yml,
# two VMs at a time (one static + one DHCP per distro), verifying SSH
# login as the distro's default user before moving on.
#
# This is a MANUAL validation helper (like scripts/run-validation.sh),
# never wired into CI — it boots real VMs on qemu:///system. Run it on a
# libvirt host whose `default` network DHCP range has been narrowed to
# free the static addresses below (see docs-extra/host-validation.md).
#
#   cd docs-extra/smoke && ./run-smoke.sh
#
# Keep the PAIRS table below in sync with Lvlab.yml when the default
# image set changes (the refresh-cloud-images skill updates the images:
# section; the machine pairs are maintained here + in Lvlab.yml).
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV="smoke"
URI="qemu:///system"
SSH_KEY="${SMOKE_SSH_KEY:-$HOME/.ssh/id_ed25519}"
SSHOPTS=(-i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=no \
         -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8)

# Locate the lvlab binary: prefer an explicit LVLAB, else PATH, else the
# project .venv relative to this file.
LVLAB="${LVLAB:-$(command -v lvlab || true)}"
[ -z "$LVLAB" ] && LVLAB="$HERE/../../.venv/bin/lvlab"
if [ ! -x "$LVLAB" ]; then echo "lvlab not found (set LVLAB=...)"; exit 2; fi

# static_vm  static_ip          dhcp_vm       ssh_user
PAIRS=(
  "deb12-static 192.168.122.190 deb12-dhcp debian"
  "deb13-static 192.168.122.191 deb13-dhcp debian"
  "fed44-static 192.168.122.192 fed44-dhcp fedora"
  "alma10-static 192.168.122.193 alma10-dhcp almalinux"
)

lease_ip() { virsh -c "$URI" domifaddr "$1" --source lease 2>/dev/null \
  | awk '/ipv4/{print $4}' | cut -d/ -f1 | head -1; }

ssh_check() { # label user ip -> 0/1
  local out
  for _ in $(seq 1 30); do
    out=$(ssh "${SSHOPTS[@]}" "$2"@"$3" \
      'echo OK:$(hostname):$(id -un):$(ip -br addr | grep -vw lo | tr "\n" " ")' 2>/dev/null)
    [ -n "$out" ] && { echo "  PASS [$1 @ $3] $out"; return 0; }
    sleep 5
  done
  echo "  FAIL [$1 @ $3] no SSH in ~150s; last: $(ssh "${SSHOPTS[@]}" "$2"@"$3" true 2>&1 | tail -1)"
  return 1
}

cd "$HERE" || exit 2
overall=0
for row in "${PAIRS[@]}"; do
  read -r svm sip dvm user <<<"$row"
  echo "=================================================================="
  echo "PAIR: $svm (static $sip) + $dvm (dhcp)  user=$user"
  echo "=================================================================="
  for vm in "$svm" "$dvm"; do echo "-- up $vm"; "$LVLAB" up "$vm" 2>&1 | tail -2; done

  dip=""
  for _ in $(seq 1 30); do dip="$(lease_ip "${dvm}_${ENV}")"; [ -n "$dip" ] && break; sleep 5; done
  echo "-- dhcp lease $dvm: ${dip:-<none>}"

  ssh_check "$svm" "$user" "$sip" || overall=1
  if [ -n "$dip" ]; then ssh_check "$dvm" "$user" "$dip" || overall=1
  else echo "  FAIL [$dvm] no DHCP lease"; overall=1; fi

  for vm in "$svm" "$dvm"; do "$LVLAB" down "$vm" >/dev/null 2>&1; done
  for _ in $(seq 1 12); do
    a=$(virsh -c "$URI" domstate "${svm}_${ENV}" 2>/dev/null)
    b=$(virsh -c "$URI" domstate "${dvm}_${ENV}" 2>/dev/null)
    [ "$a" = "shut off" ] && [ "$b" = "shut off" ] && break; sleep 5
  done
  echo "-- post-down: $svm=$a | $dvm=$b"
  for vm in "$svm" "$dvm"; do "$LVLAB" destroy "$vm" --force >/dev/null 2>&1; done
  echo "-- destroyed both"
done

echo "=================================================================="
[ "$overall" -eq 0 ] && echo "SMOKE RESULT: ALL PAIRS PASSED" || echo "SMOKE RESULT: FAILURES (see above)"
exit "$overall"
