import os
from pathlib import Path
from typing import Literal

import certifi
from dotenv import load_dotenv
from pydantic_settings import BaseSettings

# .env.local을 os.environ에 실제로 싣는 유일한 지점임. Settings의 env_file은
# 이 클래스만 채우고 os.environ에는 넣지 않으므로, os.getenv로 읽는 쪽
# (ScoreParams.from_env 등)은 이것이 없으면 값을 못 봄.
# **config에서 하는 이유**: app.py에서 부르면 그보다 먼저 import되는 모듈이
# 이미 os.getenv를 끝낸 뒤라 설정이 조용히 무시됨(실측: FS_ 파라미터 전부).
# 절대경로인 이유는 cwd에 의존하지 않기 위함임
load_dotenv(Path(__file__).resolve().parent.parent / ".env.local")

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
    # env MESSAGE_BUS — redis(기본) 또는 memory. 값 검사는 아래
    # validate_message_bus가 서버 기동(lifespan)과 스트리머 생성 시점에 수행함.
    # Literal로 두지 않는 이유는 import 시점의 pydantic 오류 대신 원인을 적은
    # 메시지로 멈추기 위함임
    message_bus: str = "redis"
    proxy_reregister_interval_sec: int = (
        20  # env PROXY_REREGISTER_INTERVAL_SEC — TTL 만료 전 재등록 주기(초)
    )

    class Config:
        env_file = ".env.local"
        env_file_encoding = "utf-8"
        extra = "ignore"  # .env.local의 미정의 변수 무시함


settings = Settings()


class MessageBusConfigError(ValueError):
    """MESSAGE_BUS 설정이 측정을 시작할 수 없는 조합일 때 raise되는 예외임"""


def validate_message_bus(
    message_bus: str, alignment_location: str, proxy_url: str | None
) -> None:
    """메시지 버스 설정 조합을 검사함. redis는 기존 동작 그대로라 통과함.

    memory는 샘플과 경보를 프록시로만 보내므로 프록시 설정이 빠지면 샘플이
    갈 곳이 없음. 조용히 버리지 않고 측정 시작 전에 멈추게 함.

    Args:
        message_bus: MESSAGE_BUS 값임 (redis 또는 memory)
        alignment_location: ALIGNMENT_LOCATION 값임
        proxy_url: PROXY_URL 값임

    Raises:
        MessageBusConfigError: 알 수 없는 값이거나 memory인데 프록시 설정이 없을 때
    """
    if message_bus == "redis":
        return
    if message_bus != "memory":
        raise MessageBusConfigError(
            f"MESSAGE_BUS={message_bus!r}는 지원하지 않음. redis 또는 memory만 가능함"
        )
    if alignment_location != "proxy":
        raise MessageBusConfigError(
            "MESSAGE_BUS=memory는 ALIGNMENT_LOCATION=proxy가 필요함 "
            f"(현재 {alignment_location!r}). Redis 없이는 샘플을 프록시로만 보낼 수 있음"
        )
    if not proxy_url:
        raise MessageBusConfigError(
            "MESSAGE_BUS=memory는 PROXY_URL이 필요함 (현재 비어 있음). "
            "예: PROXY_URL=http://127.0.0.1:5050"
        )
