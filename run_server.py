import os
import sys


def _apply_io_env() -> None:
    """자식 프로세스용 IO 환경변수 주입함 (reload 워커와 core.main spawn에 상속됨)."""
    # Phase 17.5.3 — Windows uvicorn reloader 환경에서 print() stdout buffering으로
    # lifespan 로그가 누락되는 현상 방지함. reloader process가 worker stdout을 pipe
    # forwarding하는 과정에서 flush 지연 + 터미널 라인 잘림 발생 가능. DEV_RELOAD=0
    # 단일 프로세스 경로에서도 로그 가시성에 유용해 유지함.
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    # OPS-W006 — stdout이 tty가 아니면(파이프, 파일 redirect, Start-Process) 인코딩이
    # ANSI 코드페이지(cp949)로 확정돼 em dash 같은 비ASCII 로그에서
    # UnicodeEncodeError로 프로세스가 죽음. server/services/stream.py가 core.main
    # stdout을 로그 파일로 redirect하므로 그 경로는 상시 노출됨 — 2026-06-30 측정
    # 1건이 이 원인으로 0초 종료함(logs/core_6a438aa5..._1.log, 후속 커밋 6faf7c8).
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def _force_utf8_stdout() -> None:
    """현재 프로세스 stdout/stderr를 utf-8로 재설정함 (멱등, 미지원 스트림은 무시)."""
    # 위 환경변수는 자식에만 먹음. 이미 시작된 자기 인터프리터는 이쪽이 필요하고,
    # DEV_RELOAD=0으로 lifespan이 이 프로세스에서 도는 경로가 정확히 그 대상임.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def _dev_reload() -> bool:
    """개발용 자동 reload 활성화 여부 반환함. 런처 경로는 0을 주입해 단일 프로세스 기동."""
    return os.getenv("DEV_RELOAD", "1") == "1"


# server.app import 시 logbuffer가 stdout을 감싸므로 그 전에 원본을 교정해 둠.
_apply_io_env()
_force_utf8_stdout()

import uvicorn  # noqa: E402

from server.config import settings  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(
        "server.app:app",
        host="0.0.0.0",
        port=settings.fastapi_port,
        reload=_dev_reload(),
    )
