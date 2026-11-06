"""RapidFuzz adapter for comparing previously normalized names."""

from rapidfuzz import fuzz


class RapidFuzzComparator:
    """Implement NameComparator structurally using token_set_ratio."""

    def similarity(self, query_name: str, candidate_name: str) -> float:
        """Return the full score without thresholds or preprocessing."""
        return float(
            fuzz.token_set_ratio(
                query_name,
                candidate_name,
                processor=None,
                score_cutoff=0.0,
            )
        )
