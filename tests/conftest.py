"""파이프라인 테스트 공통 fixture 정의함"""

from collections import OrderedDict

import numpy as np
import pandas as pd
import pytest

# ──────────────────────────────────────────────
# 테스트 상수
# ──────────────────────────────────────────────
TEST_SECRET = "test-secret"
TEST_GROUP_ID = "grp_test"
DEFAULT_BAND_COLS = ["alpha", "beta", "theta", "gamma"]
BASELINE_ROWS = 30
STIMULUS_DURATION = 60
N_STIMULI = 10
WINDOW_SIZE = 10
N_WINDOWS = 6  # 60 // 10
FULL_SESSION_ROWS = BASELINE_ROWS + (N_STIMULI * STIMULUS_DURATION)  # 630


@pytest.fixture
def band_cols():
    """기본 테스트용 대역 컬럼 목록 반환함"""
    return DEFAULT_BAND_COLS.copy()


@pytest.fixture
def simple_df():
    """1초 간격 timestamp와 band_cols를 포함한 100행 DataFrame 반환함"""
    np.random.seed(42)
    data = {
        "timestamp": pd.date_range("2026-01-01", periods=100, freq="s"),
        "alpha": np.random.uniform(0.1, 1.0, 100),
        "beta": np.random.uniform(0.1, 1.0, 100),
        "theta": np.random.uniform(0.1, 1.0, 100),
        "gamma": np.random.uniform(0.1, 1.0, 100),
    }
    return pd.DataFrame(data)


@pytest.fixture
def timestamped_df():
    """'timestamp' 컬럼 포함 — 동일 timestamp에 복수 행 존재하는 DataFrame 반환함"""
    np.random.seed(42)
    # 각 timestamp에 3개 샘플씩 30개 timestamp = 90행
    timestamps = np.repeat(
        pd.date_range("2026-01-01", periods=30, freq="s").values,
        3,
    )
    data = {
        "timestamp": timestamps,
        "alpha": np.random.uniform(0.1, 1.0, 90),
        "beta": np.random.uniform(0.1, 1.0, 90),
        "theta": np.random.uniform(0.1, 1.0, 90),
        "gamma": np.random.uniform(0.1, 1.0, 90),
    }
    return pd.DataFrame(data)


@pytest.fixture
def full_session_df():
    """전체 파이프라인 실행 가능한 630행 DataFrame 반환함

    baseline(30행) + stimulus×10(60행×10) = 630행
    """
    np.random.seed(42)
    data = {
        "timestamp": pd.date_range(
            "2026-01-01",
            periods=FULL_SESSION_ROWS,
            freq="s",
        ),
        "alpha": np.random.uniform(0.1, 1.0, FULL_SESSION_ROWS),
        "beta": np.random.uniform(0.1, 1.0, FULL_SESSION_ROWS),
        "theta": np.random.uniform(0.1, 1.0, FULL_SESSION_ROWS),
        "gamma": np.random.uniform(0.1, 1.0, FULL_SESSION_ROWS),
    }
    return pd.DataFrame(data)


@pytest.fixture
def sample_features():
    """테스트용 feature dict 반환함 (2 stimuli × 2 windows × 4 bands = 16개)"""
    features = OrderedDict()
    for s in range(1, 3):
        for w in range(1, 3):
            for band in DEFAULT_BAND_COLS:
                features[f"s{s}_w{w}_{band}"] = float(s * 0.1 + w * 0.01)
    return features


@pytest.fixture
def valid_pipeline_result():
    """엔드포인트 테스트용 run_full_pipeline 반환값 구조 반환함"""
    return {
        "group_id": TEST_GROUP_ID,
        "subjects": [
            {
                "subject_index": 1,
                "baseline": {"alpha": 0.5, "beta": 0.4, "theta": 0.3, "gamma": 0.2},
                "features": {"s1_w1_alpha": 0.1, "s1_w1_beta": 0.2},
                "n_features": 2,
            },
            {
                "subject_index": 2,
                "baseline": {"alpha": 0.6, "beta": 0.5, "theta": 0.4, "gamma": 0.3},
                "features": {"s1_w1_alpha": 0.15, "s1_w1_beta": 0.25},
                "n_features": 2,
            },
        ],
        "pair_features": {"a_s1_w1_alpha": 0.1, "b_s1_w1_alpha": 0.15},
        "y_score": 1.5,
        "synchrony_score": 0.75,
        "pipeline_params": {
            "stimulus_duration_sec": STIMULUS_DURATION,
            "window_size_sec": WINDOW_SIZE,
            "n_stimuli": N_STIMULI,
            "baseline_duration_sec": BASELINE_ROWS,
            "band_cols": DEFAULT_BAND_COLS,
            "n_windows_per_stimulus": N_WINDOWS,
            "total_features_per_subject": (
                N_STIMULI * N_WINDOWS * len(DEFAULT_BAND_COLS)
            ),
        },
        "dataframes": {},
    }


@pytest.fixture
def pipeline_secret_header():
    """엔드포인트 테스트용 secret 헤더 반환함"""
    return {"X-Engine-Secret": TEST_SECRET}


@pytest.fixture
def test_client():
    """FastAPI TestClient 반환함 (secret_key를 test-secret으로 패치)"""
    from server.config import settings

    # settings 속성을 테스트용으로 오버라이드함
    original_secret = settings.engine_secret_key
    original_mode = settings.registration_mode
    settings.engine_secret_key = TEST_SECRET
    settings.registration_mode = "local"  # 테스트는 ngrok tunnel 개방 금지

    from fastapi.testclient import TestClient

    from server.app import app

    client = TestClient(app)
    yield client

    # 원래 값 복원함
    settings.engine_secret_key = original_secret
    settings.registration_mode = original_mode
