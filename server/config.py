from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """환경변수 통합 관리 클래스임"""

    # FastAPI 서버
    fastapi_port: int = 5002
    registration_mode: Literal["local", "ngrok"] = "local"  # 등록 방식 선택

    # 백엔드 연동
    backend_url: str = "http://localhost:5000"
    engine_secret_key: str = "change-me-in-production"

    # DUAL_2PC 세션 env (launcher 주입, 비-DUAL_2PC 기동 시 None)
    dual_2pc_group_id: str | None = None
    dual_2pc_subject_index: int | None = None
    lan_ip: str | None = None  # LAN IP override (없으면 socket 자동 탐지)

    # Proxy 연동 (engine-proxy-sync Phase 18)
    proxy_url: str | None = None  # env PROXY_URL
    alignment_location: Literal["be", "proxy"] = "be"  # env ALIGNMENT_LOCATION

    # Emotiv
    client_id: str = ""
    client_secret: str = ""

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379

    # 실험
    experiment_duration_minutes: int = 10

    # 분석 파이프라인 파라미터
    stimulus_duration_sec: int = 60  # 1개 자극의 총 시간 (초)
    window_size_sec: int = 10  # 시간 분할 단위 (초)
    n_stimuli: int = 10  # 전체 자극 수
    n_bands: int = 4  # 사용할 뇌파 대역 수
    baseline_duration_sec: int = 30  # baseline 구간 길이 (초)
    band_cols: list[str] = ["alpha", "beta", "theta", "gamma"]  # 사용할 대역

    # ngrok (REGISTRATION_MODE=ngrok 때만 필요)
    ngrok_auth_token: str | None = None

    class Config:
        env_file = ".env.local"
        env_file_encoding = "utf-8"
        extra = "ignore"  # .env.local의 미정의 변수 무시함


settings = Settings()
