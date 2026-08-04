import asyncio
import os

import httpx

from server.config import settings

# heartbeat 주기 (초) — Heroku dyno sleep 대비 5분마다 재등록함
HEARTBEAT_INTERVAL_SEC = 300


async def register_to_backend(public_url: str, secret_key: str) -> None:
    """백엔드에 엔진 URL + secret_key를 자동 등록함"""
    payload = {
        "engineUrl": public_url,
        "secretKey": secret_key,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{settings.backend_url}/api/engine/register",
            json=payload,
        )
        # RC-7: silent catch 제거 — 실패 시 예외 전파해 lifespan SystemExit 트리거
        response.raise_for_status()
    print(f"백엔드 등록 성공함: {public_url}")


async def register_to_backend_dual(
    public_url: str,
    group_id: str,
    subject_index: int,
    secret_key: str,
) -> None:
    """DUAL_2PC 모드: groupId+subjectIndex 기반 BE 등록."""
    payload = {
        "groupId": group_id,
        "subjectIndex": subject_index,
        "engineUrl": public_url,
        "secretKey": secret_key,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"{settings.backend_url}/api/engine/register-dual",
            json=payload,
        )
        response.raise_for_status()
    # [RC3-1 반영] DE 서버 전체가 print() 사용 — 기존 convention 유지
    print(
        f"DUAL_2PC register success: groupId={group_id}, "
        f"subjectIndex={subject_index}, url={public_url}"
    )


async def register_to_backend_pending(
    public_url: str,
    subject_index: int,
    secret_key: str,
) -> None:
    """BE에 pending DE URL 사전 등록 호출함 (groupId 미정 상태)."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{settings.backend_url}/api/engine/register-pending",
            headers={"Content-Type": "application/json"},
            json={
                "subjectIndex": subject_index,
                "engineUrl": public_url,
                "secretKey": secret_key,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        print(f"[pending registration] subject={subject_index} url={public_url} OK")


async def unregister_to_backend_pending(
    public_url: str,
    subject_index: int,
    secret_key: str,
) -> None:
    """BE에 pending entry 삭제 호출함 (DE shutdown 시). soft-fail."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.request(
                "DELETE",
                f"{settings.backend_url}/api/engine/register-pending",
                headers={"Content-Type": "application/json"},
                json={
                    "subjectIndex": subject_index,
                    "engineUrl": public_url,
                    "secretKey": secret_key,
                },
                timeout=5.0,
            )
            response.raise_for_status()
        print(f"[pending unregister] subject={subject_index} url={public_url} OK")
    except httpx.HTTPError as e:
        # soft-fail: shutdown 흐름이라 raise 금지 — httpx 예외만 catch
        print(f"[WARN] pending unregister failed (soft): {e}")


async def register_to_proxy(
    proxy_url: str,
    subject_index: int,
    public_url: str,
    secret_key: str,
) -> None:
    """프록시에 엔진 URL + subject_idx 등록 완료.

    Args:
        proxy_url: 프록시 서버 베이스 URL.
        subject_index: 피실험자 인덱스 (subject_idx 필드로 전달).
        public_url: 등록할 DE 퍼블릭 URL (de_url 필드로 전달).
        secret_key: X-Engine-Secret 헤더로 전달되는 공유 시크릿.

    Raises:
        httpx.HTTPStatusError: 프록시가 4xx/5xx 응답 반환 시.
        httpx.RequestError: 네트워크 오류 발생 시.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"{proxy_url}/register",
            headers={
                "X-Engine-Secret": secret_key,
                "Content-Type": "application/json",
            },
            json={
                "subject_idx": subject_index,
                "de_url": public_url,
            },
        )
        response.raise_for_status()
    print(f"proxy 등록 완료: subject_idx={subject_index} url={public_url}")


async def start_heartbeat(public_url: str, secret_key: str):
    """주기적으로 백엔드에 엔진 URL을 재등록하는 heartbeat 태스크임 (1PC legacy 등록 전용)"""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
        try:
            await register_to_backend(public_url, secret_key)
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            # heartbeat는 transient error 허용 (log-and-continue)
            # R2-1: register_to_backend가 RC-7로 naked 됐으므로 여기서 명시 보호
            # [RC3-1 반영] DE 서버 전체가 print() 사용 — 기존 convention 유지
            print(f"heartbeat register failed (non-fatal): {e}")


async def start_heartbeat_dual(
    public_url: str,
    group_id: str,
    subject_index: int,
    secret_key: str,
):
    """DUAL_2PC mode 전용 heartbeat — 5분마다 register-dual 재호출함 (Phase 17.5)"""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
        try:
            await register_to_backend_dual(
                public_url, group_id, subject_index, secret_key
            )
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            # heartbeat transient error 허용함 (log-and-continue)
            print(f"dual heartbeat register failed (non-fatal): {e}")


def upload_csv_to_backend(
    backend_url: str,
    csv_path: str,
    secret_key: str,
    timeout: float = 10.0,
) -> bool:
    """측정 종료 시 subject CSV를 operator BE로 업로드함 (2-PC 집계). soft-fail.

    원본 CSV는 각 노트북에 분산 유지하고 분석용 사본만 operator로 전송함.
    실패해도 raise 안 함 — 측정 종료 흐름을 깨지 않기 위함. streamer가 sync 컨텍스트라
    sync httpx.Client 사용함.

    Args:
        backend_url: operator BE 베이스 URL.
        csv_path: 업로드할 로컬 CSV 절대경로.
        secret_key: X-Engine-Secret 헤더 공유 시크릿.
        timeout: HTTP 타임아웃 초.

    Returns:
        업로드 성공 여부 반환.
    """
    filename = os.path.basename(csv_path)
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            content = f.read()
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{backend_url}/api/engine/csv-upload",
                params={"filename": filename},
                headers={
                    "X-Engine-Secret": secret_key,
                    "Content-Type": "text/csv",
                },
                content=content.encode("utf-8"),
            )
            response.raise_for_status()
        print(f"[csv-upload] {filename} 업로드 완료")
        return True
    except (httpx.HTTPError, OSError) as e:
        # soft-fail: 측정 종료 흐름 보호 — raise 금지
        print(f"[WARN] csv-upload failed (soft): {e}")
        return False
