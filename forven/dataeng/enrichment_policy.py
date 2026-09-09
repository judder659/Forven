"""Shared availability limits for historical enrichment joins.

Missing observations remain unknown. Event collectors must persist observed zero
buckets themselves; a join cannot infer capture coverage from a missing row.
"""

import re
from pathlib import Path


def enrichment_max_age_seconds(path: Path, columns: tuple[str, ...] | list[str]) -> int:
    """Use the stored cadence, never gaps between observations, as an age limit."""
    if "funding_rate" in columns:
        return 8 * 3600
    match = re.search(r"(?:^|_)(\d+)([mhdw])(?:\.|$)", path.name)
    if match:
        duration = int(match[1]) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[match[2]]
        # Daily research-only series can span market holidays/weekends.
        return duration * (4 if match[2] == "d" else 1)
    return 3600
