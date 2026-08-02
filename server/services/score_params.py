"""Friendship Score 산출 파라미터 정의함.

2026-06-22 회의 정본 수식의 가중치와 구간을 전부 파라미터로 노출함. 졸업
프로젝트가 자유로운 실험 체계여야 하므로 상수로 박지 않고, 실제 사용값을
결과에 실어 어떤 설정으로 나온 점수인지 사후 구분 가능하게 함.
"""

import os
from dataclasses import dataclass

# 파라미터 값이 같아도 구현이 바뀐 경우를 구분하기 위한 식별자임.
# 소급 부여가 불가능한 정보라 처음부터 결과에 실음. 2단계(FAA)에서 "2"로 올림
FORMULA_VERSION = "1"

_VALID_CORR_METHODS = ("spearman", "pearson")


def _env_float(name: str, default: float) -> float:
    """환경변수를 실수로 읽음. 미설정이거나 빈 값이면 기본값 반환"""
    raw = os.getenv(name, "")
    return float(raw) if raw.strip() else default


def _env_int(name: str, default: int) -> int:
    """환경변수를 정수로 읽음. 미설정이거나 빈 값이면 기본값 반환"""
    raw = os.getenv(name, "")
    return int(raw) if raw.strip() else default


@dataclass(frozen=True)
class ScoreParams:
    """Friendship Score 산출에 쓰는 가중치와 구간 묶음임.

    Attributes:
        w_sync: 동조 항 가중치임. 정본 1.0
        w_faa: FAA 회피 항 가중치임. 1단계는 0, 2단계에서 0.25
        corr_method: 상관 방식임. 정본은 스피어만 순위상관
        sync_channel: 동조 대상 채널임. None이면 채널 공간평균 열 사용
        sync_band: 동조 대상 대역임. 정본은 감마
        trim_start_sec: 측정 앞쪽 진정 구간 초임
        trim_end_sec: 측정 뒤쪽 진정 구간 초임
        min_analysis_sec: trim 이후 유효 구간의 최소 초임
    """

    w_sync: float = 1.0
    w_faa: float = 0.0
    corr_method: str = "spearman"
    sync_channel: str | None = "Pz"
    sync_band: str = "gamma"
    trim_start_sec: int = 15
    trim_end_sec: int = 15
    min_analysis_sec: int = 180

    def __post_init__(self) -> None:
        """가중치 부호와 합, 상관 방식의 유효성을 검사함

        Raises:
            ValueError: 가중치가 음수이거나 합이 0 이하이거나 미지원 상관 방식임
        """
        if self.w_sync < 0 or self.w_faa < 0:
            raise ValueError(
                f"가중치는 음수일 수 없음. w_sync={self.w_sync} w_faa={self.w_faa}"
            )
        if self.w_sync + self.w_faa <= 0:
            raise ValueError("w_sync와 w_faa의 합이 양수여야 함")
        if self.corr_method not in _VALID_CORR_METHODS:
            raise ValueError(
                f"미지원 상관 방식 {self.corr_method!r}. "
                f"허용값 {_VALID_CORR_METHODS}"
            )

    @property
    def required_total_sec(self) -> int:
        """tier VALID에 필요한 총 측정 초를 파생함.

        min_analysis_sec은 trim 이후 유효 구간 기준이므로 앞뒤 진정 구간을
        더해야 실제 필요 측정 시간이 됨(기본값에서 210초, 즉 3분 30초).
        이 값을 별도 상수로 박지 말 것 — trim을 바꾸면 함께 흔들림.
        """
        return self.min_analysis_sec + self.trim_start_sec + self.trim_end_sec

    @property
    def sync_column_candidates(self) -> list[str]:
        """동조 대상 열 후보를 우선순위 순으로 반환함.

        채널별 열이 없는 구형 CSV를 위해 공간평균 열로 폴백함.
        """
        if self.sync_channel:
            return [f"{self.sync_channel}_{self.sync_band}", self.sync_band]
        return [self.sync_band]

    @classmethod
    def from_env(cls) -> "ScoreParams":
        """환경변수에서 파라미터를 읽음.

        모듈 import 시점이 아니라 호출 시점에 읽으므로 프로세스 재기동 없이도
        테스트에서 monkeypatch로 바꿀 수 있음. MIN_ANALYSIS_SECONDS만 FS_
        접두사가 없는데, 백엔드와 프론트도 같은 이름을 쓰는 의도적 예외임.
        """
        channel = os.getenv("FS_SYNC_CHANNEL", "Pz").strip()
        return cls(
            w_sync=_env_float("FS_W_SYNC", 1.0),
            w_faa=_env_float("FS_W_FAA", 0.0),
            corr_method=os.getenv("FS_CORR_METHOD", "spearman").strip() or "spearman",
            sync_channel=channel or None,
            sync_band=os.getenv("FS_SYNC_BAND", "gamma").strip() or "gamma",
            trim_start_sec=_env_int("FS_TRIM_START_SEC", 15),
            trim_end_sec=_env_int("FS_TRIM_END_SEC", 15),
            min_analysis_sec=_env_int("MIN_ANALYSIS_SECONDS", 180),
        )

    def to_dict(self) -> dict:
        """결과에 실을 파라미터 원장을 반환함"""
        return {
            "formula_version": FORMULA_VERSION,
            "w_sync": self.w_sync,
            "w_faa": self.w_faa,
            "corr_method": self.corr_method,
            "sync_channel": self.sync_channel,
            "sync_band": self.sync_band,
            "trim_start_sec": self.trim_start_sec,
            "trim_end_sec": self.trim_end_sec,
            "min_analysis_sec": self.min_analysis_sec,
            "required_total_sec": self.required_total_sec,
        }
