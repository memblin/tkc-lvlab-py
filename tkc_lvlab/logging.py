import logging
import sys

def get_logger(name: str) -> logging.Logger:
    """Create a standardized logger"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stderr_handler = logging.StreamHandler(sys.stderr)

    stdout_handler.setLevel(logging.INFO)
    stderr_handler.setLevel(logging.ERROR)

    formatter = logging.Formatter("%(asctime)s - %(message)s")
    stdout_handler.setFormatter(formatter)
    stderr_handler.setFormatter(formatter)

    logger.addHandler(stdout_handler)
    logger.addHandler(stderr_handler)

    return logger
