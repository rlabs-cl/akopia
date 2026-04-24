"""Freshness filter + soft-boost re-ranking in IndexManager.

Exercises the pure helpers (``_cutoff_epoch`` + ``_apply_freshness_boost``)
so test runs don't need Qdrant/Meilisearch. The behaviour under test is
the scoring math — the HTTP wiring is trivial pass-through.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from concentrador.index_manager import (
    FRESHNESS_DECAY_DAYS,
    FRESHNESS_NEUTRAL,
    _apply_freshness_boost,
    _cutoff_epoch,
)


def _ts_days_ago(days: float) -> int:
    now = datetime.now(timezone.utc)
    return int((now - timedelta(days=days)).timestamp())


class TestCutoffEpoch:
    def test_none_passes_through(self):
        assert _cutoff_epoch(None) is None

    def test_negative_rejected_as_none(self):
        assert _cutoff_epoch(-1) is None

    def test_cutoff_is_now_minus_ndays(self):
        cutoff = _cutoff_epoch(60)
        expected = int(datetime.now(timezone.utc).timestamp()) - 60 * 86400
        # Allow a few seconds' drift for test execution time.
        assert abs(cutoff - expected) < 5


class TestFreshnessBoost:
    def test_beta_zero_preserves_original_order(self):
        results = [
            {"path": "a", "score": 0.9, "content_modified_ts": _ts_days_ago(365)},
            {"path": "b", "score": 0.8, "content_modified_ts": _ts_days_ago(1)},
        ]
        out = _apply_freshness_boost(results, beta=0.0)
        assert [r["path"] for r in out] == ["a", "b"]

    def test_beta_one_sorts_by_freshness(self):
        results = [
            {"path": "old", "score": 0.9, "content_modified_ts": _ts_days_ago(365)},
            {"path": "new", "score": 0.5, "content_modified_ts": _ts_days_ago(1)},
        ]
        out = _apply_freshness_boost(results, beta=1.0)
        assert [r["path"] for r in out] == ["new", "old"]

    def test_missing_ts_uses_neutral(self):
        results = [
            {"path": "none", "score": 0.6},
            {"path": "fresh", "score": 0.6, "content_modified_ts": _ts_days_ago(0)},
        ]
        out = _apply_freshness_boost(results, beta=0.5)
        assert out[0]["path"] == "fresh"
        missing = next(r for r in out if r["path"] == "none")
        assert missing["fresh_score"] == FRESHNESS_NEUTRAL

    def test_fresh_score_is_exp_decay(self):
        # A doc aged exactly FRESHNESS_DECAY_DAYS should score 1/e (~0.368).
        results = [
            {
                "path": "tau",
                "score": 0.0,
                "content_modified_ts": _ts_days_ago(FRESHNESS_DECAY_DAYS),
            },
        ]
        out = _apply_freshness_boost(results, beta=1.0)
        assert out[0]["fresh_score"] == pytest.approx(1.0 / math.e, abs=1e-3)

    def test_score_final_is_convex_combination(self):
        # Single doc, beta=0.3: score_final = 0.7*0.5 + 0.3*fresh
        ts = _ts_days_ago(0)
        results = [{"path": "x", "score": 0.5, "content_modified_ts": ts}]
        out = _apply_freshness_boost(results, beta=0.3)
        # fresh ≈ 1.0 for age=0.
        assert out[0]["score_final"] == pytest.approx(0.7 * 0.5 + 0.3 * 1.0, abs=1e-2)

    def test_beta_clamped_to_unit(self):
        results = [
            {"path": "a", "score": 0.9, "content_modified_ts": _ts_days_ago(365)},
            {"path": "b", "score": 0.8, "content_modified_ts": _ts_days_ago(1)},
        ]
        out_hi = _apply_freshness_boost(list(results), beta=99.0)
        out_eq = _apply_freshness_boost(list(results), beta=1.0)
        # Both saturate at beta=1.
        assert [r["path"] for r in out_hi] == [r["path"] for r in out_eq]


class TestHardFilterContract:
    """The hard filter at the IndexManager level is a pure translation
    from ``max_age_days`` to a Qdrant/Meili filter clause. We don't
    need a live server — just verify the clause composition.
    """

    def test_cutoff_excludes_stale(self):
        cutoff = _cutoff_epoch(30)
        stale = _ts_days_ago(365)
        fresh = _ts_days_ago(1)
        assert stale < cutoff
        assert fresh >= cutoff
