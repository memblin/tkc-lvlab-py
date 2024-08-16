# Libvirt Notes

## virt-install

Getting help:

```bash
# Show --network parameter options
virt-install --network=?

# Set extra params
virt-install ... --network bridge=br0,model=virtio,mac=52:54:00:c2:de:ce,address.type=pci,address.domain=0,address.bus=1,address.slot=0,address.function=0 ...
```
