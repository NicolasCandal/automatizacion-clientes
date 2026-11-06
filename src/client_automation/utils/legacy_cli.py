"""Diagnostics for retired commands without processing legacy inputs."""

import logging
import sys
from collections.abc import Sequence

from client_automation.infra.console_logging import console_logging

LOGGER = logging.getLogger(__name__)


def command_retired(arguments: Sequence[str] | None = None) -> int:
    """Report the supported command without loading configuration or accessing files."""
    options = tuple(sys.argv[1:] if arguments is None else arguments)
    with console_logging():
        LOGGER.warning(
            "This legacy command was retired. Use client-automation --help. "
            "No operation was performed."
        )
    return 0 if options in {("--help",), ("-h",)} else 2
