"""Moudle to contain CloudImage related operations"""

import hashlib
import os
import re
from urllib.parse import urlparse

import click
import gnupg
import requests
from tqdm import tqdm


# pylint: disable=too-many-instance-attributes
class CloudImage:
    """A cloud image definition"""

    def __init__(self, name, config, environment, config_defaults):
        """CloudImage

        Args:
            config (dict): {'name': 'fedora40',
                            'image_url': 'https://{URL_TO_THE_IMAGE}/Fedora-Cloud-Base-Generic.x86_64-40-1.14.qcow2',
                            'checksum_url': 'https://{URL_TO_THE_CHECKSUM_FILE}/Fedora-Cloud-40-1.14-x86_64-CHECKSUM',
                            'checksum_type': 'sha256',
                            'checksum_url_gpg': 'https://{URL_TO_THE_GPG_KEYRING}/fedora.gpg',
                            'network_version': 2}
        """
        self.name = name
        self.image_url = config.get("image_url", None)
        self.checksum_url = config.get("checksum_url", None)
        self.checksum_type = config.get("checksum_type", None)
        self.checksum_url_gpg = config.get("checksum_url_gpg", None)
        self.network_version = config.get("network_version", 1)
        self.filename = os.path.basename(urlparse(self.image_url).path)
        self.image_dir = os.path.join(
            config_defaults.get("cloud_image_basedir", "/var/lib/libvirt/images/lvlab"),
            "cloud-images",
        )
        self.image_fpath = os.path.join(os.path.expanduser(self.image_dir), self.filename)

        if self.checksum_url:
            # Debian 10, 11, and 12 use a checksum file name that conflicts
            # with one another so we need to put a suffix on the checksum file.
            match = re.search(r"debian-(\d+)", self.filename.lower())
            if match:
                version = match.group(1)
                checksum_filename = os.path.basename(urlparse(self.checksum_url).path) + f".debian{version}"
                self.checksum_fpath = os.path.join(
                    os.path.expanduser(self.image_dir), checksum_filename
                )
            else:
                self.checksum_fpath = os.path.join(
                    os.path.expanduser(self.image_dir), os.path.basename(urlparse(self.checksum_url).path)
                )
        else:
            self.checksum_fpath = None

        if self.checksum_url_gpg:
            self.checksum_gpg_fpath = os.path.join(
                os.path.expanduser(self.image_dir), os.path.basename(urlparse(self.checksum_url_gpg).path)
            )
        else:
            self.checksum_gpg_fpath = None

    @staticmethod
    def _download_file(url, destination):
        """Download a file associated with the cloud image."""

        click.echo(f"downloading to: {destination}")
        response = requests.get(url, stream=True, timeout=10)

        total_size = int(response.headers.get("content-length", 0))
        block_size = 1024

        with tqdm(total=total_size, unit="B", unit_scale=True) as progress_bar:
            with open(destination, "wb") as file:
                for data in response.iter_content(block_size):
                    progress_bar.update(len(data))
                    file.write(data)

        if total_size not in (0, progress_bar.n):
            return False

        return True

    def _manage_image_dir(self):
        """Ensure the environments cloud-image directory exists"""
        if "~" in self.image_dir:
            image_dir = os.path.expanduser(self.image_dir)
        else:
            image_dir = self.image_dir

        if not os.path.isdir(image_dir):
            click.echo(f"CloudImage creating image directory: {image_dir}")
            os.makedirs(image_dir, exist_ok=True)

    def download_image(self) -> bool:
        """Attempt to download the cloud image"""
        self._manage_image_dir()
        if self._download_file(self.image_url, self.image_fpath):
            return True

        return False

    def download_checksum(self) -> bool:
        """Attempt to download the cloud image chekcsum file"""
        if self._download_file(self.checksum_url, self.checksum_fpath):
            return True

        return False

    def download_checksum_gpg(self) -> bool:
        """Attempt to download the cloud image checksum GPG file"""
        if self._download_file(self.checksum_url_gpg, self.checksum_gpg_fpath):
            return True

        return False

    def exists_locally(self, file_type: str = "image") -> bool:
        """Reports if the file already exists on the local disk"""
        file_map = {
            "image": self.image_fpath,
            "checksum": self.checksum_fpath,
            "checksum_gpg": self.checksum_gpg_fpath,
        }

        file_to_check = file_map.get(file_type)

        if not file_to_check:
            raise ValueError(f"Unknown file type: {file_type}")

        return os.path.exists(file_to_check)

    def gpg_verify_checksum_file(self):
        """Verify the GPG signature on the checksum file if present"""
        if os.path.isfile(self.checksum_gpg_fpath) and os.path.isfile(
            self.checksum_fpath
        ):
            click.echo(
                f"CloudImage {self.name} GPG verification of {self.checksum_fpath}"
            )

            gpg = gnupg.GPG()
            with open(self.checksum_gpg_fpath, "rb") as keyring_file:
                gpg.import_keys(keyring_file.read())

            with open(self.checksum_fpath, "rb") as signed_file:
                verified = gpg.decrypt(signed_file.read())

            if verified.valid:
                verified_checksum_fpath = self.checksum_fpath + ".verified"
                with open(verified_checksum_fpath, "wb") as verified_file:
                    verified_file.write(verified.data)
                    return True

        return False

    @staticmethod
    def _parse_checksum_file(checksum_fpath: str) -> dict:
        """Parse checksum file into checksums"""
        checksums = {}

        # Regex patterns for various checksum formats
        fedora_pattern = re.compile(r"^SHA\d+\s\((.+)\)\s=\s(.+)$")
        debian_pattern = re.compile(r"(\w+)\s+(\S+)")

        # Sometimes we want to operate from a verified checksum file
        # but only if it exists.
        if os.path.exists(checksum_fpath + ".verified"):
            checksum_fpath = checksum_fpath + ".verified"

        with open(checksum_fpath, "r", encoding="utf-8") as checksum_file:
            lines = checksum_file.readlines()

        for line in lines:
            match = fedora_pattern.match(line)
            if match:
                filename = match.group(1)
                checksum = match.group(2)
                checksums[filename] = checksum

            match = debian_pattern.match(line)
            if match:
                checksum = match.group(1)
                filename = match.group(2)
                checksums[filename] = checksum

        return checksums

    def checksum_verify_image(self):
        """Attempt checksum verification of CloudImage file"""

        hash_algorithms = {"sha256": hashlib.sha256, "sha512": hashlib.sha512}

        if self.checksum_type is None:
            raise SystemExit(
                "Please configure a checksum_type if you configure a checksume_url for an image."
            )

        if self.checksum_type in hash_algorithms:
            sha = hash_algorithms[self.checksum_type]()
        else:
            raise SystemExit(f"Unsupported checksum algorithm {self.checksum_type}")

        # Swap in the .verified checksum file name if one exists.
        checksum_fpath = self.checksum_fpath
        if os.path.isfile(self.checksum_fpath + ".verified"):
            checksum_fpath += ".verified"

        if os.path.isfile(checksum_fpath) and os.path.isfile(self.image_fpath):
            checksums = self._parse_checksum_file(checksum_fpath)
            expected_checksum = checksums.get(os.path.basename(self.image_fpath))

            with open(self.image_fpath, "rb") as verify_file:
                sha.update(verify_file.read())
                caclulated_checksum = sha.hexdigest()

            if caclulated_checksum == expected_checksum:
                return True

        return False
