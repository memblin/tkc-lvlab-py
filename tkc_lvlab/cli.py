import click
import os
import requests
import yaml

from tqdm import tqdm
from urllib.parse import urlparse


def download_file(url, destination):
    """Download a file via requests library"""

    # Streaming, so we can iterate over the response.
    response = requests.get(url, stream=True)

    # Sizes in bytes.
    total_size = int(response.headers.get("content-length", 0))
    block_size = 1024

    with tqdm(total=total_size, unit="B", unit_scale=True) as progress_bar:
        with open(destination, "wb") as file:
            for data in response.iter_content(block_size):
                progress_bar.update(len(data))
                file.write(data)

    if total_size != 0 and progress_bar.n != total_size:
        raise RuntimeError(f"Could not download file: {url}")


def parse_config(fpath=None):
    """Read config file"""

    if fpath == None:
        fpath = "Lvlab.yml"

    if os.path.isfile(fpath):
        print(f"Loading {fpath} config...\n")
        with open(fpath, "r") as f:
            config = yaml.safe_load(f)

        environment = config["environment"][0]
        images = config["images"]
        config_defaults = environment.get("config_defaults", {})
        machines = environment.get("machines", {})

        return (environment, images, config_defaults, machines)

    else:
        print(f"{fpath} not found. Please create enviornment definition.")

        return (None, None, None, None)


def parse_file_from_url(url):
    """Return the filename from the end of a URL"""
    parsed_url = urlparse(url)
    filename = os.path.basename(parsed_url.path)

    return filename


@click.group()
def run():
    """A command-line tool for managing VMs."""
    print()
    pass


@click.command()
@click.argument("vm_name")
def up(vm_name):
    """Start a VM."""
    click.echo(f"Starting VM: {vm_name}")


@click.command()
@click.argument("vm_name")
def destroy(vm_name):
    """Destroy a VM."""
    click.echo(f"Destroying VM: {vm_name}")


@click.command()
def init():
    """Initialize the environment."""
    environment, images, config_defaults, _ = parse_config()
    print(f'Initializing Libvirt Lab Environment: {environment["name"]}\n')

    cloud_image_dir = config_defaults.get(
        "cloud_image_base_dir", "/var/lib/libvirt/cloud-images"
    )

    for image in images:
        image_fname = parse_file_from_url(image["image_url"])
        image_fpath = os.path.join(cloud_image_dir, image_fname)

        if os.path.isfile(image_fpath):
            print(f"The image {image_fpath} already exists.")
        else:
            print(f"The image {image_fpath} does not exist, attempting to download.")
            download_file(image["image_url"], image_fpath)

        if image["checksum_url"]:
            print("Checksum URL is set, validating checksum of existing cloud image")
            checksum_url_fname = parse_file_from_url(image["checksum_url"])
            checksum_url_fpath = os.path.join(cloud_image_dir, checksum_url_fname)

            if os.path.isfile(checksum_url_fpath):
                print(f"The image checksum file already exists.")
            else:
                print(
                    f"The image checksum file {checksum_url_fpath} does not exist, attempting to download."
                )
                download_file(image["checksum_url"], checksum_url_fpath)

        if image.get("checksum_url_gpg", None):
            print("Checksum URL GPG is set, this is normally to validate the checksum_url content.")
            checksum_url_gpg_fname = parse_file_from_url(image["checksum_url_gpg"])
            checksum_url_gpg_fpath = os.path.join(cloud_image_dir, checksum_url_gpg_fname)

            if os.path.isfile(checksum_url_gpg_fpath):
                print(f"The image checksum GPG file {checksum_url_gpg_fpath} already exists.")
            else:
                print(
                    f"The image checksum GPG file {checksum_url_gpg_fpath} does not exist, attempting to download."
                )
                download_file(image["checksum_url_gpg"], checksum_url_gpg_fpath)

        print()


@click.command()
def status():
    """Show the status of the environment."""
    print()

    environment, images, config_defaults, machines = parse_config()

    print("Machines Defined:\n")
    for vm in machines:
        print(f"  - { vm['hostname'] }")

    print()

    print("Images Used:\n")
    for img in images:
        print(f"  - { img['name'] }")

    print()


# Bulid the CLI
run.add_command(up)
run.add_command(destroy)
run.add_command(init)
run.add_command(status)


if __name__ == "__main__":
    run()
