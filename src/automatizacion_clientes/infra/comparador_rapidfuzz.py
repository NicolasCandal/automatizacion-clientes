"""Adaptador de RapidFuzz para comparar nombres previamente normalizados."""

from rapidfuzz import fuzz


class ComparadorRapidFuzz:
    """Implementa estructuralmente ``ComparadorNombres`` mediante token_set_ratio.

    Solo calcula similitud. La normalización, los umbrales, las restricciones de
    broker y la elección de candidatos pertenecen al dominio. No mantiene
    estado ni realiza operaciones de red o archivos.
    """

    def similitud(self, nombre_consulta: str, nombre_candidato: str) -> float:
        """Devuelve la puntuación completa sin aplicar umbrales ni preprocesadores.

        Args:
            nombre_consulta: Nombre de contacto ya normalizado por el dominio.
            nombre_candidato: Nombre de titular ya normalizado por el dominio.

        Returns:
            Similitud en la escala de 0 a 100, sin redondear. Las entradas sin
            palabras reciben cero, incluso cuando ambas están vacías.
        """
        return float(
            fuzz.token_set_ratio(
                nombre_consulta,
                nombre_candidato,
                processor=None,
                score_cutoff=0.0,
            )
        )
