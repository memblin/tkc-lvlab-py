# TKC Labs : Libvirt Labs - Walkthrough

This document aims to provide information on exactly what you can epxect
`lvlab` to do with each operation.

## capabilities

This queries Libvirt for it's capabilities. It works but is not yet used
for anything in the application.

## cloudinit

This will re-write the cloud-init config files based on the current content
in your Lvlab.yml config file.

This can be useful for debugging cloud-init template rendering issues.

## destroy

Forceibly shutdown and delete the VM

Currently leaves remnants, VM directory and cloud-init files behind.

## down

Attempt graceful shutdown of the VM

## init

Initialize the environment defined in the Lvlab.yml file

The init operation will attempt to download the images you've described in the
configuraiton file into the configured `cloud_image_basedir/cloud-images`
directory.

The init operation will create the `cloud_image_basedir` which can be the same
as the `disk_image_basedir`, and should be the same unless storage constraints
prohibit it. Easier file management if they're all in the same basedir.

It attempts to verify the checksum of the dowloaded `image_url` if a checksum
URL is provided.

It attempts to verify the signature on the chechsum hash file if a
`checksum_url_gpg` parameter is provided. The example in repo shows this
for Fedora, other distributions have not been tested.

These images are then used as the backing image for new virtual machine
boot disks by using qemu-img to create a new vdisk based on the cloud-image.

This cloud-image directory can be shared with other environments so that
there is no need to duplicate images for multiple environments.

It's cleanup is also manual for now.

### Image Naming

To enable the use of custom images or multiple versions of the same OS version
we have introudced a naming standard. (Added v0.2.2)

Images should be named: `$(os_variant)-$(whatever_you_want)`

We split on the hyphen and pass the os_variant to the virt-install command as
the `--os-variant` parameter.

These are valid examples:

- debian12-CustomImage
- debian12-generic-amd64-20240717-1811
- fedora40-idM-v0.1.3

You can list the available options on a specific Libvirt host with:

```bash
# virt-install wrapper for osinfo-query
virt-install --os-variant list

# Or right to osinfo-query
osinfo-query os
```

This value is also parsed to determine which `/etc/cloud/templates/hosts.*`
template should be updated which ensures `/etc/hosts` changes made during
cloud-init will persist.

There are still some shortcomings with custom image checksuming that
will be handled in a future release.

## status

Read the config and query libvirt for VM status for each VM in the
environment. Produces a very rudamentary status output.

## up

Start a virtual machine defined in the Lvlab.yml manifest

This operation checks to see if the virtual machine exists in Libvirt. If
it does it checks the status and starts it up if the VM is in a shutdown state.

If the VM does not exist we ceate the primary vdisk with qemu-img, create the
cloud-init config iso with genisoimage, and then launch the virtual machine with
a virt-install that mounts the cloud-init iso as a cloud-init datasource.
