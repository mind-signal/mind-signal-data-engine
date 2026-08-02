import asyncio
import ipaddress
import socket
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from server.config import settings
from server.routes import analyze, control, export, health, logs, stream
from server.services import logbuffer
from server.services.analysis import AnalysisContractError
from server.services.webhook import (
    register_to_backend,
    register_to_backend_dual,
    register_to_backend_pending,
    register_to_proxy,
    start_heartbeat,
    start_heartbeat_dual,
    unregister_to_backend_pending,
)

load_dotenv(".env.local")

# Preflight soft-check: ENGINE_SECRET_KEY가 placeholder면 WARNING 로그만 출력함
# (Phase 17.5.1 — abort 제거). 실기기 테스트 등 placeholder 그대로 쓰다가 실험 후
# 보완하는 흐름을 허용함. 공개 ngrok URL에 /control/assign-group 노출 리스크는
# 경고로만 안내함
PLACEHOLDER_SECRETS = {
    "your-shared-secret-here",
    "change-me-in-production",
    "",
}

# Tailscale CGNAT 대역 — 2PC cross-machine 도달 경로는 Tailscale뿐이라
# advertise IP는 반드시 이 대역이어야 함
_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def _detect_tailscale_ip() -> str | None:
    """이 머신의 Tailscale IP(100.64.0.0/10 대역) 자동 탐지함.

    LAN_IP 미지정 시 socket.gethostbyname은 기본 어댑터(Docker/WSL/Wi-Fi)를
    골라 operator가 도달 못 하는 주소를 광고하는 결함이 있어, Tailscale 대역
    인터페이스를 우선 선택함.

    Returns:
        Tailscale IPv4 문자열, 없으면 None.
    """
    try:
        _, _, ips = socket.gethostbyname_ex(socket.gethostname())
    except OSError:
        return None
    for ip in ips:
        try:
            if ipaddress.ip_address(ip) in _TAILSCALE_NET:
                return ip
        except ValueError:
            continue
    return None


def _resolve_advertise_ip(explicit: str | None) -> str:
    """DE가 proxy에 광고할 IP 결정함.

    우선순위: 명시 LAN_IP(Tailscale 대역일 때만) > Tailscale 대역 자동탐지 >
    socket 폴백. Tailscale이 유일 transport라 명시 LAN_IP가 대역 밖(스테일
    LAN/Wi-Fi 주소)이면 무시하고 자동탐지로 내려 cross-machine 도달 실패를
    self-heal함 (하드 실패 대신 경고 — 라이브 중단 방지).

    Args:
        explicit: LAN_IP env 값 (없으면 None).

    Returns:
        광고할 IPv4 문자열.
    """
    if explicit:
        try:
            if ipaddress.ip_address(explicit) in _TAILSCALE_NET:
                return explicit
        except ValueError:
            pass
        print(
            f"[WARN] LAN_IP={explicit} 이(가) Tailscale 대역(100.64.0.0/10) 밖이라 "
            "무시하고 자동탐지로 대체함 (cross-machine 도달 보장)"
        )
    return _detect_tailscale_ip() or socket.gethostbyname(socket.gethostname())


@asynccontextmanager
async def lifespan(app: FastAPI):
    """서버 시작 시 URL 결정 + 백엔드 등록(모드별 분기) + heartbeat 수행함.

    분기 (Phase 17.5 + 17.6 + 18 통합):
    0) ALIGNMENT_LOCATION=proxy → BE 등록 분기 전체 skip + proxy /register
       retry (Phase 18 — proxy가 subjectIndex→DE_URL 레지스트리 소유, R1-11).
       app.state.pending_registered 는 False 유지 → shutdown BE unregister 미호출
    1) dual_2pc_group_id + subject_index 둘 다 env → 즉시 register-dual +
       dual heartbeat (backward-compat)
    2) subject_index만 env → pending 상태 기동 → register_to_backend_pending
       retry + /control/assign-group 대기 (heartbeat 미생성)
    3) 둘 다 없음 → SEQUENTIAL register + single heartbeat (backward-compat)

    분기 1~3 은 be 모드(ALIGNMENT_LOCATION=be, 기본값) 전용임.
    """
    # Preflight soft-check: placeholder secret 감지 시 WARNING만 출력함 (Phase 17.5.1)
    if settings.engine_secret_key in PLACEHOLDER_SECRETS:
        print(
            "[WARN] ENGINE_SECRET_KEY가 placeholder 값임 "
            f"('{settings.engine_secret_key}'). /control/assign-group이 "
            "공개 ngrok URL에서 사실상 검증 없이 열림. 실기기 테스트 후 "
            "실제 값으로 교체 권장함."
        )

    # public_url 결정 (registration_mode 기반)
    if settings.registration_mode == "ngrok":
        from pyngrok import ngrok

        tunnel = ngrok.connect(settings.fastapi_port, bind_tls=True)
        public_url = tunnel.public_url
        print(f"ngrok 퍼블릭 URL 발급됨: {public_url}", flush=True)
    else:  # local
        lan_ip = _resolve_advertise_ip(settings.lan_ip)
        public_url = f"http://{lan_ip}:{settings.fastapi_port}"
        print(f"[INFO] DE advertise URL: {public_url}")

    # app.state 초기화 (Phase 17.5) — 모든 분기 공통
    app.state.public_url = public_url
    app.state.subject_index = settings.dual_2pc_subject_index
    app.state.secret_key = settings.engine_secret_key
    app.state.registered_group_id = None
    app.state.heartbeat_task = None  # 분기 2는 미생성 → shutdown 가드용
    app.state.proxy_heartbeat_task = None  # proxy 모드 재등록 태스크 → shutdown 가드용
    app.state.assign_lock = asyncio.Lock()
    app.state.pending_registered = (
        False  # 분기 2 retry 결과 — shutdown unregister 가드용
    )

    # 모드 판별 — 빈 문자열("") env를 None과 동등하게 취급함 (Phase 17.5.2)
    # pydantic이 `DUAL_2PC_GROUP_ID=` 빈 값을 ""로 파싱해 is not None 통과하는 버그 방지
    has_group_id = bool(settings.dual_2pc_group_id)
    has_subject_index = settings.dual_2pc_subject_index is not None

    # Phase 18: proxy 모드 여부 판별 (ALIGNMENT_LOCATION=proxy 시 true)
    proxy_mode = settings.alignment_location == "proxy"

    if proxy_mode:
        # proxy 모드: BE 등록 분기 전체 skip — proxy가 subjectIndex→DE_URL 레지스트리 소유 (R1-11)
        if not settings.proxy_url:
            print(
                "[WARN] ALIGNMENT_LOCATION=proxy 이지만 PROXY_URL 미설정. "
                "proxy 등록 skip함."
            )
        elif settings.dual_2pc_subject_index is None:
            print(
                "[WARN] ALIGNMENT_LOCATION=proxy 이지만 DUAL_2PC_SUBJECT_INDEX 미설정. "
                "proxy 등록 skip함."
            )
        else:
            # proxy 등록 retry (Phase 17.6 pending-retry 패턴 미러)
            proxy_registered = False
            for attempt in range(3):
                try:
                    await register_to_proxy(
                        settings.proxy_url,
                        settings.dual_2pc_subject_index,
                        public_url,
                        settings.engine_secret_key,
                    )
                    proxy_registered = True
                    break
                except (httpx.RequestError, httpx.HTTPStatusError) as e:
                    print(
                        f"[WARN] proxy registration attempt {attempt + 1}/3 실패함: {e}"
                    )
                    if attempt < 2:
                        await asyncio.sleep(2**attempt)  # 1s, 2s
            if not proxy_registered:
                print(
                    "[WARN] proxy registration 3회 실패함. DE 계속 실행, "
                    "proxy 기동 후 재등록 대기."
                )

            # proxy TTL 만료 전 주기적 재등록 태스크 시작함 (등록 성공 여부 무관하게 시작)
            # — 최초 등록 실패 시에도 루프 안에서 재시도하여 복구 가능함
            async def _proxy_reregister_loop(
                _proxy_url: str,
                _subject_index: int,
                _public_url: str,
                _secret_key: str,
                _interval_sec: int,
            ) -> None:
                """proxy 등록 TTL 만료 전 주기적으로 register_to_proxy 재호출함.

                Args:
                    _proxy_url: 프록시 서버 베이스 URL.
                    _subject_index: 피실험자 인덱스.
                    _public_url: 등록할 DE 퍼블릭 URL.
                    _secret_key: 공유 시크릿.
                    _interval_sec: 재등록 주기(초).
                """
                while True:
                    await asyncio.sleep(_interval_sec)
                    try:
                        await register_to_proxy(
                            _proxy_url,
                            _subject_index,
                            _public_url,
                            _secret_key,
                        )
                    except (httpx.RequestError, httpx.HTTPStatusError) as e:
                        # 일시적 오류 — 루프 유지하며 다음 주기에 재시도함
                        print(f"[WARN] proxy 재등록 실패 (non-fatal): {e}")

            app.state.proxy_heartbeat_task = asyncio.create_task(
                _proxy_reregister_loop(
                    settings.proxy_url,
                    settings.dual_2pc_subject_index,
                    public_url,
                    settings.engine_secret_key,
                    settings.proxy_reregister_interval_sec,
                )
            )
    else:
        # be 모드 (default): 기존 동작 100% 보존
        try:
            if has_group_id and has_subject_index:
                # 분기 1: 즉시 DUAL_2PC 등록
                await register_to_backend_dual(
                    public_url,
                    settings.dual_2pc_group_id,
                    settings.dual_2pc_subject_index,
                    settings.engine_secret_key,
                )
                app.state.registered_group_id = settings.dual_2pc_group_id
                app.state.heartbeat_task = asyncio.create_task(
                    start_heartbeat_dual(
                        public_url,
                        settings.dual_2pc_group_id,
                        settings.dual_2pc_subject_index,
                        settings.engine_secret_key,
                    )
                )
            elif has_subject_index:
                # 분기 2: pending — BE 등록 하지 않음. /control/assign-group 대기함
                print(
                    f"DE pending: subject_index={settings.dual_2pc_subject_index}, "
                    f"awaiting POST /control/assign-group"
                )
            else:
                # 분기 3: SEQUENTIAL (backward-compat)
                await register_to_backend(public_url, settings.engine_secret_key)
                app.state.heartbeat_task = asyncio.create_task(
                    start_heartbeat(public_url, settings.engine_secret_key)
                )
        except Exception as e:
            # [RC3-1 반영] DE 서버 전체가 print() 사용 — 기존 convention 유지
            print(f"DE registration failed: {e}")
            raise SystemExit(1)

        # Phase 17.6 LD-22/LD-18: 분기 2 pending mode 시 BE pending registry에 등록 retry함
        # 독립 try/except로 외부 SystemExit 전파 차단함
        if has_subject_index and not has_group_id:
            pending_registered = False
            for attempt in range(3):
                try:
                    await register_to_backend_pending(
                        public_url,
                        settings.dual_2pc_subject_index,
                        settings.engine_secret_key,
                    )
                    pending_registered = True
                    break
                except (httpx.RequestError, httpx.HTTPStatusError) as e:
                    # Fail-Fast: httpx 예외만 catch — 설정/직렬화 오류는 propagate
                    print(
                        f"[WARN] pending registration attempt {attempt + 1}/3 실패함: {e}"
                    )
                    if attempt < 2:
                        await asyncio.sleep(2**attempt)  # 1s, 2s
            if not pending_registered:
                print(
                    "[WARN] pending registration 3회 실패함. DE 계속 실행, "
                    "수동 fallback 의존."
                )
            # 분기 2 내부 raise 금지 — yield까지 정상 진행
            app.state.pending_registered = pending_registered

    yield

    # --- shutdown ---
    # heartbeat_task 가드 (분기 2는 None 가능)
    if app.state.heartbeat_task is not None:
        app.state.heartbeat_task.cancel()

    # proxy 재등록 태스크 취소함 (proxy 모드가 아닌 경우 None)
    if app.state.proxy_heartbeat_task is not None:
        app.state.proxy_heartbeat_task.cancel()

    # Phase 17.6 LD-26: pending entry 삭제 호출함 (DE shutdown 시 soft-fail)
    if (
        has_subject_index
        and not has_group_id
        and getattr(app.state, "pending_registered", False)
    ):
        await unregister_to_backend_pending(
            app.state.public_url,
            settings.dual_2pc_subject_index,
            settings.engine_secret_key,
        )

    if settings.registration_mode == "ngrok":
        from pyngrok import ngrok

        ngrok.disconnect(public_url)


app = FastAPI(
    title="Mind Signal Data Engine",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(AnalysisContractError)
async def analysis_contract_error_handler(
    request: Request,
    exc: AnalysisContractError,
) -> JSONResponse:
    """분석 계약 오류를 최상위 평면 422 응답으로 변환함"""
    return JSONResponse(
        status_code=422,
        content={"error_code": exc.error_code, "detail": exc.detail},
    )


app.include_router(health.router, tags=["Health"])
app.include_router(analyze.router, prefix="/api", tags=["Analyze"])
app.include_router(export.router, prefix="/api", tags=["Export"])
app.include_router(stream.router, prefix="/api", tags=["Stream"])
app.include_router(control.router, tags=["Control"])
app.include_router(logs.router, tags=["Logs"])

# 서버 로그를 링버퍼에 캡처해 대시보드에서 원격 조회 가능하게 함 (멱등)
logbuffer.install()
