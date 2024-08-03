import os
import sys
from urllib.parse import urlparse

import yaml
from tkc_lvlab.logging import get_logger


logger = get_logger(__name__)


def parse_config(fpath=None):
    """Read config file"""

    if fpath == None:
        fpath = "Lvlab.yml"

    if os.path.isfile(fpath):
        with open(fpath, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        environment = config["environment"][0]
        images = config["images"]
        config_defaults = environment.get("config_defaults", {})
        machines = environment.get("machines", {})

        return (environment, images, config_defaults, machines)
    logger.error("Config file %s not found. Create an environment definition configuration to use this utility.", fpath)
    sys.exit(1)


def parse_file_from_url(url):
    """Return the filename from the end of a URL"""
    parsed_url = urlparse(url)
    filename = os.path.basename(parsed_url.path)

    return filename
