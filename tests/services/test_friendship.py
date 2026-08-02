"""Friendship Score 산출과 파라미터 계약 검증함.

핵심 회귀는 "상관 0이 50점"임. 기존 구현은 음수를 잘라 0점을 냈고 그것이
정본 대비 가장 큰 왜곡이었음(상관 0에서 0점 대 50점).
"""

import numpy as np
import pandas as pd
import pytest

from server.services.analysis import classify_session_tier, compute_synchrony
from server.services.friendship import compute_friendship_score
from server.services.score_params import ScoreParams

ALPHA_PARAMS = ScoreParams(sync_channel=None, sync_band="alpha")


def _session(start: str, n: int, values: dict[str, np.ndarray]) -> pd.DataFrame:
    """초당 1행 시계열 DataFrame 생성함"""
    ts = pd.date_range(start=start, periods=n, freq="1s")
    return pd.DataFrame({"timestamp": ts, **values})


class TestFriendshipScore:
    """가중 합과 정규화 계약 검증함"""

    def test_zero_correlation_is_fifty(self):
        """[핵심 회귀] 상관 0은 0점이 아니라 50점임"""
        score, _ = compute_friendship_score(0.0, None, ScoreParams())
        assert score == pytest.approx(50.0)

    def test_perfect_and_inverse_correlation(self):
        """상관 1은 100점, 상관 -1은 0점임"""
        params = ScoreParams()
        assert compute_friendship_score(1.0, None, params)[0] == pytest.approx(100.0)
        assert compute_friendship_score(-1.0, None, params)[0] == pytest.approx(0.0)

    def test_missing_synchrony_is_none_not_zero(self):
        """동조율 미측정은 None임. 0점으로 대체하면 완전 역상관과 구분 불가함"""
        score, meta = compute_friendship_score(None, None, ScoreParams())
        assert score is None
        assert meta["reason"] == "synchrony_missing"

    def test_faa_term_excluded_when_avoidance_missing(self):
        """avoidance가 없으면 FAA 항을 분자와 분모 양쪽에서 제외함"""
        with_faa = ScoreParams(w_sync=1.0, w_faa=0.25)
        without_faa = ScoreParams(w_sync=1.0, w_faa=0.0)
        assert (
            compute_friendship_score(0.4, None, with_faa)[0]
            == compute_friendship_score(0.4, None, without_faa)[0]
        )

    def test_faa_term_applied_when_avoidance_present(self):
        """avoidance가 있으면 가중 합에 포함됨"""
        params = ScoreParams(w_sync=1.0, w_faa=1.0)
        # sync_norm = 0.5, faa_term = 1 - 0.0 = 1.0 이므로 (0.5 + 1.0) / 2 = 0.75
        score, meta = compute_friendship_score(0.0, 0.0, params)
        assert score == pytest.approx(75.0)
        assert meta["terms"] == ["sync", "faa"]

    def test_zero_effective_weight_returns_none(self):
        """w_sync=0에 avoidance 결측이면 0으로 나누지 않고 미산출로 낮춤"""
        params = ScoreParams(w_sync=0.0, w_faa=0.25)
        score, meta = compute_friendship_score(0.5, None, params)
        assert score is None
        assert meta["reason"] == "no_effective_term"

    def test_avoidance_out_of_range_raises(self):
        """회피율은 [0, 1] 계약임"""
        with pytest.raises(ValueError):
            compute_friendship_score(0.5, 1.5, ScoreParams(w_faa=0.25))


class TestScoreParams:
    """파라미터 검증과 파생값 계약 검증함"""

    def test_required_total_derives_from_trim(self):
        """필요 측정 시간은 상수가 아니라 trim에서 파생됨"""
        assert ScoreParams().required_total_sec == 210
        assert ScoreParams(trim_start_sec=30).required_total_sec == 225

    def test_negative_weight_rejected(self):
        with pytest.raises(ValueError):
            ScoreParams(w_sync=-1.0)

    def test_unknown_corr_method_rejected(self):
        with pytest.raises(ValueError):
            ScoreParams(corr_method="kendall")

    def test_ledger_carries_formula_version(self):
        """파라미터 값이 같아도 구현 세대를 구분할 수 있어야 함"""
        assert ScoreParams().to_dict()["formula_version"] == "1"

    def test_from_env_reads_at_call_time(self, monkeypatch):
        """환경변수는 import 시점이 아니라 호출 시점에 읽음"""
        monkeypatch.setenv("FS_SYNC_BAND", "beta")
        monkeypatch.setenv("FS_W_FAA", "0.25")
        params = ScoreParams.from_env()
        assert params.sync_band == "beta"
        assert params.w_faa == 0.25

    def test_blank_channel_env_means_spatial_mean(self, monkeypatch):
        """FS_SYNC_CHANNEL이 빈 값이면 공간평균 열을 대상으로 삼음"""
        monkeypatch.setenv("FS_SYNC_CHANNEL", "")
        assert ScoreParams.from_env().sync_column_candidates == ["gamma"]


class TestSessionTier:
    """trim 파생 tier 판정의 고정 케이스임"""

    def test_three_minute_session_is_partial(self):
        """3분(180초) 측정은 VALID가 아니라 PARTIAL임. 유효 구간이 150초"""
        assert classify_session_tier(180) == "PARTIAL"

    def test_required_total_is_valid(self):
        """210초(3분 30초)부터 VALID임"""
        assert classify_session_tier(210) == "VALID"

    def test_tier_follows_trim_params(self):
        """trim을 늘리면 같은 측정이 등급을 잃음"""
        params = ScoreParams(trim_start_sec=30, trim_end_sec=30)
        assert classify_session_tier(210, params) == "PARTIAL"


class TestSynchronyColumnSelection:
    """대상 열 해석과 폴백 계약 검증함"""

    def test_prefers_channel_column(self):
        """현행 CSV에서는 Pz 감마를 씀"""
        rng = np.random.default_rng(0)
        wave = rng.normal(size=60)
        cols = {"gamma": wave, "Pz_gamma": wave}
        a = _session("2026-08-01 10:00:00", 60, cols)
        b = _session("2026-08-01 10:00:00", 60, cols)
        rho, meta = compute_synchrony(a, b)
        assert meta["sync_column_used"] == "Pz_gamma"
        assert rho == pytest.approx(1.0)
        # 공간평균 병기값도 함께 계산됨 (감마 오염 사후 판별용)
        assert meta["sync_secondary"] == pytest.approx(1.0)

    def test_falls_back_to_spatial_mean(self):
        """구형 CSV는 채널별 열이 없어 공간평균으로 폴백함"""
        rng = np.random.default_rng(1)
        wave = rng.normal(size=60)
        a = _session("2026-08-01 10:00:00", 60, {"gamma": wave})
        b = _session("2026-08-01 10:00:00", 60, {"gamma": wave})
        _, meta = compute_synchrony(a, b)
        assert meta["sync_column_used"] == "gamma"

    def test_missing_column_returns_none(self):
        """후보 열이 하나도 없으면 미측정임"""
        wave = np.arange(60, dtype=float)
        a = _session("2026-08-01 10:00:00", 60, {"alpha": wave})
        b = _session("2026-08-01 10:00:00", 60, {"alpha": wave})
        rho, meta = compute_synchrony(a, b)
        assert rho is None
        assert meta["sync_column_used"] is None

    def test_constant_series_returns_none(self):
        """한쪽이 상수면 순위상관이 nan이므로 미측정으로 낮춤"""
        n = 60
        a = _session("2026-08-01 10:00:00", n, {"alpha": np.arange(n, dtype=float)})
        b = _session("2026-08-01 10:00:00", n, {"alpha": np.ones(n)})
        rho, _ = compute_synchrony(a, b, ALPHA_PARAMS)
        assert rho is None

    def test_spearman_beats_pearson_on_monotonic_nonlinear(self):
        """단조 비선형 관계에서 순위상관은 1.0이고 피어슨은 그보다 낮음"""
        n = 60
        x = np.linspace(1.0, 5.0, n)
        pairs = {"alpha": x}
        a = _session("2026-08-01 10:00:00", n, pairs)
        b = _session("2026-08-01 10:00:00", n, {"alpha": x**5})

        spearman, _ = compute_synchrony(a, b, ALPHA_PARAMS)
        pearson, _ = compute_synchrony(
            a,
            b,
            ScoreParams(sync_channel=None, sync_band="alpha", corr_method="pearson"),
        )
        assert spearman == pytest.approx(1.0)
        assert pearson < spearman
