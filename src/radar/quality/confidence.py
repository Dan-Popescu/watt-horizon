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
from radar.evaluation.stats import partition_of, wilson
from radar.pipeline.corrections import latest
from radar.quality.arbiter import observation_groups, verified_values

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
    by_field: dict[tuple[str, bool], list[tuple[float, float, int, int]]] = field(default_factory=dict)

    def _bin(self, raw: float, field_name: str | None, present: bool) -> tuple[float, float, int, int]:
        bins = self.by_field.get((field_name, present), []) if field_name is not None else self.bins
        # Aggregate diagrams are descriptive, never a calibration for a particular decision.
        if self.by_field and field_name is None:
            bins = []
        for b in bins:
            if b[0] <= raw < b[1]:
                return b
        return (0.0, 1.0001, 0, 0)

    def confidence(self, raw: float, *, field_name: str | None = None, present: bool = True) -> float:
        low, high, correct, seen = self._bin(raw, field_name, present)
        centre = (low + min(high, 1.0)) / 2
        return round((correct + PRIOR_N * centre) / (seen + PRIOR_N) if seen else raw, 4)

    def auto_accepted(self, raw: float, *, field_name: str | None = None, present: bool = True) -> bool:
        _, _, correct, seen = self._bin(raw, field_name, present)
        bounds = wilson(correct, seen)
        return (
            seen >= AUTO_ACCEPT_MIN_N
            and self.confidence(raw, field_name=field_name, present=present) >= AUTO_ACCEPT
            and bounds is not None
            and bounds[0] >= AUTO_ACCEPT_LOW
        )

    def diagram(self) -> list[dict[str, object]]:
        """Calibration samples per field and presence decision; never imply pooled reliability."""
        series = sorted(self.by_field.items()) if self.by_field else [(("", True), self.bins)]
        return [
            {
                "field": name,
                "present": present,
                "bin": f"{lo:.2f}-{min(hi, 1.0):.2f}",
                "seen": n,
                "sample_unit": "independent_group",
                "accuracy": round(k / n, 3) if n else None,
                "ci95": wilson(k, n),
            }
            for (name, present), bins in series
            for lo, hi, k, n in bins
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

    def field_ece(self, pairs: list["FieldObservation"]) -> float | None:
        """Out-of-sample calibration error, using only independent test observations.

        Unknown decision groups stay raw rather than borrowing another field's evidence.
        """
        held_out = _independent_observations([p for p in pairs if p.partition == "test"])
        if not held_out:
            return None
        total = 0.0
        for name, present in sorted({(p.field_name, p.present) for p in held_out}):
            for low, high in pairwise(EDGES):
                inside = [p for p in held_out if p.field_name == name and p.present is present and low <= p.raw < high]
                if inside:
                    predicted = sum(self.confidence(p.raw, field_name=name, present=present) for p in inside) / len(
                        inside
                    )
                    observed = sum(p.correct for p in inside) / len(inside)
                    total += len(inside) * abs(predicted - observed)
        return round(total / len(held_out), 4)


@dataclass(frozen=True)
class FieldObservation:
    field_name: str
    present: bool
    raw: float
    correct: bool
    partition: str
    group: str
    observation_count: int = 1


def _independent_observations(pairs: list[FieldObservation]) -> list[FieldObservation]:
    """One trial per group, decision type and score bin; every constituent must be right."""
    grouped: dict[tuple[str, str, str, bool, int], list[FieldObservation]] = {}
    for item in pairs:
        bin_index = next((i for i, (lo, hi) in enumerate(pairwise(EDGES)) if lo <= item.raw < hi), None)
        if bin_index is None:
            raise ValueError("Field confidence must be a finite score between zero and one")
        key = (item.partition, item.group, item.field_name, item.present, bin_index)
        grouped.setdefault(key, []).append(item)
    independent = []
    for (partition, group, name, present, _), items in sorted(grouped.items()):
        count = sum(p.observation_count for p in items)
        independent.append(
            FieldObservation(
                name,
                present,
                sum(p.raw * p.observation_count for p in items) / count,
                all(p.correct for p in items),
                partition,
                group,
                count,
            )
        )
    return independent


def observations(filings: Iterable[Filing], corrections: Iterable[Correction]) -> list[tuple[float, bool]]:
    """Legacy unstratified view for descriptive reports; not a training population."""
    return [(p.raw, p.correct) for p in field_observations(filings, corrections)]


def field_observations(
    filings: Iterable[Filing], corrections: Iterable[Correction], *, partition: str | None = None
) -> list[FieldObservation]:
    """Independent human-verified decisions, retaining field, presence and raw sample count."""
    filings = list(filings)
    groups = observation_groups(filings)
    by_key: dict[str, list[Correction]] = {}
    for c in corrections:
        by_key.setdefault(c.filing_key, []).append(c)
    pairs = []
    for f in filings:
        assigned = partition_of(f)
        if partition is not None and assigned != partition:
            continue
        decisions = latest(by_key.get(f.key, []))
        if not decisions or not f.field_score:
            continue
        expected = verified_values(f, decisions)
        machine = dict(f.machine.values) if f.machine else field_values(f)
        for name, raw in f.field_score.items():
            if name in expected:
                pairs.append(
                    FieldObservation(
                        name,
                        machine.get(name) not in (None, ""),
                        raw,
                        compare(expected[name], machine.get(name)) in {"correct", "absent"},
                        assigned,
                        groups[f.key],
                    )
                )
    return _independent_observations(pairs)


def calibrate(filings: Iterable[Filing], corrections: Iterable[Correction]) -> Calibration:
    """Fit only the calibration partition, independently for each decision type."""
    pairs = field_observations(filings, corrections, partition="calibration")

    def bins_for(items: list[FieldObservation]) -> list[tuple[float, float, int, int]]:
        return [
            (low, high, sum(p.correct for p in items if low <= p.raw < high), sum(low <= p.raw < high for p in items))
            for low, high in pairwise(EDGES)
        ]

    groups = {(p.field_name, p.present) for p in pairs}
    return Calibration(
        bins_for(pairs),
        {
            (name, present): bins_for([p for p in pairs if p.field_name == name and p.present is present])
            for name, present in sorted(groups)
        },
    )
