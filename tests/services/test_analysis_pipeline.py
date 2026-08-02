"""analysis 서비스 파이프라인 단계별 단위/통합 테스트 수행함"""

from collections import OrderedDict
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from server.services.analysis import (
    CSV_BASE_DIR,
    AnalysisContractError,
    WindowSlot,
    analyze_pipeline_sequential,
    average_by_timestamp,
    build_pair_features,
    compute_baseline,
    compute_y,
    extract_features,
    run_full_pipeline,
    split_stimulus_windows,
)
from tests.conftest import (
    DEFAULT_BAND_COLS,
    TEST_GROUP_ID,
)

# ──────────────────────────────────────────────
# TestAverageByTimestamp
# ──────────────────────────────────────────────


class TestAverageByTimestamp:
    def test_timestamp_missing_raises_contract_error(self, band_cols):
        """timestamp 컬럼 누락 시 명시적 계약 오류가 발생함"""
        df = pd.DataFrame({band: [1.0] for band in band_cols})
        with pytest.raises(AnalysisContractError) as exc_info:
            average_by_timestamp(df, band_cols)
        assert exc_info.value.error_code == "TIMESTAMP_COLUMN_MISSING"

    def test_timestamp_col_groups_and_averages(self, timestamped_df, band_cols):
        """timestamp 컬럼 존재 시 동일 timestamp의 값이 평균화되어 행 수 감소함"""
        result = average_by_timestamp(timestamped_df, band_cols)
        # 30개 unique timestamp → 30행으로 축소
        assert len(result) == 30
        assert list(result.columns) == ["timestamp", *band_cols]
        assert result["timestamp"].is_monotonic_increasing

    def test_same_second_samples_are_averaged(self, band_cols):
        """마이크로초가 다른 같은 초 샘플을 하나로 평균화함"""
        df = pd.DataFrame(
            {
                "timestamp": [
                    "2026-01-01 00:00:00.100",
                    "2026-01-01 00:00:00.900",
                ],
                **{band: [1.0, 3.0] for band in band_cols},
            }
        )
        result = average_by_timestamp(df, band_cols)
        assert len(result) == 1
        assert result.loc[0, "alpha"] == 2.0
        assert result.loc[0, "timestamp"] == pd.Timestamp("2026-01-01")

    def test_unparsable_timestamp_raises_contract_error(self, band_cols):
        """전 행 timestamp 파싱 실패 시 명시적 계약 오류가 발생함"""
        df = pd.DataFrame(
            {
                "timestamp": ["invalid", None],
                **{band: [1.0, 2.0] for band in band_cols},
            }
        )
        with pytest.raises(AnalysisContractError) as exc_info:
            average_by_timestamp(df, band_cols)
        assert exc_info.value.error_code == "TIMESTAMP_UNPARSABLE"


# ──────────────────────────────────────────────
# TestComputeBaseline
# ──────────────────────────────────────────────


class TestComputeBaseline:
    def test_returns_dict_with_all_bands(self, simple_df, band_cols):
        """반환 dict의 키가 band_cols와 일치함"""
        result = compute_baseline(simple_df, band_cols)
        assert set(result.keys()) == set(band_cols)

    def test_mean_of_absolute_time_interval(self, simple_df, band_cols):
        """값이 절대시각 기준 첫 30초의 평균과 일치함"""
        result = compute_baseline(simple_df, band_cols, baseline_duration_sec=30)
        expected_alpha = float(simple_df["alpha"].iloc[:30].mean())
        assert abs(result["alpha"] - expected_alpha) < 1e-10

    def test_custom_baseline_duration(self, simple_df, band_cols):
        """baseline_duration_sec=10 지정 시 첫 10행 기준 평균 사용함"""
        result = compute_baseline(simple_df, band_cols, baseline_duration_sec=10)
        expected = float(simple_df["alpha"].iloc[:10].mean())
        assert abs(result["alpha"] - expected) < 1e-10

    def test_missing_band_col_excluded(self):
        """요청 대역이 누락되면 baseline coverage 오류가 발생함"""
        df = pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-01-01", periods=3, freq="s"),
                "alpha": [1.0, 2.0, 3.0],
            }
        )
        with pytest.raises(AnalysisContractError) as exc_info:
            compute_baseline(
                df,
                ["alpha", "nonexistent"],
                baseline_duration_sec=3,
            )
        assert exc_info.value.error_code == "BASELINE_COVERAGE_INSUFFICIENT"

    def test_values_are_float(self, simple_df, band_cols):
        """반환 dict의 모든 value가 float 타입임"""
        result = compute_baseline(simple_df, band_cols)
        for val in result.values():
            assert isinstance(val, float)


# ──────────────────────────────────────────────
# TestSplitStimulusWindows
# ──────────────────────────────────────────────


class TestSplitStimulusWindows:
    def test_returns_fixed_window_slots(self, full_session_df, band_cols):
        """반환값이 고정 식별자를 가진 WindowSlot 목록임"""
        result = split_stimulus_windows(full_session_df, band_cols)
        assert isinstance(result, list)
        assert isinstance(result[0], WindowSlot)
        assert isinstance(result[0].data, pd.DataFrame)

    def test_n_stimuli_length(self, full_session_df, band_cols):
        """슬롯 수가 n_stimuli × n_windows와 일치함"""
        result = split_stimulus_windows(full_session_df, band_cols, n_stimuli=10)
        assert len(result) == 60

    def test_n_windows_per_stimulus(self, full_session_df, band_cols):
        """데이터 충분할 때 자극별 고정 window 식별자가 올바름"""
        result = split_stimulus_windows(
            full_session_df, band_cols, stimulus_duration_sec=60, window_size_sec=10
        )
        assert [(slot.stim_idx, slot.win_idx) for slot in result[:6]] == [
            (0, 0),
            (0, 1),
            (0, 2),
            (0, 3),
            (0, 4),
            (0, 5),
        ]

    def test_window_row_count(self, full_session_df, band_cols):
        """각 window DataFrame의 행 수가 window_size_sec과 일치함"""
        result = split_stimulus_windows(full_session_df, band_cols, window_size_sec=10)
        assert result[0].data is not None
        assert len(result[0].data) == 10

    def test_baseline_rows_excluded(self, full_session_df, band_cols):
        """첫 윈도우가 공통 시작시각의 baseline 종료 후 시작함"""
        result = split_stimulus_windows(
            full_session_df, band_cols, baseline_duration_sec=30
        )
        assert result[0].window_start == pd.Timestamp("2026-01-01 00:00:30")
        assert result[0].window_end == pd.Timestamp("2026-01-01 00:00:40")
        assert result[0].data is not None
        assert result[0].data["timestamp"].min() == result[0].window_start

    def test_short_data_partial_windows(self, band_cols):
        """데이터가 짧아도 후속 슬롯 ID가 유지되고 data만 None이 됨"""
        short_df = pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-01-01", periods=90, freq="s"),
                **{col: np.random.uniform(0.1, 1.0, 90) for col in band_cols},
            }
        )
        result = split_stimulus_windows(
            short_df,
            band_cols,
            stimulus_duration_sec=60,
            window_size_sec=10,
            n_stimuli=3,
            baseline_duration_sec=30,
        )
        assert len(result) == 18
        assert all(slot.data is not None for slot in result[:6])
        assert all(slot.data is None for slot in result[6:])
        assert result[-1].stim_idx == 2
        assert result[-1].win_idx == 5

    def test_window_cols_match_band_cols(self, full_session_df, band_cols):
        """각 window DataFrame이 timestamp와 band_cols를 보존함"""
        result = split_stimulus_windows(full_session_df, band_cols)
        assert result[0].data is not None
        assert list(result[0].data.columns) == ["timestamp", *band_cols]

    def test_index_reset_in_each_window(self, full_session_df, band_cols):
        """각 window DataFrame의 index가 0부터 시작함"""
        slots = split_stimulus_windows(full_session_df, band_cols)
        for slot in slots:
            assert slot.data is not None
            assert slot.data.index[0] == 0

    def test_boundary_sample_belongs_to_next_window(self, band_cols):
        """경계 시각 샘플이 앞이 아니라 다음 반개구간에 포함됨"""
        df = pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-01-01", periods=50, freq="s"),
                **{band: np.arange(50, dtype=float) for band in band_cols},
            }
        )
        slots = split_stimulus_windows(
            df,
            band_cols,
            stimulus_duration_sec=20,
            window_size_sec=10,
            n_stimuli=1,
            baseline_duration_sec=30,
        )
        assert slots[0].data is not None
        assert slots[1].data is not None
        boundary = pd.Timestamp("2026-01-01 00:00:40")
        assert boundary not in set(slots[0].data["timestamp"])
        assert boundary in set(slots[1].data["timestamp"])


# ──────────────────────────────────────────────
# TestExtractFeatures
# ──────────────────────────────────────────────


class TestExtractFeatures:
    def _make_windows(self, n_stim=2, n_win=2, band_cols=None):
        """테스트용 windows 구조 생성함"""
        if band_cols is None:
            band_cols = DEFAULT_BAND_COLS
        windows = []
        origin = pd.Timestamp("2026-01-01")
        for s in range(n_stim):
            for w in range(n_win):
                data = {
                    "timestamp": pd.date_range(
                        origin + pd.Timedelta(seconds=(s * n_win + w) * 10),
                        periods=10,
                        freq="s",
                    ),
                    **{
                        band: [float(s + w + i) * 0.1 for i in range(10)]
                        for band in band_cols
                    },
                }
                window_start = origin + pd.Timedelta(seconds=(s * n_win + w) * 10)
                windows.append(
                    WindowSlot(
                        stim_idx=s,
                        win_idx=w,
                        window_start=window_start,
                        window_end=window_start + pd.Timedelta(seconds=10),
                        data=pd.DataFrame(data),
                    )
                )
        return windows

    def test_key_naming_convention(self, band_cols):
        """키 형식이 s{N}_w{N}_{band} 패턴임"""
        windows = self._make_windows(1, 1, band_cols)
        result = extract_features(windows, band_cols)
        for key in result.keys():
            parts = key.split("_")
            assert parts[0].startswith("s")
            assert parts[1].startswith("w")
            assert "_".join(parts[2:]) in band_cols

    def test_feature_count_small(self, band_cols):
        """1 stimulus × 1 window × 4 bands → feature 수 = 4"""
        windows = self._make_windows(1, 1, band_cols)
        result = extract_features(windows, band_cols)
        assert len(result) == 4

    def test_full_feature_count(self, band_cols):
        """2 stimulus × 2 windows × 4 bands → feature 수 = 16"""
        windows = self._make_windows(2, 2, band_cols)
        result = extract_features(windows, band_cols)
        assert len(result) == 16

    def test_no_baseline_uses_mean(self, band_cols):
        """baseline=None 시 feature값이 window 평균과 동일함"""
        windows = self._make_windows(1, 1, band_cols)
        result = extract_features(windows, band_cols, baseline=None)
        assert windows[0].data is not None
        expected = float(windows[0].data["alpha"].mean())
        assert abs(result["s1_w1_alpha"] - expected) < 1e-10

    def test_baseline_subtraction(self, band_cols):
        """baseline 제공 시 feature = window_mean - baseline[band]"""
        windows = self._make_windows(1, 1, band_cols)
        baseline = {"alpha": 0.5, "beta": 0.5, "theta": 0.5, "gamma": 0.5}
        result = extract_features(windows, band_cols, baseline=baseline)
        assert windows[0].data is not None
        expected = float(windows[0].data["alpha"].mean()) - 0.5
        assert abs(result["s1_w1_alpha"] - expected) < 1e-10

    def test_returns_ordered_dict(self, band_cols):
        """반환 타입이 OrderedDict임"""
        windows = self._make_windows(1, 1, band_cols)
        result = extract_features(windows, band_cols)
        assert isinstance(result, OrderedDict)

    def test_missing_band_in_window_skipped(self):
        """window DataFrame에 없는 band는 feature에서 건너뜀"""
        origin = pd.Timestamp("2026-01-01")
        windows = [
            WindowSlot(
                stim_idx=0,
                win_idx=0,
                window_start=origin,
                window_end=origin + pd.Timedelta(seconds=2),
                data=pd.DataFrame({"alpha": [1.0, 2.0]}),
            )
        ]
        result = extract_features(windows, ["alpha", "nonexistent"])
        assert "s1_w1_alpha" in result
        assert "s1_w1_nonexistent" not in result

    def test_none_slot_keeps_later_window_label(self, band_cols):
        """중간 결측 슬롯 뒤 feature 라벨이 당겨지지 않음"""
        windows = self._make_windows(1, 3, band_cols)
        windows[1] = WindowSlot(
            stim_idx=0,
            win_idx=1,
            window_start=windows[1].window_start,
            window_end=windows[1].window_end,
            data=None,
        )
        result = extract_features(windows, band_cols)
        assert "s1_w2_alpha" not in result
        assert "s1_w3_alpha" in result


# ──────────────────────────────────────────────
# TestBuildPairFeatures
# ──────────────────────────────────────────────


class TestBuildPairFeatures:
    def test_a_prefix_applied(self, sample_features):
        """교집합 features_a의 키에 'a_' 접두사가 붙음"""
        result = build_pair_features(sample_features, sample_features)
        for key in sample_features:
            assert f"a_{key}" in result

    def test_b_prefix_applied(self, sample_features):
        """교집합 features_b의 키에 'b_' 접두사가 붙음"""
        result = build_pair_features(sample_features, sample_features)
        for key in sample_features:
            assert f"b_{key}" in result

    def test_total_key_count(self, sample_features):
        """결과 dict 키 수 = len(features_a) + len(features_b)"""
        result = build_pair_features(sample_features, sample_features)
        assert len(result) == len(sample_features) * 2

    def test_values_preserved(self):
        """접두사 추가 후에도 원래 float 값과 동일함"""
        fa = {"s1_w1_alpha": 0.123}
        fb = {"s1_w1_alpha": 0.456}
        result = build_pair_features(fa, fb)
        assert result["a_s1_w1_alpha"] == 0.123
        assert result["b_s1_w1_alpha"] == 0.456

    def test_empty_features_allowed(self):
        """빈 dict 입력 시 빈 dict 반환 (오류 없음)"""
        result = build_pair_features({}, {})
        assert result == {}

    def test_a_keys_before_b_keys(self, sample_features):
        """OrderedDict에서 a_ 키가 b_ 키보다 먼저 나옴"""
        result = build_pair_features(sample_features, sample_features)
        keys = list(result.keys())
        a_end = max(i for i, k in enumerate(keys) if k.startswith("a_"))
        b_start = min(i for i, k in enumerate(keys) if k.startswith("b_"))
        assert a_end < b_start

    def test_one_sided_slot_excluded_from_both_subjects(self):
        """한쪽에만 있는 슬롯은 pair 양쪽에서 모두 제외됨"""
        features_a = {"s2_w3_alpha": 0.1, "s2_w4_alpha": 0.2}
        features_b = {"s2_w4_alpha": 0.3}
        result = build_pair_features(features_a, features_b)
        assert "a_s2_w3_alpha" not in result
        assert "b_s2_w3_alpha" not in result
        assert "a_s2_w4_alpha" in result
        assert "b_s2_w4_alpha" in result


# ──────────────────────────────────────────────
# TestComputeY
# ──────────────────────────────────────────────


class TestComputeY:
    def test_positive_difference(self):
        """abs(7.5 - 6.0) = 1.5"""
        assert compute_y(7.5, 6.0) == 1.5

    def test_reversed_order_same_result(self):
        """abs(6.0 - 7.5) = 1.5 (순서 무관)"""
        assert compute_y(6.0, 7.5) == 1.5

    def test_same_score_returns_zero(self):
        """abs(5.0 - 5.0) = 0.0"""
        assert compute_y(5.0, 5.0) == 0.0

    def test_returns_float(self):
        """반환 타입이 float임"""
        result = compute_y(3.0, 1.0)
        assert isinstance(result, float)


# ──────────────────────────────────────────────
# TestRunFullPipeline
# ──────────────────────────────────────────────


class TestRunFullPipeline:
    @pytest.fixture(autouse=True)
    def _mock_io(self, monkeypatch, full_session_df):
        """CSV I/O와 MindSignalAnalyzer를 mock함"""
        monkeypatch.setattr(
            "server.services.analysis.find_csv_files",
            lambda group_id, idx: [Path(f"/fake/subject_{idx}_{group_id}.csv")],
        )
        monkeypatch.setattr(
            "server.services.analysis.load_session_data",
            lambda path: full_session_df.copy(),
        )
        mock_analyzer = MagicMock()
        mock_analyzer.calculate_synchrony.return_value = 0.75
        monkeypatch.setattr(
            "server.services.analysis.MindSignalAnalyzer",
            lambda: mock_analyzer,
        )

    def test_returns_expected_keys(self):
        """반환 dict에 필수 키 존재함"""
        result = run_full_pipeline(TEST_GROUP_ID, [1, 2])
        expected_keys = {
            "group_id",
            "subjects",
            "pair_features",
            "y_score",
            "synchrony_score",
            "friendship_score",
            "score_params",
            "pipeline_params",
            "dataframes",
        }
        assert expected_keys == set(result.keys())

    def test_feature_count_matches_params(self):
        """subjects[i]['n_features'] == n_stimuli * n_windows * len(band_cols)"""
        result = run_full_pipeline(
            TEST_GROUP_ID,
            [1, 2],
            n_stimuli=10,
            window_size_sec=10,
            stimulus_duration_sec=60,
        )
        expected_count = 10 * 6 * 4  # n_stimuli × n_windows × n_bands
        assert result["subjects"][0]["n_features"] == expected_count

    def test_with_satisfaction_scores(self):
        """satisfaction_scores 제공 시 y_score가 float임"""
        result = run_full_pipeline(
            TEST_GROUP_ID,
            [1, 2],
            satisfaction_scores={1: 7.5, 2: 6.0},
        )
        assert isinstance(result["y_score"], float)
        assert result["y_score"] == 1.5

    def test_without_satisfaction_scores(self):
        """satisfaction_scores=None 시 y_score도 None임"""
        result = run_full_pipeline(TEST_GROUP_ID, [1, 2])
        assert result["y_score"] is None

    def test_pair_features_with_two_subjects(self):
        """subject 2명일 때 pair_features 존재함"""
        result = run_full_pipeline(TEST_GROUP_ID, [1, 2])
        assert result["pair_features"] is not None
        # a_ 키와 b_ 키 모두 존재해야 함
        keys = list(result["pair_features"].keys())
        assert any(k.startswith("a_") for k in keys)
        assert any(k.startswith("b_") for k in keys)

    def test_csv_not_found(self, monkeypatch):
        """CSV 미존재 subject는 error 키 포함함"""
        monkeypatch.setattr(
            "server.services.analysis.find_csv_files",
            lambda group_id, idx: [],  # 빈 리스트 반환
        )
        result = run_full_pipeline(TEST_GROUP_ID, [1])
        assert "error" in result["subjects"][0]

    def test_band_cols_default(self):
        """band_cols=None 시 기본값 사용됨"""
        result = run_full_pipeline(TEST_GROUP_ID, [1, 2])
        assert result["pipeline_params"]["band_cols"] == [
            "alpha",
            "beta",
            "theta",
            "gamma",
        ]

    def test_synchrony_score_mocked(self):
        """MindSignalAnalyzer.calculate_synchrony Mock → 0.75 반환함"""
        result = run_full_pipeline(TEST_GROUP_ID, [1, 2])
        assert result["synchrony_score"] == 0.75

    def test_subjects_with_39_second_offset_use_same_absolute_window(
        self,
        monkeypatch,
    ):
        """39초 늦게 시작한 subject를 공통 절대시각 윈도우로 정렬함"""
        absolute_origin = pd.Timestamp("2026-01-01")

        def make_frame(start_second, seconds):
            timestamps = pd.date_range(
                absolute_origin + pd.Timedelta(seconds=start_second),
                periods=seconds,
                freq="s",
            )
            values = (timestamps - absolute_origin).total_seconds().astype(float)
            return pd.DataFrame(
                {
                    "timestamp": timestamps,
                    "alpha": values,
                }
            )

        frames = {
            1: make_frame(39, 101),
            2: make_frame(0, 140),
        }
        monkeypatch.setattr(
            "server.services.analysis.load_session_data",
            lambda path: frames[1 if "subject_1_" in str(path) else 2].copy(),
        )
        result = run_full_pipeline(
            TEST_GROUP_ID,
            [1, 2],
            stimulus_duration_sec=20,
            window_size_sec=10,
            n_stimuli=1,
            baseline_duration_sec=10,
            band_cols=["alpha"],
        )
        subject_a, subject_b = result["subjects"]
        assert subject_a["features"] == subject_b["features"]
        assert subject_a["features"]["s1_w1_alpha"] == 10.0

    def test_common_end_trims_later_subject_and_keeps_feature_counts_symmetric(
        self,
        monkeypatch,
    ):
        """한쪽 조기 종료 시 common_end 이후 양쪽 feature를 생성하지 않음"""

        def make_frame(seconds):
            return pd.DataFrame(
                {
                    "timestamp": pd.date_range(
                        "2026-01-01",
                        periods=seconds,
                        freq="s",
                    ),
                    "alpha": np.arange(seconds, dtype=float),
                }
            )

        frames = {1: make_frame(70), 2: make_frame(100)}
        monkeypatch.setattr(
            "server.services.analysis.load_session_data",
            lambda path: frames[1 if "subject_1_" in str(path) else 2].copy(),
        )
        result = run_full_pipeline(
            TEST_GROUP_ID,
            [1, 2],
            stimulus_duration_sec=20,
            window_size_sec=10,
            n_stimuli=4,
            baseline_duration_sec=10,
            band_cols=["alpha"],
        )
        counts = [subject["n_features"] for subject in result["subjects"]]
        assert counts == [6, 6]
        assert all(
            "s4_" not in key
            for subject in result["subjects"]
            for key in subject["features"]
        )

    def test_one_sided_internal_gap_is_excluded_from_pair(
        self,
        monkeypatch,
    ):
        """한쪽 s2_w3 coverage 미달 시 pair 양쪽 슬롯을 모두 제외함"""
        timestamps = pd.date_range("2026-01-01", periods=70, freq="s")
        base = pd.DataFrame(
            {
                "timestamp": timestamps,
                "alpha": np.arange(70, dtype=float),
            }
        )
        frames = {
            1: base.drop(index=range(60, 66)).reset_index(drop=True),
            2: base,
        }
        monkeypatch.setattr(
            "server.services.analysis.load_session_data",
            lambda path: frames[1 if "subject_1_" in str(path) else 2].copy(),
        )
        result = run_full_pipeline(
            TEST_GROUP_ID,
            [1, 2],
            stimulus_duration_sec=30,
            window_size_sec=10,
            n_stimuli=2,
            baseline_duration_sec=10,
            band_cols=["alpha"],
        )
        assert "s2_w3_alpha" not in result["subjects"][0]["features"]
        assert "s2_w3_alpha" in result["subjects"][1]["features"]
        assert "a_s2_w3_alpha" not in result["pair_features"]
        assert "b_s2_w3_alpha" not in result["pair_features"]

    def test_no_usable_csv_preserves_partial_response(self, monkeypatch):
        """usable CSV 0개면 subject별 오류와 pair None을 반환함"""
        monkeypatch.setattr(
            "server.services.analysis.find_csv_files",
            lambda group_id, idx: [],
        )
        result = run_full_pipeline(TEST_GROUP_ID, [1, 2])
        assert [subject["error"] for subject in result["subjects"]] == [
            "CSV 파일 미발견",
            "CSV 파일 미발견",
        ]
        assert result["pair_features"] is None
        assert result["synchrony_score"] is None

    def test_subject_contract_error_is_isolated_to_that_subject(
        self,
        monkeypatch,
    ):
        """한 subject의 timestamp 계약 위반이 다른 subject 분석을 막지 않음"""
        healthy = pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-01-01", periods=30, freq="s"),
                "alpha": np.ones(30),
            }
        )
        broken = pd.DataFrame({"alpha": np.ones(30)})
        monkeypatch.setattr(
            "server.services.analysis.load_session_data",
            lambda path: (
                broken.copy() if "subject_1_" in str(path) else healthy.copy()
            ),
        )
        result = run_full_pipeline(
            TEST_GROUP_ID,
            [1, 2],
            stimulus_duration_sec=10,
            window_size_sec=10,
            n_stimuli=1,
            baseline_duration_sec=10,
            band_cols=["alpha"],
        )
        failed, healthy_subject = result["subjects"]
        assert failed["error_code"] == "TIMESTAMP_COLUMN_MISSING"
        assert healthy_subject["n_features"] == 1
        assert result["pair_features"] is None

    def test_baseline_mean_uses_complete_observations_only(self):
        """coverage 판정에서 제외한 불완전 관측 초는 평균에도 포함하지 않음"""
        df = pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-01-01", periods=10, freq="s"),
                "alpha": [1.0] * 5 + [100.0] * 5,
                "beta": [1.0] * 5 + [np.nan] * 5,
            }
        )
        result = compute_baseline(df, ["alpha", "beta"], baseline_duration_sec=10)
        assert result == {"alpha": 1.0, "beta": 1.0}

    @pytest.mark.parametrize("bad_value", [np.inf, -np.inf, "n/a"])
    def test_non_finite_band_value_is_not_a_valid_observation(self, bad_value):
        """Inf와 수치 변환 실패는 결측으로 처리해 baseline에 섞이지 않음"""
        df = pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-01-01", periods=30, freq="s"),
                "alpha": [1.0] * 30,
            }
        )
        df["alpha"] = df["alpha"].astype(object)
        df.loc[0, "alpha"] = bad_value

        result = compute_baseline(df, ["alpha"], baseline_duration_sec=30)
        assert result["alpha"] == 1.0

        # 유효 관측이 임계 아래로 떨어지면 계약 오류로 거부함
        df.loc[1:, "alpha"] = bad_value
        with pytest.raises(AnalysisContractError) as exc_info:
            compute_baseline(df, ["alpha"], baseline_duration_sec=30)
        assert exc_info.value.error_code == "BASELINE_COVERAGE_INSUFFICIENT"

    def test_numeric_string_band_values_are_averaged_not_concatenated(self):
        """변환 가능한 숫자 문자열은 수치로 평균함

        마스크는 통과하지만 object Series의 mean()이 문자열을 이어붙여
        TypeError로 500이 나던 경로임(회귀 방지).
        """
        df = pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-01-01", periods=30, freq="s"),
                "alpha": ["1.0"] * 15 + [3.0] * 15,
            }
        )
        result = compute_baseline(df, ["alpha"], baseline_duration_sec=30)
        assert result["alpha"] == 2.0

    def test_non_positive_baseline_duration_is_rejected(self):
        """baseline 길이가 0 이하면 계약 오류를 발생시킴"""
        df = pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-01-01", periods=5, freq="s"),
                "alpha": np.ones(5),
            }
        )
        with pytest.raises(AnalysisContractError) as exc_info:
            compute_baseline(df, ["alpha"], baseline_duration_sec=0)
        assert exc_info.value.error_code == "BASELINE_COVERAGE_INSUFFICIENT"

    def test_one_usable_csv_uses_its_own_time_range(self, monkeypatch):
        """usable CSV 1개면 해당 파일 시각으로 분석하고 다른 subject 오류를 유지함"""
        monkeypatch.setattr(
            "server.services.analysis.find_csv_files",
            lambda group_id, idx: (
                [Path(f"/fake/subject_{idx}_{group_id}.csv")] if idx == 1 else []
            ),
        )
        result = run_full_pipeline(
            TEST_GROUP_ID,
            [1, 2],
            stimulus_duration_sec=20,
            window_size_sec=10,
            n_stimuli=1,
            baseline_duration_sec=10,
            band_cols=["alpha"],
        )
        assert result["subjects"][0]["n_features"] == 2
        assert result["subjects"][1]["error"] == "CSV 파일 미발견"
        assert result["pair_features"] is None

    def test_exact_baseline_length_is_valid(self, monkeypatch):
        """마지막 관측 초 + 1초 배타 경계로 정확한 baseline 길이를 인정함"""
        exact_df = pd.DataFrame(
            {
                "timestamp": pd.date_range(
                    "2026-01-01",
                    periods=30,
                    freq="s",
                ),
                "alpha": np.ones(30),
            }
        )
        monkeypatch.setattr(
            "server.services.analysis.load_session_data",
            lambda path: exact_df.copy(),
        )
        result = run_full_pipeline(
            TEST_GROUP_ID,
            [1],
            stimulus_duration_sec=10,
            window_size_sec=10,
            n_stimuli=1,
            baseline_duration_sec=30,
            band_cols=["alpha"],
        )
        assert result["subjects"][0]["baseline"]["alpha"] == 1.0
        assert result["subjects"][0]["n_features"] == 0


class TestCoverageContract:
    """baseline과 자극 윈도우 coverage 계약을 검증함"""

    @staticmethod
    def _baseline_frame(observations: int) -> pd.DataFrame:
        """60초 구간 안에 지정 개수의 완전한 관측 초를 생성함"""
        return pd.DataFrame(
            {
                "timestamp": pd.date_range(
                    "2026-01-01",
                    periods=observations,
                    freq="s",
                ),
                "alpha": np.ones(observations),
                "beta": np.ones(observations),
            }
        )

    def test_custom_60_second_baseline_requires_30_observations(self):
        """가변 60초 baseline의 최소 coverage가 30초로 계산됨"""
        valid = self._baseline_frame(30)
        result = compute_baseline(
            valid,
            ["alpha", "beta"],
            baseline_duration_sec=60,
        )
        assert result == {"alpha": 1.0, "beta": 1.0}

        invalid = self._baseline_frame(29)
        with pytest.raises(AnalysisContractError) as exc_info:
            compute_baseline(
                invalid,
                ["alpha", "beta"],
                baseline_duration_sec=60,
            )
        assert exc_info.value.error_code == "BASELINE_COVERAGE_INSUFFICIENT"

    def test_all_requested_bands_must_be_non_nan_in_same_second(self):
        """한 초의 요청 대역 중 하나가 NaN이면 완전한 관측 초로 세지 않음"""
        df = self._baseline_frame(30)
        df.loc[14:, "beta"] = np.nan
        with pytest.raises(AnalysisContractError) as exc_info:
            compute_baseline(
                df,
                ["alpha", "beta"],
                baseline_duration_sec=30,
            )
        assert exc_info.value.error_code == "BASELINE_COVERAGE_INSUFFICIENT"


LIVE_GROUP_ID = "6a508a6b1048b553eea41778"
LIVE_FILES = [
    (
        CSV_BASE_DIR / f"subject_{idx}_{LIVE_GROUP_ID}_20260710_150135.csv"
        if idx == 1
        else CSV_BASE_DIR / f"subject_{idx}_{LIVE_GROUP_ID}_20260710_150059.csv"
    )
    for idx in (1, 2)
]


@pytest.mark.skipif(
    not all(path.exists() for path in LIVE_FILES),
    reason="2026-07-10 라이브 CSV가 로컬에 없음",
)
def test_live_csv_regression_uses_symmetric_absolute_window_keys():
    """라이브 CSV에서 시작시각 차이와 무관하게 양쪽 윈도우 키를 정렬함"""
    result = run_full_pipeline(LIVE_GROUP_ID, [1, 2])
    feature_keys = [set(subject["features"].keys()) for subject in result["subjects"]]
    raw_frames = result["dataframes"]
    start_gap = abs(
        pd.to_datetime(raw_frames[1]["timestamp"]).min()
        - pd.to_datetime(raw_frames[2]["timestamp"]).min()
    ).total_seconds()
    assert start_gap > 30
    assert feature_keys[0] == feature_keys[1]
    assert len(result["pair_features"]) == len(feature_keys[0]) * 2


# ──────────────────────────────────────────────
# TestAnalyzePipelineSequential
# ──────────────────────────────────────────────


class TestAnalyzePipelineSequential:
    """SEQUENTIAL 모드 분기 analyze_pipeline_sequential 검증함"""

    @pytest.fixture
    def waves_df(self):
        """5대역 + EmotivMetrics 컬럼을 포함한 최소 측정 DataFrame 반환함"""
        np.random.seed(1)
        n = 60  # TRIM_START(15) + effective(30) + TRIM_END(15) 최소 충족
        data = {
            "delta": np.random.uniform(0.1, 1.0, n),
            "theta": np.random.uniform(0.1, 1.0, n),
            "alpha": np.random.uniform(0.1, 1.0, n),
            "beta": np.random.uniform(0.1, 1.0, n),
            "gamma": np.random.uniform(0.1, 1.0, n),
            "focus": np.random.uniform(0, 1, n),
            "engagement": np.random.uniform(0, 1, n),
            "interest": np.random.uniform(0, 1, n),
            "excitement": np.random.uniform(0, 1, n),
            "stress": np.random.uniform(0, 1, n),
            "relaxation": np.random.uniform(0, 1, n),
        }
        return pd.DataFrame(data)

    def test_sequential_returns_similarity_features(self, monkeypatch, waves_df):
        """SEQUENTIAL 모드 → similarity_features dict 반환함"""
        monkeypatch.setattr(
            "server.services.analysis.find_csv_files",
            lambda group_id, subject_index: [
                Path(f"/fake/subject_{subject_index}_{group_id}.csv")
            ],
        )
        monkeypatch.setattr(
            "server.services.analysis.load_session_data",
            lambda path: waves_df.copy(),
        )
        result = analyze_pipeline_sequential(TEST_GROUP_ID, subject_indices=[1, 2])
        assert isinstance(result["similarity_features"], dict)
        assert "similarity_score" in result["similarity_features"]

    def test_sequential_pair_features_is_none(self, monkeypatch, waves_df):
        """SEQUENTIAL 모드 → pair_features=None (DUAL 전용 필드)"""
        monkeypatch.setattr(
            "server.services.analysis.find_csv_files",
            lambda group_id, subject_index: [
                Path(f"/fake/subject_{subject_index}_{group_id}.csv")
            ],
        )
        monkeypatch.setattr(
            "server.services.analysis.load_session_data",
            lambda path: waves_df.copy(),
        )
        result = analyze_pipeline_sequential(TEST_GROUP_ID, subject_indices=[1, 2])
        assert result["pair_features"] is None

    def test_sequential_csv_not_found_raises_value_error(self, monkeypatch):
        """subject 첫 번째 인덱스 CSV 없을 때 ValueError 발생함"""
        monkeypatch.setattr(
            "server.services.analysis.find_csv_files",
            lambda group_id, subject_index: [],
        )
        with pytest.raises(ValueError, match="CSV 미발견"):
            analyze_pipeline_sequential(TEST_GROUP_ID, subject_indices=[1, 2])

    def test_sequential_csv_not_found_subject2_raises_value_error(
        self, monkeypatch, waves_df
    ):
        """subject 1은 CSV 있지만 subject 2 CSV 없을 때 ValueError 발생함"""

        def mock_find(group_id, subject_index):
            if subject_index == 1:
                return [Path(f"/fake/subject_1_{group_id}.csv")]
            return []

        monkeypatch.setattr(
            "server.services.analysis.find_csv_files",
            mock_find,
        )
        monkeypatch.setattr(
            "server.services.analysis.load_session_data",
            lambda path: waves_df.copy(),
        )
        with pytest.raises(ValueError, match="CSV 미발견"):
            analyze_pipeline_sequential(TEST_GROUP_ID, subject_indices=[1, 2])

    def test_sequential_uses_provided_indices_not_hardcoded(
        self, monkeypatch, waves_df
    ):
        """subject_indices=[5, 7] → 실제 5, 7 인덱스로 CSV 탐색 수행함"""
        called_indices = []

        def mock_find(group_id, subject_index):
            called_indices.append(subject_index)
            return [Path(f"/fake/subject_{subject_index}_{group_id}.csv")]

        monkeypatch.setattr(
            "server.services.analysis.find_csv_files",
            mock_find,
        )
        monkeypatch.setattr(
            "server.services.analysis.load_session_data",
            lambda path: waves_df.copy(),
        )
        result = analyze_pipeline_sequential(TEST_GROUP_ID, subject_indices=[5, 7])
        assert 5 in called_indices
        assert 7 in called_indices
        assert 1 not in called_indices
        assert 2 not in called_indices
        # 반환된 subjects 인덱스도 5, 7이어야 함
        subject_idx_in_result = [s["subject_index"] for s in result["subjects"]]
        assert subject_idx_in_result == [5, 7]

    def test_sequential_single_index_raises_value_error(self, monkeypatch):
        """subject_indices=[1] (길이 != 2) → ValueError 발생함"""
        monkeypatch.setattr(
            "server.services.analysis.find_csv_files",
            lambda group_id, subject_index: [],
        )
        with pytest.raises(
            ValueError,
            match="SEQUENTIAL mode requires exactly 2 subject_indices",
        ):
            analyze_pipeline_sequential(TEST_GROUP_ID, subject_indices=[1])

    def test_dual_regression_pair_features_not_none(self, monkeypatch, full_session_df):
        """DUAL 모드 기존 run_full_pipeline 동작 유지 — pair_features 존재함"""
        monkeypatch.setattr(
            "server.services.analysis.find_csv_files",
            lambda group_id, idx: [Path(f"/fake/subject_{idx}_{group_id}.csv")],
        )
        monkeypatch.setattr(
            "server.services.analysis.load_session_data",
            lambda path: full_session_df.copy(),
        )
        mock_analyzer = MagicMock()
        mock_analyzer.calculate_synchrony.return_value = 0.75
        monkeypatch.setattr(
            "server.services.analysis.MindSignalAnalyzer",
            lambda: mock_analyzer,
        )
        result = run_full_pipeline(TEST_GROUP_ID, [1, 2])
        # DUAL 모드: pair_features 반드시 존재함 (회귀 검증)
        assert result["pair_features"] is not None
