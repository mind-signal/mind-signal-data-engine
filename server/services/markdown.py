from collections import defaultdict

import pandas as pd

from server.services.session_csv import METRIC_COLUMNS, WAVE_COLUMNS


def dataframe_to_markdown(dataframes: dict[int, pd.DataFrame]) -> str:
    """복수의 DataFrame을 LLM용 Markdown 텍스트로 변환함"""
    sections = []

    for subject_idx, df in dataframes.items():
        sections.append(f"## Subject {subject_idx}")

        # 요약 통계 테이블 생성함
        metric_cols = list(METRIC_COLUMNS)
        wave_cols = list(WAVE_COLUMNS)

        available_metrics = [c for c in metric_cols if c in df.columns]
        available_waves = [c for c in wave_cols if c in df.columns]

        if available_metrics:
            sections.append("### Performance Metrics (평균)")
            summary = (
                df[available_metrics].describe().loc[["mean", "std", "min", "max"]]
            )
            sections.append(summary.to_markdown(floatfmt=".4f"))

        if available_waves:
            sections.append("### Brain Wave Powers (평균)")
            summary = df[available_waves].describe().loc[["mean", "std", "min", "max"]]
            sections.append(summary.to_markdown(floatfmt=".6f"))

        sections.append(f"- 총 샘플 수: {len(df)}")
        sections.append(f"- 측정 시간: {len(df)}초")
        sections.append("")

    return "\n\n".join(sections)


def features_to_markdown(subject_index: int, features: dict[str, float]) -> str:
    """Feature 매트릭스를 LLM용 Markdown 테이블로 변환함

    s{stim}_w{win}_{band} 형태의 feature dict를
    stimulus × window × band 구조의 테이블로 변환함.
    """
    # stimulus별 {(window_idx, band): value} 구조로 그룹핑함
    stim_data: dict[int, dict[tuple[int, str], float]] = defaultdict(dict)
    all_bands: list[str] = []

    for key, value in features.items():
        parts = key.split("_")
        if len(parts) < 3:
            continue
        # s{n} → stimulus_idx, w{n} → window_idx, 나머지 → band
        stim_idx = int(parts[0][1:])
        win_idx = int(parts[1][1:])
        band = "_".join(parts[2:])
        stim_data[stim_idx][(win_idx, band)] = value
        if band not in all_bands:
            all_bands.append(band)

    sections = [f"## Subject {subject_index} — Feature Matrix"]

    for stim_idx in sorted(stim_data.keys()):
        cell_map = stim_data[stim_idx]
        window_indices = sorted({win for win, _ in cell_map.keys()})

        sections.append(f"\n### Stimulus {stim_idx}")

        # 헤더 행 생성함
        header = "| Window | " + " | ".join(all_bands) + " |"
        separator = "|--------|" + "|".join(["-------"] * len(all_bands)) + "|"
        sections.append(header)
        sections.append(separator)

        # 데이터 행 생성함
        for win_idx in window_indices:
            values = [
                f"{cell_map.get((win_idx, band), float('nan')):.4f}"
                for band in all_bands
            ]
            row = f"| W{win_idx} | " + " | ".join(values) + " |"
            sections.append(row)

    sections.append(f"\n- 총 feature 수: {len(features)}개")

    return "\n".join(sections)
