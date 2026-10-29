"""Statistics that keep small samples honest, and the stable dev/test split.

- Wilson interval: a 95 % range for a rate measured on n items (8/10 = 80 % but really 49-94 %).
- Split: each filing number is assigned for good to "dev" (look at its errors, tune rules on it) or "test" (only
  aggregate scores are ever shown). Tuning on the test split would make its scores optimistic.
"""

import hashlib
import math
from typing import Literal

from radar.domain.models import Filing

TEST_SHARE = 0.3
Partition = Literal["dev", "calibration", "test"]


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)


def split_of(number: str) -> str:
    value = int(hashlib.sha256(f"split:{number}".encode()).hexdigest()[:8], 16) / 2**32
    return "test" if value < TEST_SHARE else "dev"


def partition_of(filing: Filing) -> Partition:
    """Use the persisted group assignment; retain legacy test membership as a safe fallback."""
    assigned = filing.strata.get("evaluation_partition")
    if assigned in {"dev", "calibration", "test"}:
        if assigned == "dev":
            return "dev"
        return "calibration" if assigned == "calibration" else "test"
    if assigned is not None:
        raise ValueError("Invalid evaluation partition")
    if split_of(filing.number) == "test":
        return "test"
    value = int(hashlib.sha256(f"calibration:{filing.number}".encode()).hexdigest()[:8], 16) / 2**32
    return "calibration" if value < 0.2 else "dev"
