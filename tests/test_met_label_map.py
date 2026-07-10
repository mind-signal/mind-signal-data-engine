"""build_met_map 회귀 검증.

회귀 배경(2026-07-10 라이브 측정, groupId 6a508a6b1048b553eea41778):
    core/streamer.py가 met 라벨 'foc'만 탐색했으나 Insight2 Cortex는
    'attention'을 보냄. focus 키가 매핑에서 빠져 CSV의 focus 컬럼이
    827행 전 구간 0.000으로 기록됨.
    로그 24행 실측: {'engagement':3,'interest':12,'excitement':5,'stress':8,'relaxation':10}
"""

from core.streamer import build_met_map

# 2026-07-10 operator core 로그 23행에서 그대로 옮긴 Cortex Insight2 met 라벨임
INSIGHT2_LABELS = [
    "attention.isActive",
    "attention",
    "eng.isActive",
    "eng",
    "exc.isActive",
    "exc",
    "lex",
    "str.isActive",
    "str",
    "rel.isActive",
    "rel",
    "int.isActive",
    "int",
]


def test_insight2_attention_maps_to_focus():
    """Insight2의 attention 라벨이 focus로 매핑됨 (수정 전 실패하던 케이스)."""
    met_map = build_met_map(INSIGHT2_LABELS)
    assert met_map["focus"] == 1


def test_insight2_all_six_metrics_mapped():
    """6개 지표 전부 매핑되고 인덱스가 라이브 로그 실측과 일치함."""
    met_map = build_met_map(INSIGHT2_LABELS)
    assert met_map == {
        "focus": 1,
        "engagement": 3,
        "interest": 12,
        "excitement": 5,
        "stress": 8,
        "relaxation": 10,
    }


def test_legacy_foc_label_still_supported():
    """foc 라벨을 쓰는 구세대 헤드셋도 회귀 없이 매핑됨."""
    labels = ["foc", "eng", "exc", "str", "rel", "int"]
    met_map = build_met_map(labels)
    assert met_map["focus"] == 0


def test_attention_preferred_over_foc_when_both_present():
    """두 라벨이 함께 있으면 후보 순서대로 attention을 채택함."""
    labels = ["foc", "attention", "eng", "exc", "str", "rel", "int"]
    assert build_met_map(labels)["focus"] == 1


def test_missing_label_omits_key_instead_of_defaulting():
    """미발견 지표는 키를 만들지 않아 호출부가 누락을 감지함."""
    met_map = build_met_map(["eng", "exc"])
    assert "focus" not in met_map
    assert met_map["engagement"] == 0


def test_relabel_replaces_map_without_stale_keys():
    """라벨 재수신 시 이전 매핑이 남지 않음 (CodeRabbit PR #36).

    update()로 병합하면 이전 이벤트의 focus 인덱스가 남아, 새 라벨에
    focus가 없는데도 on_new_met_data가 옛 인덱스로 값을 읽는다.
    """
    first = build_met_map(INSIGHT2_LABELS)
    assert first["focus"] == 1

    second = build_met_map(["eng", "exc", "str", "rel", "int"])
    assert "focus" not in second
