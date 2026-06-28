"""proxy_client.py — 동기 HTTP 클라이언트로 DE→Proxy 샘플 포워딩 수행함

Emotiv Cortex SDK 콜백(동기 스레드)에서 직접 호출되므로 httpx.Client(동기) 사용함.
AsyncClient 사용 금지 — 동기 스레드에서 이벤트 루프 없이 호출됨.
"""

import time

import httpx


class ProxyForwardError(Exception):
    """retry 소진 후 proxy 전송 최종 실패 시 raise되는 예외임"""

    pass


def check_health(proxy_url: str, timeout: float = 5.0) -> bool:
    """proxy /health 폴링하여 정상 여부 반환함. 200 ok→True, 503/네트워크오류→False.

    Args:
        proxy_url: 프록시 서버 베이스 URL.
        timeout: HTTP 요청 타임아웃(초).

    Returns:
        HTTP 200 응답 시 True, 그 외(503/비200/네트워크 오류) False 반환함.
    """
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"{proxy_url}/health")
            return response.status_code == 200
    except httpx.RequestError:
        return False


class ProxyHealthTracker:
    """proxy /health 폴링 결과를 누적하여 fail-closed 발동 여부 판단하는 트래커임.

    스레드 없이 순수하게 상태만 추적함 — I/O 없음, time.time() 미사용.
    호출자가 now_sec(float)를 직접 전달하여 테스트 용이성 보장함.
    """

    def __init__(self, threshold_ms: int) -> None:
        """트래커 초기화함.

        Args:
            threshold_ms: fail 연속 지속 임계값(밀리초). 이 값 이상 지속 시 발동함.
        """
        self._threshold_ms = threshold_ms
        self._first_fail_sec: float | None = None

    def record(self, healthy: bool, now_sec: float) -> bool:
        """폴링 결과 기록. fail이 threshold_ms 이상 연속 지속 시 True(=fail-closed 발동) 반환함.

        healthy=True 시 내부 상태 reset.

        Args:
            healthy: True이면 정상 응답, False이면 비정상(503/연결 오류).
            now_sec: 현재 시각(초, 단조 시계). 호출자가 time.monotonic() 전달함.

        Returns:
            fail-closed 발동 시 True, 미발동 시 False 반환함.
        """
        if healthy:
            self._first_fail_sec = None
            return False

        # healthy=False 처리 수행함
        if self._first_fail_sec is None:
            self._first_fail_sec = now_sec
            return False

        elapsed_ms = (now_sec - self._first_fail_sec) * 1000
        return elapsed_ms >= self._threshold_ms


def post_sample(
    proxy_url: str,
    secret_key: str,
    group_id: str,
    subject_idx: int,
    seq: int,
    payload: dict,
    sync_meta: dict,
    max_retries: int = 2,
    backoffs_sec: tuple[float, ...] = (0.1, 0.2),
) -> None:
    """DE 샘플 데이터를 proxy /ingest/sample 엔드포인트로 포워딩함.

    Args:
        proxy_url: 프록시 서버 베이스 URL.
        secret_key: X-Engine-Secret 헤더로 전달되는 공유 시크릿 (D14).
        group_id: 세션 그룹 ID.
        subject_idx: 피실험자 인덱스.
        seq: 엔진 단위 단조 증가 시퀀스 번호.
        payload: 5대역 파워 dict (delta/theta/alpha/beta/gamma).
        sync_meta: 동기화 메타 dict (de_clock_domain 포함).
        max_retries: 재시도 최대 횟수 (총 시도 = 1 + max_retries).
        backoffs_sec: 각 retry 전 대기 시간 (초) 순서 튜플.

    Raises:
        ProxyForwardError: max_retries 소진 후 최종 실패 시 raise됨.
    """
    # 전송 시각 나노초 타임스탬프 생성함 (십진수 문자열, R1-2)
    de_ts_ns = str(time.monotonic_ns())

    # 정식 envelope body 구성함 (proxy_ingress_ts_ns는 프록시가 추가 — 포함 금지)
    body = {
        "group_id": group_id,
        "subject_idx": subject_idx,
        "de_ts_ns": de_ts_ns,
        "seq": seq,
        "payload": payload,
        "sync_meta": sync_meta,
    }

    last_exc: Exception | None = None

    # 총 시도 횟수 = 1(초기) + max_retries
    for attempt in range(1 + max_retries):
        if attempt > 0:
            # CX2-5: 지수 백오프 대기 수행함
            backoff_idx = attempt - 1
            wait_sec = (
                backoffs_sec[backoff_idx]
                if backoff_idx < len(backoffs_sec)
                else backoffs_sec[-1]
            )
            time.sleep(wait_sec)

        try:
            with httpx.Client(timeout=10) as client:
                response = client.post(
                    f"{proxy_url}/ingest/sample",
                    headers={
                        "X-Engine-Secret": secret_key,
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                # 4xx/5xx 응답 시 HTTPStatusError raise함
                response.raise_for_status()
                # 전송 성공 — 즉시 반환함
                return
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            # 네트워크 오류 또는 상태 코드 오류 — retry 대상임
            last_exc = exc

    # retry 소진 — 터미널 실패 처리함
    raise ProxyForwardError(
        f"proxy 전송 retry 소진 ({1 + max_retries}회 시도): {last_exc}"
    ) from last_exc
