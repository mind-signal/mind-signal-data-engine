"""Friendship Score 산출함.

2026-06-22 회의 정본 수식의 가중 합을 0..100으로 환산함. 1단계는 FAA 회피
항을 뺀 부분식이라 동조 항만 유효하고, 상관 0이 50점이 됨(기존 구현은 음수를
잘라 0점을 냈고 그것이 가장 큰 왜곡이었음).
"""

from server.services.score_params import ScoreParams


def normalize_synchrony(rho: float) -> float:
    """상관계수를 민맥스 정규화함. 정의역 [-1, 1]에서 [0, 1]로 옮김"""
    return (rho + 1.0) / 2.0


def compute_friendship_score(
    synchrony: float | None,
    avoidance_rate: float | None,
    params: ScoreParams,
) -> tuple[float | None, dict]:
    """정본 수식의 가중 합을 0..100 점수로 환산해 반환함.

    동조율은 필수 항이고 FAA는 선택 항임. 이 비대칭은 의도이며 2단계에서도
    유지함 — 수식 전체가 동조율 위에 서 있으므로 동조율 결측은 세션 실패로
    보고 점수를 내지 않고, FAA 결측은 항만 빼고 분모를 줄임.

    Args:
        synchrony: 동조율(순위상관 등), 미측정이면 None임
        avoidance_rate: FAA 회피율 [0, 1], 1단계는 항상 None임
        params: 가중치와 구간 파라미터임

    Returns:
        (점수 또는 None, 산출 메타 dict) 튜플 반환. 점수는 소수 1자리이며
        정수화는 저장 시점에 함

    Raises:
        ValueError: avoidance_rate가 [0, 1] 범위 밖임
    """
    meta: dict = {"terms": []}

    if synchrony is None:
        meta["reason"] = "synchrony_missing"
        return None, meta

    numerator = params.w_sync * normalize_synchrony(synchrony)
    w_effective = params.w_sync
    meta["terms"].append("sync")

    if avoidance_rate is not None:
        if not 0.0 <= avoidance_rate <= 1.0:
            raise ValueError(
                f"avoidance_rate는 [0, 1]이어야 함. 받은 값 {avoidance_rate}"
            )
        numerator += params.w_faa * (1.0 - avoidance_rate)
        w_effective += params.w_faa
        meta["terms"].append("faa")

    # w_sync=0에 w_faa>0인 설정은 ScoreParams 검증을 통과하는데, 1단계는
    # avoidance가 항상 None이라 유효 가중치가 0이 됨. 0으로 나누지 않고
    # 미산출로 낮춤 — 파라미터를 실험용으로 열어둔 이상 이 조합은 들어옴
    if w_effective <= 0:
        meta["reason"] = "no_effective_term"
        return None, meta

    score = 100.0 * numerator / w_effective
    meta["w_effective"] = w_effective
    return round(min(100.0, max(0.0, score)), 1), meta
