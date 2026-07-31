import os
from typing import Literal

import certifi
from pydantic_settings import BaseSettings

# SSL_CERT_FILE이 존재하지 않는 경로를 가리키면 certifi 번들로 교정함.
# conda base 활성화가 심는 stale 값(miniconda3/ssl/cacert.pem 부재)으로 httpx가
# FileNotFoundError로 죽는 것을 방어함. config는 httpx 사용 모듈 대부분이 import하므로
# 여기서 교정하면 런타임+테스트 전 경로에서 유효함.
_ssl_cert = os.environ.get("SSL_CERT_FILE")
if _ssl_cert and not os.path.exists(_ssl_cert):
    os.environ["SSL_CERT_FILE"] = certifi.where()


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
    lan_ip: str | None = (
        None  # advertise IP override (없으면 Tailscale 자동탐지 후 socket 폴백)
    )

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
