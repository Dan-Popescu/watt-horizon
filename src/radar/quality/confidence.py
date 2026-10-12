"""Per-field confidence: a raw score from the evidence, calibrated on human-verified values.

raw = arbiter support (quality/arbiter.py) x penalties (sources disagree, field flagged, OCR / unread pages,
failed check). Missing fields get a raw score for "absent" (lower when the text mentions the value).
Calibration maps raw scores to observed accuracy per bin (smoothed toward the bin centre), learned at every run
from reviewed filings. A field is auto-accepted only when its calibrated confidence is >= AUTO_ACCEPT and its bin
has enough reviewed values with a Wilson lower bound >= AUTO_ACCEPT_LOW: before that, nothing is auto-accepted.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import pairwise

from radar.domain.enums import CheckStatus
from radar.domain.fields import Value, field_values
from radar.domain.models import Check, Correction, Filing, Flag
from radar.evaluation.fields import compare
from radar.evaluation.stats import wilson
from radar.pipeline.corrections import latest
from radar.quality.arbiter import verified_values

SCORED = ("developer", "siret", "commune", "power_mw", "energy_mwh", "substation", "filed_on", "review_decision")
EDGES = (0.0, 0.5, 0.7, 0.8, 0.9, 0.95, 1.0001)
PRIOR_N = 2.0
AUTO_ACCEPT = 0.95
AUTO_ACCEPT_LOW = 0.9
AUTO_ACCEPT_MIN_N = 30
FLAG_FIELD = {"power": "power_mw", "mw_max": "power_mw"}  # flag field names -> scored field names


def raw_scores(
    values: dict[str, Value],
    support: dict[str, tuple[float, int, int]],
    flags: Iterable[Flag],
    checks: Iterable[Check],
    read: str,
) -> dict[str, float]:
    flagged: dict[str, set[str]] = {}
    for f in flags:
        if f.field:
            flagged.setdefault(FLAG_FIELD.get(f.field, f.field), set()).add(f.code)
    reading = {"unread": 0.6, "ocr": 0.9}.get(read, 1.0)
    siret_ok = any(c.name == "siret" and c.status is CheckStatus.OK for c in checks)
    siret_alert = any(c.name == "siret" and c.status is CheckStatus.ALERT for c in checks)
    out = {}
    for name in SCORED:
        codes = flagged.get(name, set())
        if values.get(name) in (None, ""):  # confidence that the value is really absent from the documents
            score = 0.3 if "mentioned_not_extracted" in codes else 0.7
        else:
            score, agreeing, total = support.get(name, (0.75, 1, 1))
            if agreeing < total:
                score *= 0.7
            if codes & {"quote_not_found", "disagreement"}:
                score *= 0.5
            if name == "siret":
                score = max(score, 0.97) if siret_ok else score * (0.5 if siret_alert else 1.0)
        out[name] = round(score * reading, 4)
    return out


@dataclass
class Calibration:
    bins: list[tuple[float, float, int, int]] = field(default_factory=list)  # (low, high, correct, seen)

    def _bin(self, raw: float) -> tuple[float, float, int, int]:
        for b in self.bins:
            if b[0] <= raw < b[1]:
                return b
        return (0.0, 1.0001, 0, 0)

    def confidence(self, raw: float) -> float:
        low, high, correct, seen = self._bin(raw)
        centre = (low + min(high, 1.0)) / 2
        return round((correct + PRIOR_N * centre) / (seen + PRIOR_N) if seen else raw, 4)

    def auto_accepted(self, raw: float) -> bool:
        _, _, correct, seen = self._bin(raw)
        bounds = wilson(correct, seen)
        return (
            seen >= AUTO_ACCEPT_MIN_N
            and self.confidence(raw) >= AUTO_ACCEPT
            and bounds is not None
            and bounds[0] >= AUTO_ACCEPT_LOW
        )

    def diagram(self) -> list[dict[str, object]]:
        """Reliability diagram: per bin, observed accuracy vs bin range, with interval."""
        return [
            {
                "bin": f"{lo:.2f}-{min(hi, 1.0):.2f}",
                "seen": n,
                "accuracy": round(k / n, 3) if n else None,
                "ci95": wilson(k, n),
            }
            for lo, hi, k, n in self.bins
        ]

    def ece(self, pairs: list[tuple[float, bool]]) -> float | None:
        """Expected calibration error: average |predicted - observed| weighted by bin size (0 = perfect)."""
        if not pairs:
            return None
        total = 0.0
        for lo, hi, _, _ in self.bins:
            inside = [(r, ok) for r, ok in pairs if lo <= r < hi]
            if inside:
                predicted = sum(self.confidence(r) for r, _ in inside) / len(inside)
                observed = sum(ok for _, ok in inside) / len(inside)
                total += len(inside) * abs(predicted - observed)
        return round(total / len(pairs), 4)


def observations(filings: Iterable[Filing], corrections: Iterable[Correction]) -> list[tuple[float, bool]]:
    """(machine raw score, machine value was right) for every human-verified field."""
    by_key: dict[str, list[Correction]] = {}
    for c in corrections:
        by_key.setdefault(c.filing_key, []).append(c)
    pairs = []
    for f in filings:
        decisions = latest(by_key.get(f.key, []))
        if not decisions or not f.field_score:
            continue
        expected = verified_values(f, decisions)
        machine = dict(f.machine.values) if f.machine else field_values(f)
        for name, raw in f.field_score.items():
            if name in expected:
                pairs.append((raw, compare(expected[name], machine.get(name)) in {"correct", "absent"}))
    return pairs


def calibrate(filings: Iterable[Filing], corrections: Iterable[Correction]) -> Calibration:
    pairs = observations(filings, corrections)
    bins = []
    for low, high in pairwise(EDGES):
        inside = [ok for raw, ok in pairs if low <= raw < high]
        bins.append((low, high, sum(inside), len(inside)))
    return Calibration(bins)
