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
        raise RuntimeError(f"Could not download file: {url}")
