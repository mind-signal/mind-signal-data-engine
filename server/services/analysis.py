import logging
import os
from collections import OrderedDict
from pathlib import Path

import pandas as pd

from core.analyzer import MindSignalAnalyzer

logger = logging.getLogger(__name__)

# CSV 저장 기본 경로 (streamer.py와 동일한 위치)
CSV_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "csv"

# 측정 진정 구간 — 분석 제외, 표시만 함
TRIM_START_SECONDS = 15
TRIM_END_SECONDS = 15

# 최소 분석 가능 시간 (trimming 후 유효 구간 기준, 임시값 — 교수 확인 후 확정)
MIN_ANALYSIS_SECONDS = int(os.getenv("MIN_ANALYSIS_SECONDS", 180))


def classify_session_tier(total_samples: int) -> str:
    """측정 시간 기반 세션 tier를 분류함 (1행 = 1초 가정)

    Returns:
        "VALID" — 유효 구간 ≥ MIN_ANALYSIS_SECONDS
        "PARTIAL" — trimming 후 > 0초, < MIN
        "ABORTED" — trimming 후 ≤ 0초
    """
    effective = total_samples - TRIM_START_SECONDS - TRIM_END_SECONDS
    if effective >= MIN_ANALYSIS_SECONDS:
        return "VALID"
    elif effective > 0:
        return "PARTIAL"
    else:
        return "ABORTED"


def trim_dataframe(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """진정 구간 trimming 수행함

    Returns:
        (trimmed_df, baseline_df)
        - trimmed_df: 유효 분석 구간 (시작 15초 ~ 종료-15초)
        - baseline_df: 시작 15초 구간 (기저 뇌파 참조용)
    """
    total = len(df)
    end_trim = max(0, total - TRIM_END_SECONDS)

    baseline_df = df.iloc[:TRIM_START_SECONDS].copy()
    trimmed_df = df.iloc[TRIM_START_SECONDS:end_trim].copy().reset_index(drop=True)

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


def compute_synchrony(df1: pd.DataFrame, df2: pd.DataFrame) -> float | None:
    """두 피실험자 간 뇌파 동기화 점수를 계산함 (trimming 적용).

    각 피실험자의 진정 구간을 먼저 제거한 뒤 공통 절대시각 구간만 비교함.
    측정 시작 시각이 어긋나도(2026-07-10 라이브: 38.3초 차이) 같은 시각끼리
    맞대므로 시차가 상관에 섞이지 않음.
    """
    analyzer = MindSignalAnalyzer()

    # trimming 적용 — 진정 구간 제외한 유효 구간만 사용함
    trimmed1, _ = trim_dataframe(df1)
    trimmed2, _ = trim_dataframe(df2)

    aligned = align_on_second(trimmed1, trimmed2, "alpha")

    if aligned is None:
        # timestamp 부재 시 구 위치 정렬로 폴백함 (합성 픽스처 호환)
        logger.warning("timestamp 컬럼 부재로 위치 기반 정렬 폴백함")
        min_len = min(len(trimmed1), len(trimmed2))
        if min_len < 10:
            return None
        alpha1 = trimmed1["alpha"].values[:min_len]
        alpha2 = trimmed2["alpha"].values[:min_len]
        return float(analyzer.calculate_synchrony(alpha1, alpha2))

    if len(aligned) < 10:
        return None

    return float(
        analyzer.calculate_synchrony(
            aligned["alpha_1"].values, aligned["alpha_2"].values
        )
    )


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
        synchrony_score = compute_synchrony(dataframes[keys[0]], dataframes[keys[1]])

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
    """타임스탬프별 뇌파 신호를 평균화하여 노이즈를 감소시킴

    각 timestamp에서 측정된 원시 뇌파 신호가 여러 샘플인 경우,
    평균값으로 변환하여 대표값을 설정함.
    CSV가 이미 1초 1행이면 그대로 반환함.
    """
    # 'time' 또는 'timestamp' 컬럼 유무 확인
    time_col = None
    if "time" in df.columns:
        time_col = "time"
    elif "timestamp" in df.columns:
        time_col = "timestamp"

    if time_col is None:
        # 이미 1초 1행 구조로 가정하여 band_cols만 추출하여 반환함
        available = [c for c in band_cols if c in df.columns]
        return df[available].reset_index(drop=True)

    # 타임스탬프 기준 groupby 후 band_cols의 mean 계산 수행함
    available = [c for c in band_cols if c in df.columns]
    grouped = df.groupby(time_col)[available].mean().reset_index(drop=True)
    return grouped


def compute_baseline(
    df: pd.DataFrame,
    band_cols: list[str],
    baseline_duration_sec: int = 30,
) -> dict[str, float]:
    """Baseline 구간(처음 N초)의 대역별 평균값을 산출함"""
    # 처음 baseline_duration_sec 행을 baseline 구간으로 사용함 (1행 = 1초 가정)
    baseline_df = df.iloc[:baseline_duration_sec]
    result = {}
    for band in band_cols:
        if band in baseline_df.columns:
            result[band] = float(baseline_df[band].mean())
    return result


def split_stimulus_windows(
    df: pd.DataFrame,
    band_cols: list[str],
    stimulus_duration_sec: int = 60,
    window_size_sec: int = 10,
    n_stimuli: int = 10,
    baseline_duration_sec: int = 30,
) -> list[list[pd.DataFrame]]:
    """Stimulus 구간을 윈도우 단위로 분할함

    baseline 구간 이후의 데이터를 stimulus별 → window별로 분할함.
    반환: windows[stimulus_idx][window_idx] = DataFrame (band_cols만 포함)
    """
    n_windows = stimulus_duration_sec // window_size_sec
    available = [c for c in band_cols if c in df.columns]
    windows: list[list[pd.DataFrame]] = []

    for stim_idx in range(n_stimuli):
        # 각 stimulus의 시작 행 인덱스 계산함
        stimulus_start = baseline_duration_sec + (stim_idx * stimulus_duration_sec)
        stim_windows: list[pd.DataFrame] = []

        for win_idx in range(n_windows):
            win_start = stimulus_start + (win_idx * window_size_sec)
            win_end = win_start + window_size_sec

            # 데이터가 부족한 경우 해당 window 건너뜀
            if win_start >= len(df):
                break

            window_df = df[available].iloc[win_start:win_end].reset_index(drop=True)

            # 실제 데이터가 존재하는 경우만 추가함
            if len(window_df) > 0:
                stim_windows.append(window_df)

        windows.append(stim_windows)

    return windows


def extract_features(
    windows: list[list[pd.DataFrame]],
    band_cols: list[str],
    baseline: dict[str, float] | None = None,
) -> dict[str, float]:
    """윈도우별 × 대역별 feature를 추출함

    네이밍 컨벤션: s{stimulus_idx}_w{window_idx}_{band}
    baseline이 주어지면 baseline 대비 변화량으로 feature 계산함.
    """
    features: dict[str, float] = OrderedDict()

    for stim_idx, stim_windows in enumerate(windows):
        for win_idx, window_df in enumerate(stim_windows):
            for band in band_cols:
                if band not in window_df.columns:
                    continue
                window_mean = float(window_df[band].mean())

                # baseline이 있으면 baseline 대비 변화량으로 계산함
                if baseline is not None and band in baseline:
                    feature_val = window_mean - baseline[band]
                else:
                    feature_val = window_mean

                # 1-indexed 키 네이밍 적용함
                key = f"s{stim_idx + 1}_w{win_idx + 1}_{band}"
                features[key] = feature_val

    return features


def build_pair_features(
    features_a: dict[str, float],
    features_b: dict[str, float],
) -> dict[str, float]:
    """두 피실험자의 feature를 결합하여 pair feature를 구성함"""
    pair: dict[str, float] = OrderedDict()

    # Subject A feature에 "a_" 접두사 추가함
    for key, val in features_a.items():
        pair[f"a_{key}"] = val

    # Subject B feature에 "b_" 접두사 추가함
    for key, val in features_b.items():
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

    subjects_result = []
    subject_features: dict[int, dict[str, float]] = {}
    dataframes: dict[int, pd.DataFrame] = {}

    for idx in subject_indices:
        # [1] CSV 로드 수행함
        csv_files = find_csv_files(group_id, idx)
        if not csv_files:
            subjects_result.append({"subject_index": idx, "error": "CSV 파일 미발견"})
            continue

        raw_df = load_session_data(csv_files[0])
        dataframes[idx] = raw_df

        # [2] 타임스탬프별 평균화 수행함
        averaged_df = average_by_timestamp(raw_df, band_cols)

        # [3] Baseline 산출 수행함
        baseline = compute_baseline(averaged_df, band_cols, baseline_duration_sec)

        # [4] Stimulus 윈도우 분할 수행함
        windows = split_stimulus_windows(
            averaged_df,
            band_cols,
            stimulus_duration_sec=stimulus_duration_sec,
            window_size_sec=window_size_sec,
            n_stimuli=n_stimuli,
            baseline_duration_sec=baseline_duration_sec,
        )

        # [5] Feature 추출 수행함
        features = extract_features(windows, band_cols, baseline=baseline)

        subject_features[idx] = features
        subjects_result.append(
            {
                "subject_index": idx,
                "baseline": baseline,
                "features": features,
                "n_features": len(features),
            }
        )

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

    # 기존 compute_synchrony를 활용한 synchrony_score 계산 수행함
    synchrony_score = None
    if len(dataframes) == 2:
        keys = list(dataframes.keys())
        synchrony_score = compute_synchrony(dataframes[keys[0]], dataframes[keys[1]])

    return {
        "group_id": group_id,
        "subjects": subjects_result,
        "pair_features": pair_features,
        "y_score": y_score,
        "synchrony_score": synchrony_score,
        "pipeline_params": {
            "stimulus_duration_sec": stimulus_duration_sec,
            "window_size_sec": window_size_sec,
            "n_stimuli": n_stimuli,
            "baseline_duration_sec": baseline_duration_sec,
            "band_cols": band_cols,
            "n_windows_per_stimulus": n_windows_per_stimulus,
            "total_features_per_subject": total_features_per_subject,
        },
        "dataframes": dataframes,  # Markdown 변환용
    }
