# WIP: Work in Progress notes

## How to handle the image directory?

Running without using sudo all the time means being in the libvirt
group and having a directory for cloud-images and VM disk images that
your user has write access to but doesn't cause issues with Libvirt
due to selinux or other access issues.

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
#sudo mkdir --mode 0750 /var/lib/libvirt/images/lvlab/cloud-images
#sudo mkdir --mode 0750 /var/lib/libvirt/images/lvlab/{$environment}
sudo chown -R $user:$group /var/lib/libvirt/images/lvlab

```

- With `/var/lib/libvirt/images/lvlab` writeable we can create the subdirs
  ourselves without root privileges.
- Libvirt will sometimes change the uid/gid on a image file or iso
