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