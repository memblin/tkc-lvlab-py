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