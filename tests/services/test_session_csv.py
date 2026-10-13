"""세션 CSV 위치와 파일 이름 규칙 모듈 검증"""

from server.services import analysis, stream
from server.services.session_csv import (
    METRIC_COLUMNS,
    SESSION_CSV_DIR,
    WAVE_COLUMNS,
    session_csv_pattern,
)


def test_pattern_without_escape():
    """이스케이프 없이 group_id를 그대로 넣음"""
    assert session_csv_pattern("g[1]", 2) == "subject_2_g[1]_*.csv"


def test_pattern_with_escape():
    """escape_group_id가 참이면 glob 메타문자를 이스케이프함"""
    assert (
        session_csv_pattern("g[1]", 2, escape_group_id=True) == "subject_2_g[[]1]_*.csv"
    )


def test_csv_dir_is_shared_by_readers():
    """분석과 업로드가 같은 폴더를 가리킴"""
    assert str(SESSION_CSV_DIR) == stream._CSV_DIR
    assert SESSION_CSV_DIR == analysis.CSV_BASE_DIR


def test_column_lists():
    """지표 6종과 대역 5종의 열 이름과 순서를 고정함"""
    assert METRIC_COLUMNS == (
        "focus",
        "engagement",
        "interest",
        "excitement",
        "stress",
        "relaxation",
    )
    assert WAVE_COLUMNS == ("delta", "theta", "alpha", "beta", "gamma")
