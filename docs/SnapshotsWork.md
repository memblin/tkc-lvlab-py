# Snapshots notes

```bash
lvlab snapshot list
lvlab snapshot delete <all|(vm_name)>
lvlab snapshot create (vm_name) (?description)
```

- Functions:
  - create_snapshot(vm_name, description)
  - delete_snapshot(vm_name, snapshot_name)
  - get_snapshots(vm_name)

- Update
  - Functions that delete VMs to..
    - Ask "Are you sure?"
    - Check for snapshots and list them
    - Nuke snapshots before deletion to avoid errors


```python
import libvirt

uri = "qemu:///system"
conn = libvirt.open(uri)

if not conn:
    raise SystemExit("Failed to open libvirt connection.")

current_vms = [dom.name() for dom in conn.listAllDomains()]

vm_name = "fedora40.local-clone"
snapshot_name = "Base"

if vm_name in current_vms:
    vm = conn.lookupByName(vm_name)

    if vm.hasCurrentSnapshot():
        for snapshot in vm.snapshotListNames():
            print(f"Removing snapshot: {snapshot}")
            snap_handle = vm.snapshotLookupByName(snapshot)
            snap_handle.delete()
    else:
        print(f"No snapshots found for {vm.name()}, creating one.")

        snapshot_xml = f"""
        <domainsnapshot>
            <name>{snapshot_name}</name>
            <description>Snapshot of {vm.name()}</description>
        </domainsnapshot>
        """

        try:
            vm.snapshotCreateXML(snapshot_xml, 0)
            print(f"Snapshot {snapshot_name} created successfully for {vm.name()}.")
        except libvirt.libvirtError:
            raise SystemExit(f"Failed to create snapshot: {snapshot_name}")

conn.close()
```
