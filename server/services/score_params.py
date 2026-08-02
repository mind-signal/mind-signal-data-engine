"""Friendship Score 산출 파라미터 정의함.

2026-06-22 회의 정본 수식의 가중치와 구간을 전부 파라미터로 노출함. 졸업
프로젝트가 자유로운 실험 체계여야 하므로 상수로 박지 않고, 실제 사용값을
결과에 실어 어떤 설정으로 나온 점수인지 사후 구분 가능하게 함.
"""

import math
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
        sync_channels: 동조 대상 채널 묶음임. 둘 이상이면 채널 평균을 씀.
            비어 있으면 CSV의 공간평균 열을 그대로 사용
        sync_band: 동조 대상 대역임. 정본은 감마
        trim_start_sec: 측정 앞쪽 제외 구간 초임. 정본은 baseline과 같은 30초
        trim_end_sec: 측정 뒤쪽 제외 구간 초임. 정본은 제외하지 않음(0)
        min_analysis_sec: trim 이후 유효 구간의 최소 초임
        min_synchrony_pairs: 상관을 낼 최소 유효 쌍 수임
    """

    w_sync: float = 1.0
    w_faa: float = 0.0
    corr_method: str = "spearman"
    # 정본은 측두-두정엽 셋임. 제안서 원문이 "두 참여자의 측두, 두정엽 감마파의
    # 시계열 데이터 간의 스피어만 순위 상관계수"이고 지표 표도 (T7, T8, Pz)임.
    # Pz 단독은 HANDOFF 요약 표기였고 원문과 어긋남
    sync_channels: tuple[str, ...] = ("T7", "T8", "Pz")
    sync_band: str = "gamma"
    # 정본은 "초반 30초 제외"임. 기존 앞뒤 15초는 계약 위반이었고, 그 값으로는
    # 동조율이 보는 구간과 feature baseline 구간(compute_baseline의
    # baseline_duration_sec=30)이 어긋나 같은 세션을 두 기준으로 잘랐음
    trim_start_sec: int = 30
    trim_end_sec: int = 0
    min_analysis_sec: int = 180
    # 상관을 낼 최소 유효 쌍 수임. 동조율 산출 계약의 일부라 원장에 함께 실음
    min_synchrony_pairs: int = 10

    def __post_init__(self) -> None:
        """가중치와 구간과 상관 방식의 유효성을 검사함

        설정을 실험용으로 열어 둔 구조라 잘못된 값은 반드시 들어옴. 로딩
        지점에서 거부하지 않으면 음수 trim이 예외 없이 엉뚱한 구간을 자르고
        nan 가중치가 검증을 다 통과해 응답 직렬화에서 터짐.

        Raises:
            ValueError: 가중치가 음수이거나 비유한이거나 합이 0 이하인 경우,
                구간이 음수인 경우, 미지원 상관 방식인 경우임
        """
        if not all(math.isfinite(w) for w in (self.w_sync, self.w_faa)):
            raise ValueError(
                f"가중치는 유한해야 함. w_sync={self.w_sync} w_faa={self.w_faa}"
            )
        for name in ("trim_start_sec", "trim_end_sec", "min_analysis_sec"):
            if getattr(self, name) < 0:
                raise ValueError(
                    f"{name}는 음수일 수 없음. 받은 값 {getattr(self, name)}"
                )
        if self.min_synchrony_pairs < 2:
            raise ValueError("min_synchrony_pairs는 2 이상이어야 함")
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
    def channel_columns(self) -> list[str]:
        """채널별 대상 열 이름을 반환함. 채널 미지정이면 빈 목록임"""
        return [f"{ch}_{self.sync_band}" for ch in self.sync_channels]

    @property
    def spatial_column(self) -> str:
        """채널별 열이 없는 구형 CSV용 공간평균 열 이름을 반환함"""
        return self.sync_band

    @classmethod
    def from_env(cls) -> "ScoreParams":
        """환경변수에서 파라미터를 읽음.

        모듈 import 시점이 아니라 호출 시점에 읽으므로 프로세스 재기동 없이도
        테스트에서 monkeypatch로 바꿀 수 있음. 기본값은 전부 이 클래스의 필드
        기본값을 그대로 씀 — 두 자리에 숫자를 적으면 반드시 갈라짐.
        MIN_ANALYSIS_SECONDS만 FS_ 접두사가 없는데, 백엔드와 프론트도 같은
        이름을 쓰는 의도적 예외임.
        """
        base = cls()
        raw_channels = os.getenv("FS_SYNC_CHANNELS", "").strip()
        channels = (
            tuple(c.strip() for c in raw_channels.split(",") if c.strip())
            if raw_channels
            else base.sync_channels
        )
        # 명시적으로 빈 값을 주면 공간평균 열을 쓰겠다는 뜻임
        if raw_channels in ("none", "-"):
            channels = ()
        return cls(
            w_sync=_env_float("FS_W_SYNC", base.w_sync),
            w_faa=_env_float("FS_W_FAA", base.w_faa),
            # 대소문자를 정규화함. 오타 하나가 모듈 로드 실패로 이어지지 않게 함
            corr_method=os.getenv("FS_CORR_METHOD", base.corr_method).strip().lower()
            or base.corr_method,
            sync_channels=channels,
            sync_band=os.getenv("FS_SYNC_BAND", base.sync_band).strip().lower()
            or base.sync_band,
            trim_start_sec=_env_int("FS_TRIM_START_SEC", base.trim_start_sec),
            trim_end_sec=_env_int("FS_TRIM_END_SEC", base.trim_end_sec),
            min_analysis_sec=_env_int("MIN_ANALYSIS_SECONDS", base.min_analysis_sec),
            min_synchrony_pairs=_env_int(
                "FS_MIN_SYNCHRONY_PAIRS", base.min_synchrony_pairs
            ),
        )

    def to_dict(self) -> dict:
        """결과에 실을 파라미터 원장을 반환함"""
        return {
            "formula_version": FORMULA_VERSION,
            "w_sync": self.w_sync,
            "w_faa": self.w_faa,
            "corr_method": self.corr_method,
            "sync_channels": list(self.sync_channels),
            "sync_band": self.sync_band,
            "trim_start_sec": self.trim_start_sec,
            "trim_end_sec": self.trim_end_sec,
            "min_analysis_sec": self.min_analysis_sec,
            "min_synchrony_pairs": self.min_synchrony_pairs,
            "required_total_sec": self.required_total_sec,
        }
