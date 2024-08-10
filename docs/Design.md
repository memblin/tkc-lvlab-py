# Libvirt Labs - Design

## Origin

I started this as side project of a similar learning project I'm working on
to create a Rust based host agent able control virtual machine deployments
on distributed Libvirt host clusters by leveraging [nats.io](https://nats.io)
as a messaging / data layer.

I then attempted to try to use the Rust library I was creating for that
distributed Rust agent to create a local Rust version of what is now Python
`lvlab`. This was slow going because I'm still very new to Rust.

One day I wondered, "How quickly Ian I get this far in Python?", and set out
one evening to find out. It was fast enough I have parked the Rust version in
favor of ironing out a bunch of the logic here before porting it back to Rust.

## Specifics

There is not a battle of procedural vs. object oriented going on. I started
with a procedural approach getting familiar with the new libraries but then
realized that many of the data sets and operations I was working with could
work much better if I started applying them with objects. So it turned into a
Python learning project on object oriented programming, excuse the mess.

There is still LOTs of shuffling going on. As I add new features and
functionality sometimes past choices need a re-work for both features to be
implemented efficiently and without duplicate code. Still plenty of dupe code
too.

Inconsistent type hints should become consistent over time; still learning
about proper use.

## Goals

- First, and foremost; A functional tool that is safe and easy to use for
  local libvirt+qemu based integration testing of systems management code.

  Think full end-to-end environmental testing of Salt, Ansible, Chef, or
  Puppet environments.
