"""Comando retirado; el punto de entrada único es automatizacion-clientes."""

from collections.abc import Sequence

from automatizacion_clientes.utils.cli_compatibilidad import comando_retirado


def main(argv: Sequence[str] | None = None) -> int:
    """Informa cómo migrar sin ejecutar operaciones ni exponer argumentos."""
    return comando_retirado(argv)


if __name__ == "__main__":
    raise SystemExit(main())
