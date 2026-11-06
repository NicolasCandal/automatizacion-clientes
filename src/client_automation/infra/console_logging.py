"""Temporary console logging with UTC timestamps and no log files."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from time import gmtime


@contextmanager
def console_logging() -> Iterator[logging.Logger]:
    """Install a CLI handler and restore the prior logging state afterward."""
    package = logging.getLogger("client_automation")
    level_previous = package.level
    propagation_previous = package.propagate
    handler = logging.StreamHandler()
    format = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    format.converter = gmtime
    handler.setFormatter(format)
    package.addHandler(handler)
    package.setLevel(logging.INFO)
    package.propagate = False
    try:
        yield package
    finally:
        package.removeHandler(handler)
        handler.close()
        package.setLevel(level_previous)
        package.propagate = propagation_previous
