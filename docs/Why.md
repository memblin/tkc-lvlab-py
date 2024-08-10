# TKC Labs : Libvirt Labs - Why?

Heavily opinionated intro...

Libvirt with QEMU-KVM has been my go-to for testing infrastructure automation
when Podman, Docker, or Kubernetes won't fit the bill for various reasons.
However, it can be cumbersome for new users to get familiar with the Libvirt
toolset. Options like Vagrant exist and can often get you testing faster in
MacOS and windows environments.

Vagrant, generally easy to use and multi-platform, is an excellent option.
Most of the projects I see using Vagrant also use the VirtualBox provider. It
works well yet I dislike needing to use Virtualbox if I have a Linux
workstation or Development VM available to work from. This is because for me,
VirtualBox VMs on Linux specifically seem sluggish to me when compared to a
Libvirt QEMU-KVM VMs.

I've tried using Vagrant with the Libvirt provider and it does work but the
projects I've seen that use Vagrant also normally use an off-the-shelf
Vagrant Box that is only available for the VirtualBox provider. That means
building and publishing an identical box of your own for the Libvirt provider
or possibly even building for both providers so the image is exactly the same.
That's an additional difficulty for me using Vagrant with Libvirt. If those
things were easier or not applicable in-project then Vagrant is an awesome
solution.

## Hurdles with Libvirt and the goal of Libvirt Labs

The biggest hurdle; if you're not on Linux, FreeBSD, or MacOS I don't think
Libvirt is an option for you. I've never tried to run it on MacOS, on Mac I
normally go for Vagrant and VirtualBox. Libvirt and QEMU do show as available
via brew, would love to know how well it works.

If it is an option for you and you acquire the foundational knowledge of tools
such as `virsh`, `qemu-img`, and cloud-init configs you might find yourself
with a collection of scripts, snippets, and gists to facilitate automating
the toolkit. Managing that collection can become cumbersome and time consuming
as the collection grows.

This utility aims to create a more functional and efficient approach to using
Libvirt for local testing needs.

## Yeah, but why?

Vagrant networking gets in the way of a few of the host types I currently need
to test with. Due to that I need to be able to test in Libvirt.

Libvirt and VirtualBox can't run VMs at the same time and I often need to test
multiple companion service VMs alongside the VMs that don't test well in VBox.

I found myself with over 20 variations of qemu-img, genisoimage, and cloud-init
templates that I was using to mix and match to create VMs for local testing.
