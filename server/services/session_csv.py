"""세션 CSV의 폴더 위치, 파일 이름 규칙, 열 이름 목록을 한곳에 모은 모듈임"""

import glob
from pathlib import Path

# CSV 저장 루트 — 레포 부모의 csv/. core/streamer.py가 쓰는 위치와 같음
SESSION_CSV_DIR: Path = Path(__file__).resolve().parent.parent.parent.parent / "csv"

# EMOTIV 지표 6종의 CSV 열 이름임
METRIC_COLUMNS: tuple[str, ...] = (
    "focus",
    "engagement",
    "interest",
    "excitement",
    "stress",
    "relaxation",
)

# 대역 파워 5종의 CSV 열 이름임
WAVE_COLUMNS: tuple[str, ...] = ("delta", "theta", "alpha", "beta", "gamma")


def session_csv_pattern(
    group_id: str, subject_index: int, *, escape_group_id: bool = False
) -> str:
    """세션 CSV 파일 이름의 glob 패턴을 반환함 (폴더 경로는 포함하지 않음)"""
    gid = glob.escape(group_id) if escape_group_id else group_id
    return f"subject_{subject_index}_{gid}_*.csv"
