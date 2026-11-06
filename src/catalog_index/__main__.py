"""Retired command; use the client-automation entry point."""

from collections.abc import Sequence

from client_automation.utils.legacy_cli import command_retired


def main(argv: Sequence[str] | None = None) -> int:
    """Report the supported command without running operations or exposing arguments."""
    return command_retired(argv)


if __name__ == "__main__":
    raise SystemExit(main())
