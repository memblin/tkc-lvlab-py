import hashlib
import gnupg
import os
import re
import requests

from tqdm import tqdm


def download_file(url, destination):
    """Download a file via requests library"""

    # Streaming, so we can pop a stauts bar
    response = requests.get(url, stream=True, timeout=10)

    total_size = int(response.headers.get("content-length", 0))
    block_size = 1024

    with tqdm(total=total_size, unit="B", unit_scale=True) as progress_bar:
        with open(destination, "wb") as file:
            for data in response.iter_content(block_size):
                progress_bar.update(len(data))
                file.write(data)

    if total_size != 0 and progress_bar.n != total_size:
        #raise RuntimeError(f"Could not download file: {url}")
        print(f"Failed to download {url}, try init again. If issue continues validate remote file accessibility.")



def gpg_verify_file(keyring_fpath=None, verify_fpath=None):
    """Attempt GPG verification of file content."""

    if os.path.isfile(keyring_fpath) and os.path.isfile(verify_fpath):
        print(f"Attempting verification of {verify_fpath} with {keyring_fpath}.")

        gpg = gnupg.GPG()

        with open(keyring_fpath, 'rb') as keyring_data:
            gpg.import_keys(keyring_data.read())

        with open(verify_fpath, 'rb') as signed_data:
            verified_data = gpg.decrypt(signed_data.read())
 
        if verified_data.valid:
            verified_fpath = f"{verify_fpath}.verified" 
            with open(verified_fpath, 'wb') as verified_f:
                verified_f.write(verified_data.data)
            print(f"Verified checksum data written: {verified_fpath }")
        else:
            print(f"\n !! GPG verification of {verify_fpath} failed !!\n")
            print(f"Status: {verified_data.status}")
            print(f"Error: {verified_data.stderr}")
    else:
        raise SystemExit(f"{keyring_fpath} or {verify_fpath} file missing. Verify configuration.")


def parse_checksum_file(checksum_fpath=None):
    """Parse checksum file"""
    checksums = {}

    # Regex patterns for various checksum formats
    fedora_pattern = re.compile(r'^SHA\d+\s\((.+)\)\s=\s(.+)$')
    debian_pattern = re.compile(r'(\w+)\s+(\S+)')

    with open(checksum_fpath, 'r') as checksum_file:
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


def checksum_verify_file(checksum_fpath=None, verify_fpath=None, checksum_type=None, ):
    """Attempt checksum verification of file content."""

    hash_algorithms = {
        'sha256': hashlib.sha256,
        'sha512': hashlib.sha512
    }

    if checksum_type == None:
        raise SystemExit(f"Please configure a checksum_type if you configure a checksume_url for an image.")

    if checksum_type in hash_algorithms:
        sha = hash_algorithms[checksum_type]()
    else:
        raise SystemExit(f"Unsupported checksum algorithm {checksum_type}")

    # Swap in the .verified checksum file name if one exists.
    if os.path.isfile(checksum_fpath + ".verified"):
        checksum_fpath += ".verified"

    if os.path.isfile(checksum_fpath) and os.path.isfile(verify_fpath):
        print(f"Attempting {checksum_type} verification of {verify_fpath} with {checksum_fpath}.")

        checksums = parse_checksum_file(checksum_fpath)
        expected_checksum = checksums.get(os.path.basename(verify_fpath))

        with open(verify_fpath, 'rb') as verify_file:
            sha.update(verify_file.read())
            caclulated_checksum =  sha.hexdigest()

        if caclulated_checksum == expected_checksum:
            print(f"Calculated checksum OK for {verify_fpath} matches expected checksum from {checksum_fpath}.")
        else:
            print(f"Calculated checksum BAD for {verify_fpath} does not match expected checksum from {checksum_fpath}!!")
            print(f"Calculated Cheksum: {caclulated_checksum}")

    else:
        raise SystemExit(f"{checksum_fpath} or {verify_fpath} file missing. Verify configuration.")
