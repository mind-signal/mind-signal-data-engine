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
    proxy_reregister_interval_sec: int = (
        20  # env PROXY_REREGISTER_INTERVAL_SEC — TTL 만료 전 재등록 주기(초)
    )

    class Config:
        env_file = ".env.local"
        env_file_encoding = "utf-8"
        extra = "ignore"  # .env.local의 미정의 변수 무시함


settings = Settings()
