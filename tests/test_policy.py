"""Verdict policy on SYNTHETIC score data. No model is involved: these numbers are made up on purpose to
exercise the labels, and are not results."""
import pytest

from verdictloop.policy import decide, noise_band, tag_drops

N_T, N_H = 16, 8


def runs(*scores, status="OK"):
    return [{"score": s, "status": status} for s in scores]


BASE = runs(0.5000, 0.5625, 0.5000, 0.5625, 0.5000)       # sd ~0.034 -> band ~0.069 (floor 0.0625)
HBASE = runs(0.375, 0.375, 0.500, 0.375, 0.375)


def verdict(cand, hcand=None, base=BASE, hbase=HBASE):
    return decide(base, cand, hbase, hcand if hcand is not None else runs(0.375, 0.375, 0.375),
                  n_tuning=N_T, n_heldout=N_H)


def test_noise_band_floor_is_one_scenario():
    assert noise_band([0.5, 0.5, 0.5], N_T) == pytest.approx(1 / N_T)


def test_noise_band_scales_with_spread():
    assert noise_band([0.25, 0.50, 0.75], N_T) == pytest.approx(2 * 0.25)


def test_noise_band_needs_three_repeats():
    with pytest.raises(ValueError):
        noise_band([0.5, 0.6], N_T)


def test_known_bad_edit_is_refuted():
    assert verdict(runs(0.250, 0.3125, 0.250)).label == "REFUTED"


def test_known_noop_is_neutral():
    assert verdict(runs(0.5625, 0.5000, 0.5625)).label == "NEUTRAL"


def test_identical_scores_are_neutral():
    assert verdict(runs(0.5, 0.5, 0.5)).label == "NEUTRAL"


def test_one_lucky_scenario_is_not_validated():
    # +1 scenario on every repeat is exactly the floor, never beyond it
    assert verdict(runs(0.5625, 0.5625, 0.5625), base=runs(0.5, 0.5, 0.5)).label == "NEUTRAL"


def test_improvement_with_clean_heldout_is_validated():
    assert verdict(runs(0.75, 0.8125, 0.75), runs(0.375, 0.500, 0.375)).label == "VALIDATED"


def test_improvement_with_heldout_regression_is_refuted_and_says_why():
    v = verdict(runs(0.75, 0.8125, 0.75), runs(0.125, 0.125, 0.250))
    assert v.label == "REFUTED" and "heldout_regression" in v.reason


def test_too_few_valid_candidate_repeats_is_infra_fail():
    cand = runs(0.9) + runs(None, status="INFRA_FAIL") * 2
    assert verdict(cand).label == "INFRA_FAIL"


def test_infra_fail_runs_are_excluded_not_counted_as_zero():
    assert verdict(runs(0.5, 0.5625) + runs(None, status="INFRA_FAIL")).label == "NEUTRAL"


def test_tag_drops_flags_a_group_that_regressed_while_the_aggregate_rose():
    tags = {"a": "W1", "b": "W1", "c": "W3", "d": "W3"}
    base = [{"a": False, "b": False, "c": True, "d": True}] * 2
    cand = [{"a": True, "b": True, "c": False, "d": True}] * 2
    rep = tag_drops(base, cand, tags)
    assert rep["W3"]["flagged"] and not rep["W1"]["flagged"]


def test_tag_drops_ignores_untagged_scenarios():
    assert tag_drops([{"a": True}], [{"a": False}], {"a": None}) == {}
