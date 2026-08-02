import logging
from collections import OrderedDict
from dataclasses import dataclass
from math import ceil
from pathlib import Path

import numpy as np
import pandas as pd

from core.analyzer import MindSignalAnalyzer
from server.services.friendship import compute_friendship_score
from server.services.score_params import ScoreParams

logger = logging.getLogger(__name__)

# CSV 저장 기본 경로 (streamer.py와 동일한 위치)
CSV_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "csv"

# 구간 상수의 정본은 ScoreParams임. 아래 세 이름은 하위 호환 별칭이며
# **import 시점 스냅샷**이라 이후의 환경변수 변경을 반영하지 않음.
# 런타임 값이 필요하면 ScoreParams.from_env()를 호출할 것
_DEFAULT_PARAMS = ScoreParams.from_env()

# 측정 진정 구간 — 분석 제외, 표시만 함
TRIM_START_SECONDS = _DEFAULT_PARAMS.trim_start_sec
TRIM_END_SECONDS = _DEFAULT_PARAMS.trim_end_sec

# 최소 분석 가능 시간 (trimming 후 유효 구간 기준, 임시값 — 교수 확인 후 확정).
# 실제 필요 측정 시간은 여기에 앞뒤 trim을 더한 ScoreParams.required_total_sec임
MIN_ANALYSIS_SECONDS = _DEFAULT_PARAMS.min_analysis_sec


def _resolve_params(params: ScoreParams | None) -> ScoreParams:
    """생략된 파라미터를 모듈 기본값으로 채움"""
    return params if params is not None else _DEFAULT_PARAMS


class AnalysisContractError(Exception):
    """분석 입력 계약 위반을 나타내는 예외임"""

    def __init__(self, error_code: str, detail: str) -> None:
        """구조화된 오류 코드와 설명을 저장함"""
        super().__init__(detail)
        self.error_code = error_code
        self.detail = detail


@dataclass(frozen=True)
class WindowSlot:
    """자극 윈도우의 고정 식별자와 절대시각 구간을 보관함"""

    stim_idx: int
    win_idx: int
    window_start: pd.Timestamp
    window_end: pd.Timestamp
    data: pd.DataFrame | None


def classify_session_tier(total_samples: int, params: ScoreParams | None = None) -> str:
    """측정 시간 기반 세션 tier를 분류함 (1행 = 1초 가정)

    판정 기준은 trim 이후 유효 구간이므로, VALID에 필요한 총 측정 시간은
    params.required_total_sec임(기본값 210초). trim을 바꾸면 함께 흔들림.

    Returns:
        "VALID" — 유효 구간이 min_analysis_sec 이상임
        "PARTIAL" — trimming 후 0초 초과, 최소 미만임
        "ABORTED" — trimming 후 0초 이하임
    """
    resolved = _resolve_params(params)
    effective = total_samples - resolved.trim_start_sec - resolved.trim_end_sec
    if effective >= resolved.min_analysis_sec:
        return "VALID"
    elif effective > 0:
        return "PARTIAL"
    else:
        return "ABORTED"


def trim_dataframe(
    df: pd.DataFrame, params: ScoreParams | None = None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """진정 구간 trimming 수행함

    Returns:
        (trimmed_df, baseline_df)
        - trimmed_df: 유효 분석 구간 (시작 trim 이후부터 종료 trim 이전까지)
        - baseline_df: 시작 trim 구간 (기저 뇌파 참조용)
    """
    resolved = _resolve_params(params)
    total = len(df)
    end_trim = max(0, total - resolved.trim_end_sec)

    baseline_df = df.iloc[: resolved.trim_start_sec].copy()
    trimmed_df = (
        df.iloc[resolved.trim_start_sec : end_trim].copy().reset_index(drop=True)
    )

    return trimmed_df, baseline_df


def compute_baseline_from_warmup(
    baseline_df: pd.DataFrame,
    band_cols: list[str],
) -> dict[str, float]:
    """시작 15초 진정 구간에서 기저 뇌파 평균을 추출함"""
    result = {}
    for band in band_cols:
        if band in baseline_df.columns:
            result[band] = float(baseline_df[band].mean())
    return result


def find_csv_files(group_id: str, subject_index: int) -> list[Path]:
    """특정 그룹/피실험자의 CSV 파일을 검색함"""
    pattern = f"subject_{subject_index}_{group_id}_*.csv"
    return sorted(
        CSV_BASE_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True
    )


def load_session_data(csv_path: Path) -> pd.DataFrame:
    """CSV 파일을 DataFrame으로 로드함"""
    return pd.read_csv(csv_path)


def compute_subject_summary(df: pd.DataFrame) -> dict:
    """단일 피실험자의 세션 요약 통계를 계산함 (trimming 적용)"""
    metric_cols = [
        "focus",
        "engagement",
        "interest",
        "excitement",
        "stress",
        "relaxation",
    ]
    wave_cols = ["delta", "theta", "alpha", "beta", "gamma"]

    total_samples = len(df)
    tier = classify_session_tier(total_samples)

    # trimming 적용 — 유효 구간과 baseline 분리함
    trimmed_df, baseline_df = trim_dataframe(df)
    baseline_warmup = compute_baseline_from_warmup(baseline_df, wave_cols)

    # 유효 구간 기준 통계 계산함
    analysis_df = trimmed_df if len(trimmed_df) > 0 else df

    summary = {
        "metrics_mean": {
            col: float(analysis_df[col].mean())
            for col in metric_cols
            if col in analysis_df.columns
        },
        "metrics_std": {
            col: float(analysis_df[col].std())
            for col in metric_cols
            if col in analysis_df.columns
        },
        "waves_mean": {
            col: float(analysis_df[col].mean())
            for col in wave_cols
            if col in analysis_df.columns
        },
        "total_samples": total_samples,
        "effective_samples": len(trimmed_df),
        "duration_seconds": total_samples,
        "effective_duration_seconds": len(trimmed_df),
        "tier": tier,
        "baseline_warmup": baseline_warmup,
    }
    return summary


def align_on_second(
    df1: pd.DataFrame, df2: pd.DataFrame, column: str
) -> pd.DataFrame | None:
    """두 피실험자 시계열을 공통 절대시각(정수 초) 격자에서 정렬함.

    각 timestamp를 초 단위로 내림한 뒤 같은 초끼리 평균 집계하고 교집합만 남김.
    merge_asof 최근접 매칭을 쓰지 않는 이유: 두 스트림의 초 경계 위상차와
    행 간격 드리프트 때문에 경계 부근에서 최근접 이웃이 뒤집혀 상관값이
    pandas 버전과 부동소수에 민감해짐 (2026-07-10 교차검토).

    Args:
        df1 - subject 1 시계열, timestamp 컬럼 필요함
        df2 - subject 2 시계열, timestamp 컬럼 필요함
        column - 정렬 대상 값 컬럼명임

    Returns:
        `sec`, `{column}_1`, `{column}_2` 컬럼을 가진 DataFrame 반환.
        timestamp 컬럼이 없으면 None 반환(호출부가 폴백 판단함).
    """
    if "timestamp" not in df1.columns or "timestamp" not in df2.columns:
        return None

    frames = []
    for df in (df1, df2):
        sec = pd.to_datetime(df["timestamp"]).dt.floor("s")
        frames.append(
            pd.DataFrame({"sec": sec, column: df[column].values})
            .groupby("sec", as_index=False)[column]
            .mean()
        )

    return frames[0].merge(frames[1], on="sec", how="inner", suffixes=("_1", "_2"))


MIN_SYNCHRONY_PAIRS = 10


SYNC_SERIES_COLUMN = "_sync_series"


def resolve_sync_columns(
    df1: pd.DataFrame, df2: pd.DataFrame, params: ScoreParams
) -> list[str]:
    """양쪽 DataFrame에 모두 있는 동조 대상 열을 고름.

    정본은 측두-두정엽 셋(T7, T8, Pz)의 감마임. 채널별 열은 현행 37열 CSV에만
    있으므로 구형 CSV에서는 공간평균 열로 폴백하되 **무엇을 썼는지 호출부가
    결과에 남겨야 함** — 조용한 폴백은 나중에 "왜 점수가 다르지"를 만듦.

    Returns:
        선택된 열 이름 목록 반환. 쓸 열이 없으면 빈 목록 반환
    """
    shared = set(df1.columns) & set(df2.columns)
    channel_cols = [c for c in params.channel_columns if c in shared]
    if channel_cols:
        return channel_cols
    if params.spatial_column in shared:
        return [params.spatial_column]
    return []


def _sync_series(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """대상 열들의 채널 평균을 단일 시계열 열로 만든 DataFrame 반환함.

    채널 평균은 정본이 지정한 영역 전체를 하나의 시계열로 다루기 위함이며,
    단일 채널보다 창당 추정 잡음이 상쇄됨.
    """
    series = df[columns].mean(axis=1)
    frame = pd.DataFrame({SYNC_SERIES_COLUMN: series.values})
    if "timestamp" in df.columns:
        frame.insert(0, "timestamp", df["timestamp"].values)
    return frame


def _correlate_pair(
    df1: pd.DataFrame,
    df2: pd.DataFrame,
    column: str,
    params: ScoreParams,
) -> tuple[float | None, int]:
    """지정 열의 두 시계열을 정렬해 상관과 유효 쌍 수를 반환함"""
    analyzer = MindSignalAnalyzer()
    aligned = align_on_second(df1, df2, column)

    if aligned is None:
        # timestamp 부재 시 구 위치 정렬로 폴백함 (합성 픽스처 호환)
        logger.warning("timestamp 컬럼 부재로 위치 기반 정렬 폴백함")
        min_len = min(len(df1), len(df2))
        if min_len < MIN_SYNCHRONY_PAIRS:
            return None, min_len
        series1 = df1[column].values[:min_len]
        series2 = df2[column].values[:min_len]
        n_pairs = min_len
    else:
        if len(aligned) < MIN_SYNCHRONY_PAIRS:
            return None, len(aligned)
        series1 = aligned[f"{column}_1"].values
        series2 = aligned[f"{column}_2"].values
        n_pairs = len(aligned)

    rho = analyzer.calculate_synchrony(series1, series2, method=params.corr_method)
    # 한쪽이 상수면 상관이 nan임. 그대로 흘리면 응답 직렬화에서 터지므로
    # 미측정으로 낮춤(_finite_mask 주석의 Inf 사례와 같은 계열)
    if rho is None or not np.isfinite(rho):
        return None, n_pairs
    return float(rho), n_pairs


def compute_synchrony(
    df1: pd.DataFrame,
    df2: pd.DataFrame,
    params: ScoreParams | None = None,
) -> tuple[float | None, dict]:
    """두 피실험자 간 뇌파 동조율을 계산함 (trimming 적용).

    각 피실험자의 진정 구간을 먼저 제거한 뒤 공통 절대시각 구간만 비교함.
    측정 시작 시각이 어긋나도(2026-07-10 라이브: 38.3초 차이) 같은 시각끼리
    맞대므로 시차가 상관에 섞이지 않음.

    입력은 반드시 원본 CSV DataFrame임. average_by_timestamp를 거친 쪽은
    band_cols만 남아 채널별 열이 사라짐.

    Returns:
        (동조율 또는 None, 메타 dict) 튜플 반환. 메타에는 사용한 열들과 유효 쌍
        수, 상관 방식, 그리고 공간평균 기준 병기값(sync_secondary)이 담김
    """
    resolved = _resolve_params(params)

    # trimming 적용 — 진정 구간 제외한 유효 구간만 사용함
    trimmed1, _ = trim_dataframe(df1, resolved)
    trimmed2, _ = trim_dataframe(df2, resolved)

    meta: dict = {
        "corr_method": resolved.corr_method,
        "sync_columns_used": [],
        "n_pairs": 0,
        "sync_secondary": None,
    }

    columns = resolve_sync_columns(trimmed1, trimmed2, resolved)
    if not columns:
        logger.warning(
            "동조 대상 열 부재함. 후보 %s 또는 %s",
            resolved.channel_columns,
            resolved.spatial_column,
        )
        return None, meta

    rho, n_pairs = _correlate_pair(
        _sync_series(trimmed1, columns),
        _sync_series(trimmed2, columns),
        SYNC_SERIES_COLUMN,
        resolved,
    )
    meta["sync_columns_used"] = columns
    meta["n_pairs"] = n_pairs

    # 공간평균 기준값 병기함. 점수에는 쓰지 않되, 감마가 근전도로 오염됐을 때
    # "정본 준수와 강건성 중 무엇이 문제였나"를 사후에 판별할 유일한 수단임
    # (docs/research/friendship-score-validity-threats.md 2절 권고)
    spatial = resolved.spatial_column
    if (
        columns != [spatial]
        and spatial in trimmed1.columns
        and spatial in trimmed2.columns
    ):
        secondary, _ = _correlate_pair(trimmed1, trimmed2, spatial, resolved)
        meta["sync_secondary"] = secondary

    return rho, meta


def compute_session_analysis(group_id: str, subject_indices: list[int]) -> dict:
    """그룹 세션의 전체 분석을 수행함"""
    subjects = []
    dataframes = {}

    for idx in subject_indices:
        csv_files = find_csv_files(group_id, idx)
        if not csv_files:
            subjects.append({"subject_index": idx, "error": "CSV 파일 미발견"})
            continue

        df = load_session_data(csv_files[0])  # 가장 최신 파일 사용
        dataframes[idx] = df
        summary = compute_subject_summary(df)
        subjects.append({"subject_index": idx, **summary})

    # 두 명의 피실험자가 있을 때 동기화 점수 계산 수행함
    synchrony_score = None
    if len(dataframes) == 2:
        keys = list(dataframes.keys())
        synchrony_score, _ = compute_synchrony(dataframes[keys[0]], dataframes[keys[1]])

    return {
        "group_id": group_id,
        "subjects": subjects,
        "synchrony_score": synchrony_score,
        "dataframes": dataframes,  # Markdown 변환용 (응답에서는 제외됨)
    }


# ──────────────────────────────────────────────
# SEQUENTIAL 모드 파이프라인
# ──────────────────────────────────────────────


def analyze_pipeline_sequential(
    group_id: str,
    subject_indices: list[int],
    algorithm: str = "default",
) -> dict:
    """SEQUENTIAL 모드 분석 파이프라인을 실행함.

    두 피실험자를 시분할로 측정한 CSV를 로드하여 반응 유사도를 계산함.
    FAA는 raw EEG 채널 배열이 필요하므로 초기 버전에서는 None으로 처리함 (RR3).
    pair_features / y_score / synchrony_score는 DUAL 전용이므로 None 반환함.

    Args:
        group_id: 그룹 식별자
        subject_indices: 정확히 2명의 피실험자 인덱스 목록 (예: [1, 2], [3, 4])
        algorithm: 유사도 알고리즘 식별자

    Raises:
        ValueError: subject_indices가 정확히 2개가 아닌 경우
    """
    # 입력 검증 — SEQUENTIAL 모드는 정확히 2명 필요함
    if len(subject_indices) != 2:
        raise ValueError(
            f"SEQUENTIAL mode requires exactly 2 subject_indices, "
            f"got {len(subject_indices)}"
        )

    idx_a, idx_b = subject_indices[0], subject_indices[1]

    # 1. Subject A CSV 로드 수행함
    csv_files_a = find_csv_files(group_id, subject_index=idx_a)
    if not csv_files_a:
        raise ValueError(f"group_id={group_id} subject_index={idx_a} CSV 미발견")
    df_a = load_session_data(csv_files_a[0])

    # 2. Subject B CSV 로드 수행함
    csv_files_b = find_csv_files(group_id, subject_index=idx_b)
    if not csv_files_b:
        raise ValueError(f"group_id={group_id} subject_index={idx_b} CSV 미발견")
    df_b = load_session_data(csv_files_b[0])

    # 3. compute_subject_summary로 waves_mean 확보함 (N2: run_full_pipeline 대신)
    summary_a = compute_subject_summary(df_a)
    summary_b = compute_subject_summary(df_b)

    # 4. Scalar 기반 input contract 구성함 (I6: faa_mean=None 초기 처리)
    a_data = {"waves_mean": summary_a["waves_mean"], "faa_mean": None}
    b_data = {"waves_mean": summary_b["waves_mean"], "faa_mean": None}

    # 5. Strategy 호출하여 유사도 계산 수행함
    from server.services.similarity import compute as compute_similarity

    similarity_features = compute_similarity(a_data, b_data, algorithm=algorithm)

    return {
        "group_id": group_id,
        "subjects": [
            {"subject_index": idx_a, **summary_a},
            {"subject_index": idx_b, **summary_b},
        ],
        "similarity_features": similarity_features,
        "pair_features": None,  # DUAL 전용 필드
        "y_score": None,  # DUAL 전용 필드
        "synchrony_score": None,  # DUAL 전용 필드 (ADR-14-004)
    }


# ──────────────────────────────────────────────
# 파이프라인 단계별 함수 (알고리즘 명세 기반)
# ──────────────────────────────────────────────


def average_by_timestamp(df: pd.DataFrame, band_cols: list[str]) -> pd.DataFrame:
    """정수 초별 뇌파 신호를 평균화하고 절대시각을 보존함

    파싱할 수 없는 행은 제외하고 같은 정수 초의 중복 샘플을 평균화함.

    Raises:
        AnalysisContractError: timestamp 컬럼이 없거나 전 행 파싱 실패한 경우
    """
    if "timestamp" not in df.columns:
        raise AnalysisContractError(
            "TIMESTAMP_COLUMN_MISSING",
            "CSV에 timestamp 컬럼이 없음",
        )

    parsed = pd.to_datetime(df["timestamp"], errors="coerce")
    valid_mask = parsed.notna()
    if not valid_mask.any():
        raise AnalysisContractError(
            "TIMESTAMP_UNPARSABLE",
            "CSV의 timestamp를 파싱할 수 없음",
        )

    available = [c for c in band_cols if c in df.columns]
    normalized = df.loc[valid_mask, available].copy()
    normalized.insert(0, "timestamp", parsed.loc[valid_mask].dt.floor("s"))
    normalized = normalized.sort_values("timestamp")

    if not available:
        return normalized[["timestamp"]].drop_duplicates().reset_index(drop=True)

    return (
        normalized.groupby("timestamp", as_index=False, sort=True)[available]
        .mean()
        .reset_index(drop=True)
    )


def _resolve_origin(
    df: pd.DataFrame,
    common_origin: pd.Timestamp | None,
) -> pd.Timestamp:
    """명시된 공통 시작시각 또는 DataFrame의 첫 시각을 반환함"""
    if common_origin is not None:
        return pd.Timestamp(common_origin)
    if "timestamp" not in df.columns or df.empty:
        raise AnalysisContractError(
            "TIMESTAMP_UNPARSABLE",
            "분석 가능한 timestamp 행이 없음",
        )
    return pd.Timestamp(df["timestamp"].min())


def _finite_mask(interval_df: pd.DataFrame, band_cols: list[str]) -> pd.Series:
    """요청 대역이 모두 유한 수치인 행의 불리언 마스크를 반환함

    NaN뿐 아니라 ±Inf와 수치 변환 실패도 결측으로 처리함. Inf를 유효값으로
    인정하면 baseline이 Inf가 되어 응답 직렬화에서 500으로 이어짐.
    """
    if any(band not in interval_df.columns for band in band_cols):
        return pd.Series(False, index=interval_df.index)

    numeric = interval_df[band_cols].apply(pd.to_numeric, errors="coerce")
    return numeric.map(np.isfinite).all(axis=1)


def _valid_observation_count(
    interval_df: pd.DataFrame,
    band_cols: list[str],
) -> int:
    """요청 대역이 모두 유한 수치인 고유 관측 초를 계산함"""
    if "timestamp" not in interval_df.columns:
        return 0
    complete = interval_df.loc[_finite_mask(interval_df, band_cols), "timestamp"]
    return int(complete.nunique())


def compute_baseline(
    df: pd.DataFrame,
    band_cols: list[str],
    baseline_duration_sec: int = 30,
    common_origin: pd.Timestamp | None = None,
) -> dict[str, float]:
    """공통 시작시각부터 baseline 구간의 대역별 평균값을 산출함

    Raises:
        AnalysisContractError: baseline 길이가 0 이하이거나 coverage 미달인 경우
    """
    if baseline_duration_sec <= 0:
        raise AnalysisContractError(
            "BASELINE_COVERAGE_INSUFFICIENT",
            f"baseline 길이가 양수가 아님: {baseline_duration_sec}",
        )

    origin = _resolve_origin(df, common_origin)
    baseline_end = origin + pd.Timedelta(seconds=baseline_duration_sec)
    baseline_df = df.loc[(df["timestamp"] >= origin) & (df["timestamp"] < baseline_end)]
    minimum_observations = ceil(baseline_duration_sec * 0.5)
    observed = _valid_observation_count(baseline_df, band_cols)
    if observed < minimum_observations:
        raise AnalysisContractError(
            "BASELINE_COVERAGE_INSUFFICIENT",
            (
                "baseline 관측 초가 최소 coverage에 미달함: "
                f"{observed}/{minimum_observations}"
            ),
        )

    # coverage 판정과 같은 마스크에 더해 판정과 같은 수치 변환값으로 평균을 산출함
    # (object 컬럼의 숫자 문자열은 마스크를 통과하지만 mean()에서 문자열 연결로 터짐)
    complete = baseline_df.loc[
        _finite_mask(baseline_df, band_cols),
        band_cols,
    ].apply(pd.to_numeric, errors="coerce")
    return {band: float(complete[band].mean()) for band in band_cols}


def split_stimulus_windows(
    df: pd.DataFrame,
    band_cols: list[str],
    stimulus_duration_sec: int = 60,
    window_size_sec: int = 10,
    n_stimuli: int = 10,
    baseline_duration_sec: int = 30,
    common_origin: pd.Timestamp | None = None,
) -> list[WindowSlot]:
    """자극 구간을 절대시각 기반 고정 윈도우 슬롯으로 분할함

    coverage 미달 윈도우도 None 데이터와 원래 식별자를 가진 슬롯으로 유지함.
    """
    origin = _resolve_origin(df, common_origin)
    n_windows = stimulus_duration_sec // window_size_sec
    slots: list[WindowSlot] = []
    minimum_observations = ceil(window_size_sec * 0.5)

    for stim_idx in range(n_stimuli):
        stimulus_start = origin + pd.Timedelta(
            seconds=baseline_duration_sec + (stim_idx * stimulus_duration_sec)
        )

        for win_idx in range(n_windows):
            window_start = stimulus_start + pd.Timedelta(
                seconds=win_idx * window_size_sec
            )
            window_end = window_start + pd.Timedelta(seconds=window_size_sec)
            window_df = df.loc[
                (df["timestamp"] >= window_start) & (df["timestamp"] < window_end)
            ].reset_index(drop=True)
            observed = _valid_observation_count(window_df, band_cols)
            data = window_df if observed >= minimum_observations else None
            slots.append(
                WindowSlot(
                    stim_idx=stim_idx,
                    win_idx=win_idx,
                    window_start=window_start,
                    window_end=window_end,
                    data=data,
                )
            )

    return slots


def extract_features(
    windows: list[WindowSlot],
    band_cols: list[str],
    baseline: dict[str, float] | None = None,
) -> dict[str, float]:
    """윈도우별 × 대역별 feature를 추출함

    네이밍 컨벤션: s{stimulus_idx}_w{window_idx}_{band}
    baseline이 주어지면 baseline 대비 변화량으로 feature 계산함.
    """
    features: dict[str, float] = OrderedDict()

    for slot in windows:
        if slot.data is None:
            continue
        for band in band_cols:
            if band not in slot.data.columns:
                continue
            window_mean = float(slot.data[band].mean())

            # baseline이 있으면 baseline 대비 변화량으로 계산함
            if baseline is not None and band in baseline:
                feature_val = window_mean - baseline[band]
            else:
                feature_val = window_mean

            # 슬롯의 고정 식별자로 1-indexed 키를 생성함
            key = f"s{slot.stim_idx + 1}_w{slot.win_idx + 1}_{band}"
            features[key] = feature_val

    return features


def build_pair_features(
    features_a: dict[str, float],
    features_b: dict[str, float],
) -> dict[str, float]:
    """양쪽에 모두 존재하는 feature만 pair feature로 구성함"""
    pair: dict[str, float] = OrderedDict()
    common_keys = features_a.keys() & features_b.keys()

    # Subject A의 교집합 feature에 "a_" 접두사 추가함
    for key, val in features_a.items():
        if key in common_keys:
            pair[f"a_{key}"] = val

    # Subject B의 교집합 feature에 "b_" 접두사 추가함
    for key, val in features_b.items():
        if key in common_keys:
            pair[f"b_{key}"] = val

    return pair


def compute_y(satisfaction_a: float, satisfaction_b: float) -> float:
    """두 참가자의 관계 만족도 차이를 계산함 (타겟 변수)"""
    return abs(satisfaction_a - satisfaction_b)


def run_full_pipeline(
    group_id: str,
    subject_indices: list[int],
    stimulus_duration_sec: int = 60,
    window_size_sec: int = 10,
    n_stimuli: int = 10,
    baseline_duration_sec: int = 30,
    band_cols: list[str] | None = None,
    satisfaction_scores: dict[int, float] | None = None,
    params: ScoreParams | None = None,
) -> dict:
    """알고리즘 명세의 전체 파이프라인을 실행함

    [1] CSV 로드 → [2] 타임스탬프별 평균화 → [3] Baseline 산출
    → [4] Stimulus 윈도우 분할 → [5] Feature 추출
    → [7] Pair Feature 구성 → [8] Y 계산
    """
    # band_cols 기본값 설정함
    if band_cols is None:
        band_cols = ["alpha", "beta", "theta", "gamma"]

    n_windows_per_stimulus = stimulus_duration_sec // window_size_sec
    total_features_per_subject = n_stimuli * n_windows_per_stimulus * len(band_cols)

    subjects_by_index: dict[int, dict] = {}
    subject_features: dict[int, dict[str, float]] = {}
    raw_dataframes: dict[int, pd.DataFrame] = {}
    normalized_dataframes: dict[int, pd.DataFrame] = {}

    # 1차 pass에서 모든 사용 가능한 CSV를 로드하고 정규화함
    for idx in subject_indices:
        csv_files = find_csv_files(group_id, idx)
        if not csv_files:
            subjects_by_index[idx] = {
                "subject_index": idx,
                "error": "CSV 파일 미발견",
            }
            continue

        raw_df = load_session_data(csv_files[0])
        raw_dataframes[idx] = raw_df
        # 한 subject의 계약 위반이 다른 subject 분석까지 막지 않도록 격리함
        try:
            normalized_dataframes[idx] = average_by_timestamp(raw_df, band_cols)
        except AnalysisContractError as exc:
            subjects_by_index[idx] = {
                "subject_index": idx,
                "error": exc.detail,
                "error_code": exc.error_code,
            }

    # usable CSV가 없으면 기존 partial 200 계약을 유지함
    if not normalized_dataframes:
        return {
            "group_id": group_id,
            "subjects": [subjects_by_index[idx] for idx in subject_indices],
            "pair_features": None,
            "y_score": None,
            "synchrony_score": None,
            "friendship_score": None,
            "score_params": _resolve_params(params).to_dict(),
            "pipeline_params": {
                "stimulus_duration_sec": stimulus_duration_sec,
                "window_size_sec": window_size_sec,
                "n_stimuli": n_stimuli,
                "baseline_duration_sec": baseline_duration_sec,
                "band_cols": band_cols,
                "n_windows_per_stimulus": n_windows_per_stimulus,
                "total_features_per_subject": total_features_per_subject,
            },
            "dataframes": raw_dataframes,
        }

    common_origin = max(df["timestamp"].min() for df in normalized_dataframes.values())
    common_end = min(
        df["timestamp"].max() for df in normalized_dataframes.values()
    ) + pd.Timedelta(seconds=1)
    common_duration_sec = (common_end - common_origin).total_seconds()
    if common_duration_sec < baseline_duration_sec:
        raise AnalysisContractError(
            "COMMON_WINDOW_TOO_SHORT",
            (
                "공통 분석 구간이 baseline보다 짧음: "
                f"{common_duration_sec:g}/{baseline_duration_sec}초"
            ),
        )

    # 2차 pass에서 공통 구간으로 트리밍한 후 feature를 추출함
    aligned_dataframes: dict[int, pd.DataFrame] = {}
    for idx, normalized_df in normalized_dataframes.items():
        aligned_df = normalized_df.loc[
            (normalized_df["timestamp"] >= common_origin)
            & (normalized_df["timestamp"] < common_end)
        ].reset_index(drop=True)
        aligned_dataframes[idx] = aligned_df

        # subject 단위 계약 위반은 그 subject만 실패 처리하고 나머지를 살림
        try:
            # [3] Baseline 산출 수행함
            baseline = compute_baseline(
                aligned_df,
                band_cols,
                baseline_duration_sec,
                common_origin=common_origin,
            )

            # [4] Stimulus 윈도우 분할 수행함
            windows = split_stimulus_windows(
                aligned_df,
                band_cols,
                stimulus_duration_sec=stimulus_duration_sec,
                window_size_sec=window_size_sec,
                n_stimuli=n_stimuli,
                baseline_duration_sec=baseline_duration_sec,
                common_origin=common_origin,
            )

            # [5] Feature 추출 수행함
            features = extract_features(windows, band_cols, baseline=baseline)
        except AnalysisContractError as exc:
            subjects_by_index[idx] = {
                "subject_index": idx,
                "error": exc.detail,
                "error_code": exc.error_code,
            }
            continue

        subject_features[idx] = features
        subjects_by_index[idx] = {
            "subject_index": idx,
            "baseline": baseline,
            "features": features,
            "n_features": len(features),
        }

    subjects_result = [subjects_by_index[idx] for idx in subject_indices]

    # [7] Pair Feature 구성 (subject 2명일 때만 수행함)
    pair_features = None
    if len(subject_indices) == 2:
        idx_a, idx_b = subject_indices[0], subject_indices[1]
        if idx_a in subject_features and idx_b in subject_features:
            pair_features = build_pair_features(
                subject_features[idx_a], subject_features[idx_b]
            )

    # [8] Y 계산 (satisfaction_scores가 있을 때만 수행함)
    y_score = None
    if satisfaction_scores is not None and len(subject_indices) == 2:
        idx_a, idx_b = subject_indices[0], subject_indices[1]
        if idx_a in satisfaction_scores and idx_b in satisfaction_scores:
            y_score = compute_y(satisfaction_scores[idx_a], satisfaction_scores[idx_b])

    # 동조율과 Friendship Score 산출함. 입력은 반드시 raw_dataframes임 —
    # normalized 쪽은 band_cols만 남아 채널별 열(Pz_gamma)이 사라짐
    score_params = _resolve_params(params)
    synchrony_score = None
    sync_meta: dict = {}
    if len(raw_dataframes) == 2:
        keys = list(raw_dataframes.keys())
        synchrony_score, sync_meta = compute_synchrony(
            raw_dataframes[keys[0]], raw_dataframes[keys[1]], score_params
        )

    # 1단계는 FAA 회피율이 없으므로 avoidance_rate=None임 (2단계에서 연결)
    friendship_score, score_meta = compute_friendship_score(
        synchrony_score, None, score_params
    )

    return {
        "group_id": group_id,
        "subjects": subjects_result,
        "pair_features": pair_features,
        "y_score": y_score,
        "synchrony_score": synchrony_score,
        "friendship_score": friendship_score,
        "score_params": {**score_params.to_dict(), **sync_meta, **score_meta},
        "pipeline_params": {
            "stimulus_duration_sec": stimulus_duration_sec,
            "window_size_sec": window_size_sec,
            "n_stimuli": n_stimuli,
            "baseline_duration_sec": baseline_duration_sec,
            "band_cols": band_cols,
            "n_windows_per_stimulus": n_windows_per_stimulus,
            "total_features_per_subject": total_features_per_subject,
        },
        "dataframes": raw_dataframes,  # Markdown 변환용 원본 데이터임
    }
