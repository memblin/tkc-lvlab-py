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
