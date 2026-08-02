"""compute_synchrony 시간축 정렬 회귀 검증.

회귀 배경(2026-07-10 라이브 측정, groupId 6a508a6b1048b553eea41778):
    compute_synchrony가 CSV의 timestamp 컬럼을 무시하고 각 스트림의 0번 행끼리
    맞대어(.values[:min_len]) 상관을 계산함. 두 피실험자의 측정 시작이 38.3초
    어긋나 고정 시차가 그대로 상관에 섞임.
    실측: 위치 정렬 r=-0.12738, 시간축 정렬 r=+0.05943 (부호까지 뒤집힘).

라이브 CSV의 정확한 float를 박제하지 않음. 이유:
    (1) 레포 밖 Team-project/csv/ 파일 의존이라 CI에서 부재
    (2) pandas 버전과 부동소수에 민감
    대신 합성 픽스처로 정렬 로직을 결정적으로 검증하고, 부호와 범위만 단언함.
"""

import numpy as np
import pandas as pd
import pytest

from server.services.analysis import (
    TRIM_END_SECONDS,
    TRIM_START_SECONDS,
    align_on_second,
    compute_synchrony,
)
from server.services.score_params import ScoreParams

# 이 픽스처는 alpha 열만 만들므로 정본 대상(Pz 감마) 대신 공간평균 alpha를
# 명시 주입함. 검증 대상이 지표 선택이 아니라 시간축 정렬 로직이기 때문임
ALPHA_PARAMS = ScoreParams(sync_channels=(), sync_band="alpha")


def _make_df(start: str, n: int, alpha: np.ndarray) -> pd.DataFrame:
    """초당 1행, 지정 시각부터 시작하는 시계열 생성함"""
    ts = pd.date_range(start=start, periods=n, freq="1s")
    return pd.DataFrame({"timestamp": ts, "alpha": alpha})


def test_align_on_second_removes_start_offset():
    """시작 시각이 어긋나도 같은 절대시각끼리 정렬됨."""
    wave = np.sin(np.linspace(0, 8 * np.pi, 100))
    a = _make_df("2026-07-10 15:00:00", 100, wave)
    # b는 30초 늦게 시작하지만 같은 절대시각에는 같은 값을 가짐
    b = _make_df("2026-07-10 15:00:30", 70, wave[30:])

    aligned = align_on_second(a, b, "alpha")

    assert len(aligned) == 70
    # 같은 시각의 값이 정확히 일치해야 함
    np.testing.assert_allclose(aligned["alpha_1"], aligned["alpha_2"])


def test_align_on_second_returns_none_without_timestamp():
    """timestamp 컬럼이 없으면 None 반환해 호출부가 폴백하게 함."""
    a = pd.DataFrame({"alpha": [1.0, 2.0, 3.0]})
    b = pd.DataFrame({"alpha": [1.0, 2.0, 3.0]})
    assert align_on_second(a, b, "alpha") is None


def test_positional_alignment_would_report_spurious_correlation():
    """위치 정렬(구 동작)은 시차를 상관으로 오인함 — 회귀 재현.

    동일 신호를 시간축에서 30초 밀면 진짜 상관은 1.0이어야 하나,
    행 인덱스로 맞대면 위상이 어긋나 1.0이 아님.
    """
    wave = np.sin(np.linspace(0, 8 * np.pi, 130))
    a = _make_df("2026-07-10 15:00:00", 130, wave)
    b = _make_df("2026-07-10 15:00:30", 100, wave[30:])

    trim = TRIM_START_SECONDS + TRIM_END_SECONDS
    assert len(b) - trim >= 10  # 픽스처가 유효 구간을 남기는지 확인함

    # 수정 후: 절대시각 정렬이라 완전 상관
    rho, meta = compute_synchrony(a, b, ALPHA_PARAMS)
    assert rho == pytest.approx(1.0, abs=1e-9)
    assert meta["sync_columns_used"] == ["alpha"]

    # 구 동작(위치 정렬)을 직접 재현하면 1.0이 아님
    t1 = a.iloc[TRIM_START_SECONDS : len(a) - TRIM_END_SECONDS].reset_index(drop=True)
    t2 = b.iloc[TRIM_START_SECONDS : len(b) - TRIM_END_SECONDS].reset_index(drop=True)
    n = min(len(t1), len(t2))
    positional = np.corrcoef(t1["alpha"].values[:n], t2["alpha"].values[:n])[0, 1]
    assert abs(positional - 1.0) > 0.1


def test_returns_none_when_overlap_too_short():
    """겹치는 구간이 10초 미만이면 None 반환함."""
    wave = np.random.default_rng(0).normal(size=60)
    a = _make_df("2026-07-10 15:00:00", 60, wave)
    b = _make_df("2026-07-10 15:10:00", 60, wave)  # 겹침 없음
    rho, _ = compute_synchrony(a, b, ALPHA_PARAMS)
    assert rho is None


def test_duplicate_seconds_are_averaged_not_duplicated():
    """같은 초에 여러 행이 오면 평균 집계해 1:1 정렬 유지함."""
    ts = pd.to_datetime(
        [
            "2026-07-10 15:00:00.100",
            "2026-07-10 15:00:00.600",
            "2026-07-10 15:00:01.200",
        ]
    )
    a = pd.DataFrame({"timestamp": ts, "alpha": [1.0, 3.0, 5.0]})
    b = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2026-07-10 15:00:00.000", "2026-07-10 15:00:01.000"]
            ),
            "alpha": [10.0, 20.0],
        }
    )

    aligned = align_on_second(a, b, "alpha")

    assert len(aligned) == 2
    # 15:00:00 초에 1.0과 3.0이 있었으므로 평균 2.0
    assert aligned.loc[aligned["sec"].dt.second == 0, "alpha_1"].iloc[0] == 2.0
