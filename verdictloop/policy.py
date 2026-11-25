"""Verdict policy: turns repeated scores into one label.

Inputs are lists of pass results, one per repeat of the whole eval:
  {"score": float in [0, 1] or None, "status": "OK" | "INFRA_FAIL"}

- Noise band = max(2 x sample sd of the baseline repeats, 1 / number of scenarios). One scenario
  flipping is never evidence on its own.
- delta = mean(candidate valid repeats) - mean(baseline valid repeats).
- delta < -band            -> REFUTED
- -band <= delta <= band   -> NEUTRAL
- delta > band             -> the held-out set decides: worse than its own band -> REFUTED
                              (heldout_regression), otherwise VALIDATED.
- Fewer than 2 valid candidate repeats -> INFRA_FAIL (excluded, rerun; never a zero).
"""
import statistics
from dataclasses import dataclass, field
from typing import List, Optional

K_SD = 2.0
MIN_BASELINE_REPEATS = 3
MIN_CANDIDATE_REPEATS = 2
MAX_INFRA_FRACTION = 0.15


@dataclass
class Verdict:
    label: str                      # VALIDATED | NEUTRAL | REFUTED | INFRA_FAIL
    reason: str
    delta: Optional[float] = None
    band: Optional[float] = None
    heldout_delta: Optional[float] = None
    heldout_band: Optional[float] = None
    detail: dict = field(default_factory=dict)


def valid_scores(runs) -> List[float]:
    return [r["score"] for r in runs if r.get("status") == "OK" and r.get("score") is not None]


def noise_band(baseline_scores: List[float], n_scenarios: int) -> float:
    if len(baseline_scores) < MIN_BASELINE_REPEATS:
        raise ValueError(f"need at least {MIN_BASELINE_REPEATS} valid baseline repeats, got {len(baseline_scores)}")
    return max(K_SD * statistics.stdev(baseline_scores), 1.0 / n_scenarios)


def decide(baseline, candidate, heldout_baseline, heldout_candidate, *, n_tuning, n_heldout) -> Verdict:
    cand = valid_scores(candidate)
    if len(cand) < MIN_CANDIDATE_REPEATS:
        return Verdict("INFRA_FAIL", f"only {len(cand)} valid candidate repeats", detail={"valid": len(cand)})
    base = valid_scores(baseline)
    band = noise_band(base, n_tuning)
    delta = statistics.mean(cand) - statistics.mean(base)
    if delta < -band:
        return Verdict("REFUTED", "worse beyond the noise band on the tuning set", delta, band)
    if delta <= band:
        return Verdict("NEUTRAL", "inside the noise band on the tuning set", delta, band)
    hb, hc = valid_scores(heldout_baseline), valid_scores(heldout_candidate)
    if len(hc) < MIN_CANDIDATE_REPEATS:
        return Verdict("INFRA_FAIL", f"only {len(hc)} valid held-out candidate repeats", delta, band)
    hband = noise_band(hb, n_heldout)
    hdelta = statistics.mean(hc) - statistics.mean(hb)
    if hdelta < -hband:
        return Verdict("REFUTED", "heldout_regression: better on tuning, worse beyond the band on held-out",
                       delta, band, hdelta, hband)
    return Verdict("VALIDATED", "better beyond the band, no held-out regression", delta, band, hdelta, hband)


def tag_drops(baseline_outcomes, candidate_outcomes, tags, drop: float = 0.20):
    """Advisory only, never changes a verdict. Flags a tag whose pass rate fell by more than `drop`
    while the aggregate may have risen. `*_outcomes` are lists of {scenario_id: bool}."""
    def rates(outcomes):
        agg = {}
        for passed_by_id in outcomes:
            for sid, ok in passed_by_id.items():
                tag = tags.get(sid)
                if tag is not None:
                    p, n = agg.get(tag, (0, 0))
                    agg[tag] = (p + (1 if ok else 0), n + 1)
        return agg

    base, cand = rates(baseline_outcomes), rates(candidate_outcomes)
    return {t: {"baseline": base[t][0] / base[t][1], "candidate": cand[t][0] / cand[t][1],
                "flagged": base[t][0] / base[t][1] - cand[t][0] / cand[t][1] > drop + 1e-9}
            for t in sorted(set(base) & set(cand))}
