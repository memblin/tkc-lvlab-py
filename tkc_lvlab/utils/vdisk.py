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

    # TODO: Make sure paths exist

    # Execute the command
    try:
        subprocess.run(command, check=True)
        print("vDisk image created successfully.")
    except subprocess.CalledProcessError as e:
        print(f"An error occurred creating vDisk image: {e}")
