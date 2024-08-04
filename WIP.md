# WIP: Work in Progress notes

## Generating the cidata.iso

```python
import pycdlib

iso = pycdlib.PyCdlib()
iso.new(interchange_level=3, vol_ident='cidata', joliet=True, rock_ridge='1.09')

iso.add_file(
    'meta-data',
    iso_path='/META_DATA;1',
    rr_name='meta-data',
    joliet_path='/meta-data',
)
iso.add_file(
    'user-data',
    iso_path='/USER_DATA;1',
    rr_name='user-data',
    joliet_path='/user-data',
)
iso.add_file(
    'network-config',
    iso_path='/NETWORK_CONFIG;1',
    rr_name='network-config',
    joliet_path='/network-config',
)

iso.write('cidata.iso')
iso.close()
```

## How to handle the image directory?

```bash
# Do we need to put images under /var/lib/libvirt/images for local testing?
#
# - What about something like ~/.cache/lvlab/cloud-images and 
#   ~/.local/lvlab/<project>/<vm>? That would allow many projects
#   to share the same cloud-images.


# Best options to get writeable in /var/lib/libvirt/images/<lvproject_name>
# without changing base permissions on /var/lib/libvirt/images
#
# Pre-create and chown the directory via sudo before initializing?
sudo mkdir --mode 0750 /var/lib/libvirt/images/lvlab
sudo mkdir --mode 0750 /var/lib/libvirt/images/lvlab/cloud-images
sudo mkdir --mode 0750 /var/lib/libvirt/images/lvlab/{$environment}
sudo chown -R $user:$group /var/lib/libvirt/images/lvlab

```

- Libvirt will sometimes change the uid/gid on a image file or iso

## Cloud-init issues

### Debian 12

Found cloud-config data types: user-data, network-config

1. user-data at /var/lib/cloud/instances/iid-vault/cloud-config.txt:
  Invalid user-data /var/lib/cloud/instances/iid-vault/cloud-config.txt
  Error: Cloud config schema errors: users.0.sudo: ['ALL=(ALL) NOPASSWD:ALL'] is not of type 'boolean', users.0.sudo: ['ALL=(ALL) NOPASSWD:ALL'] is not of type 'string', 'null'


2. network-config at /var/lib/cloud/instances/iid-vault/network-config.json:
Skipping network-config schema validation. No network schema for version: 2
Error: Invalid schema: user-data

