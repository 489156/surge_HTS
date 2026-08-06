"""Calibration-discrimination diagnostic — the AUC metric + honest verdict."""

from surge.duel import calibsearch


def test_auc_perfect_discrimination():
    # high conviction always correct, low conviction always wrong → AUC 1.0
    samples = [(0.9, 1), (0.8, 1), (0.7, 1), (0.1, 0), (0.2, 0), (0.05, 0)]
    assert calibsearch.auc_conviction_correct(samples) == 1.0


def test_auc_inverted_discrimination():
    # high conviction always WRONG → AUC 0.0 (conviction anti-predicts)
    samples = [(0.9, 0), (0.8, 0), (0.1, 1), (0.2, 1)]
    assert calibsearch.auc_conviction_correct(samples) == 0.0


def test_auc_no_discrimination_is_half():
    # correctness independent of conviction → AUC 0.5
    samples = [(0.9, 1), (0.9, 0), (0.1, 1), (0.1, 0)]
    assert calibsearch.auc_conviction_correct(samples) == 0.5


def test_auc_none_when_one_class_absent():
    assert calibsearch.auc_conviction_correct([(0.5, 1), (0.6, 1)]) is None
    assert calibsearch.auc_conviction_correct([]) is None


def test_split_auc_honest_verdict_threshold():
    # flat holdout (all AUC ~0.5) → discriminates False
    flat = sorted([(f"d{i}", 0.5, i % 2) for i in range(200)])
    r = calibsearch._split_auc(flat, holdout_frac=0.3)
    assert r["auc_hold"] is not None
    assert r["auc_hold"] < calibsearch._MARGIN          # no usable discrimination
