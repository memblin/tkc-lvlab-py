import os
import subprocess


def create_vdisk(vdisk_fpath, vdisk_size, vdisk_backing_image):
    """Create a virtual disk image with qemu-img"""
    command = [
        'qemu-img', 'create',
        '-b', vdisk_backing_image,
        '-f', 'qcow2',
        '-F', 'qcow2',
        vdisk_fpath,
        vdisk_size
    ]

    if os.path.isfile(vdisk_fpath):
        print(f"vDisk {vdisk_fpath} already exists, May need to clean-up previous deployment.")
        return
    
    if not os.path.exists(os.path.dirname(vdisk_fpath)):
        print(f"vDisk path doesn't exist, attempting to create.")
        try:
            os.mkdirs(os.path.exists(os.path.dirname(vdisk_fpath)))
        except Exception as e:
            print(e)
            return
    else:
        try:
            subprocess.run(command, check=True)
            print("vDisk image created successfully.")
        except subprocess.CalledProcessError as e:
            print(f"An error occurred creating vDisk image: {e}")
