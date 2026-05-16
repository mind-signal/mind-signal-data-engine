"""proxy_client.py 단위 테스트 모음 — TDD RED → GREEN"""

import inspect
import json

import pytest
from pytest_httpx import HTTPXMock

# ──────────────────────────────────────────────
# 테스트 상수
# ──────────────────────────────────────────────
MOCK_PROXY_URL = "http://mock-proxy:3100"
MOCK_SECRET_KEY = "de-secret"
MOCK_GROUP_ID = "507f1f77bcf86cd799439011"
MOCK_SUBJECT_IDX = 1
MOCK_SEQ = 42
MOCK_PAYLOAD = {
    "delta": 0.1,
    "theta": 0.2,
    "alpha": 0.3,
    "beta": 0.4,
    "gamma": 0.5,
}
MOCK_SYNC_META = {"de_clock_domain": "monotonic_ns"}
INGEST_SAMPLE_URL = f"{MOCK_PROXY_URL}/ingest/sample"
FIXED_TS_NS = 123456789


# ──────────────────────────────────────────────
# T-PC-1: 성공 — 올바른 URL / 헤더 / body / de_ts_ns 확인
# ──────────────────────────────────────────────
def test_post_sample_success(httpx_mock: HTTPXMock, monkeypatch) -> None:
    """200 OK 시 URL·헤더·body·de_ts_ns 검증 — proxy_ingress_ts_ns 부재 확인함"""
    from server.services import proxy_client

    # time.monotonic_ns 고정함
    monkeypatch.setattr(proxy_client.time, "monotonic_ns", lambda: FIXED_TS_NS)

    httpx_mock.add_response(
        url=INGEST_SAMPLE_URL,
        method="POST",
        json={},
        status_code=200,
    )

    proxy_client.post_sample(
        proxy_url=MOCK_PROXY_URL,
        secret_key=MOCK_SECRET_KEY,
        group_id=MOCK_GROUP_ID,
        subject_idx=MOCK_SUBJECT_IDX,
        seq=MOCK_SEQ,
        payload=MOCK_PAYLOAD,
        sync_meta=MOCK_SYNC_META,
    )

    request = httpx_mock.get_request()
    assert request is not None
    assert request.method == "POST"
    assert str(request.url) == INGEST_SAMPLE_URL

    # X-Engine-Secret 헤더 검증함
    assert request.headers.get("x-engine-secret") == MOCK_SECRET_KEY

    # body 검증함
    body = json.loads(request.content)
    assert body["group_id"] == MOCK_GROUP_ID
    assert body["subject_idx"] == MOCK_SUBJECT_IDX
    assert body["de_ts_ns"] == str(FIXED_TS_NS)  # 십진수 문자열임
    assert body["seq"] == MOCK_SEQ
    assert body["payload"] == MOCK_PAYLOAD
    assert body["sync_meta"] == MOCK_SYNC_META
    assert body["sync_meta"]["de_clock_domain"] == "monotonic_ns"

    # proxy_ingress_ts_ns는 프록시가 추가 — DE body에서 부재 확인함
    assert "proxy_ingress_ts_ns" not in body


# ──────────────────────────────────────────────
# T-PC-2: retry 후 성공 — 503 1회 + 200 → 성공, sleep 1회 확인
# ──────────────────────────────────────────────
def test_post_sample_retry_then_success(httpx_mock: HTTPXMock, monkeypatch) -> None:
    """503 1회 후 200 → 성공 확인, time.sleep(0.1) 1회 호출 검증함"""
    from server.services import proxy_client

    monkeypatch.setattr(proxy_client.time, "monotonic_ns", lambda: FIXED_TS_NS)

    sleep_calls: list[float] = []

    def mock_sleep(seconds: float) -> None:
        # 실제 sleep 없이 호출 기록만 수행함
        sleep_calls.append(seconds)

    monkeypatch.setattr(proxy_client.time, "sleep", mock_sleep)

    # 503 1회 → 200 1회
    httpx_mock.add_response(
        url=INGEST_SAMPLE_URL,
        method="POST",
        status_code=503,
        json={"error": "Service Unavailable"},
    )
    httpx_mock.add_response(
        url=INGEST_SAMPLE_URL,
        method="POST",
        json={},
        status_code=200,
    )

    # max_retries=2 기본값으로 호출함 (1st retry에 0.1s backoff)
    proxy_client.post_sample(
        proxy_url=MOCK_PROXY_URL,
        secret_key=MOCK_SECRET_KEY,
        group_id=MOCK_GROUP_ID,
        subject_idx=MOCK_SUBJECT_IDX,
        seq=MOCK_SEQ,
        payload=MOCK_PAYLOAD,
        sync_meta=MOCK_SYNC_META,
    )

    # 요청 2회 발생 확인함 (1st 503, 2nd 200)
    requests = httpx_mock.get_requests(url=INGEST_SAMPLE_URL)
    assert len(requests) == 2

    # sleep 1회 / 0.1s backoff 확인함
    assert sleep_calls == [0.1]


# ──────────────────────────────────────────────
# T-PC-3: retry 소진 → ProxyForwardError + sleep 순서 + Redis 미포함 확인
# ──────────────────────────────────────────────
def test_post_sample_503_retry_exhausted_raises(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    """503 3회(1+max_retries=2) → ProxyForwardError raise, sleep [0.1, 0.2] 확인함"""
    from server.services import proxy_client
    from server.services.proxy_client import ProxyForwardError

    monkeypatch.setattr(proxy_client.time, "monotonic_ns", lambda: FIXED_TS_NS)

    sleep_calls: list[float] = []

    def mock_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(proxy_client.time, "sleep", mock_sleep)

    # 총 1+max_retries=3회 503 응답 등록함
    for _ in range(3):
        httpx_mock.add_response(
            url=INGEST_SAMPLE_URL,
            method="POST",
            status_code=503,
            json={"error": "Service Unavailable"},
        )

    with pytest.raises(ProxyForwardError):
        proxy_client.post_sample(
            proxy_url=MOCK_PROXY_URL,
            secret_key=MOCK_SECRET_KEY,
            group_id=MOCK_GROUP_ID,
            subject_idx=MOCK_SUBJECT_IDX,
            seq=MOCK_SEQ,
            payload=MOCK_PAYLOAD,
            sync_meta=MOCK_SYNC_META,
            max_retries=2,
        )

    # 총 요청 3회 확인함 (1 + max_retries=2)
    requests = httpx_mock.get_requests(url=INGEST_SAMPLE_URL)
    assert len(requests) == 3

    # 지수 백오프 순서 확인함 (0.1 → 0.2)
    assert sleep_calls == [0.1, 0.2]

    # Redis 미포함 검증 — proxy_client 모듈에 redis import 부재 확인함
    source = inspect.getsource(proxy_client)
    assert "redis" not in source.lower(), "proxy_client에 redis import가 있어서는 안 됨"


# ──────────────────────────────────────────────
# T1-DE-C: check_health 단위 테스트 (pytest-httpx)
# ──────────────────────────────────────────────
HEALTH_URL = f"{MOCK_PROXY_URL}/health"


def test_check_health_200_ok_returns_true(httpx_mock: HTTPXMock) -> None:
    """200 응답 시 True 반환 확인함"""
    from server.services.proxy_client import check_health

    httpx_mock.add_response(
        url=HEALTH_URL,
        method="GET",
        status_code=200,
        json={"status": "ok", "version": "1.0.0"},
    )

    result = check_health(MOCK_PROXY_URL)
    assert result is True


def test_check_health_503_fail_closed_returns_false(httpx_mock: HTTPXMock) -> None:
    """503 응답 시 False 반환 확인함"""
    from server.services.proxy_client import check_health

    httpx_mock.add_response(
        url=HEALTH_URL,
        method="GET",
        status_code=503,
        json={"status": "fail_closed", "version": "1.0.0"},
    )

    result = check_health(MOCK_PROXY_URL)
    assert result is False


def test_check_health_connection_error_returns_false(httpx_mock: HTTPXMock) -> None:
    """연결 오류 시 예외 미발생, False 반환 확인함"""
    import httpx as _httpx

    from server.services.proxy_client import check_health

    httpx_mock.add_exception(
        _httpx.ConnectError("Connection refused"),
        url=HEALTH_URL,
        method="GET",
    )

    result = check_health(MOCK_PROXY_URL)
    assert result is False


# ──────────────────────────────────────────────
# T1-DE-C: ProxyHealthTracker 순수 단위 테스트 (모킹 없음)
# ──────────────────────────────────────────────


def test_tracker_healthy_returns_false_and_resets() -> None:
    """healthy=True 시 False 반환 및 내부 상태 reset 확인함"""
    from server.services.proxy_client import ProxyHealthTracker

    tracker = ProxyHealthTracker(threshold_ms=3000)
    # healthy 상태에서는 항상 False 반환함
    assert tracker.record(True, 0.0) is False
    assert tracker.record(True, 100.0) is False


def test_tracker_first_fail_returns_false() -> None:
    """최초 fail 기록 시 False 반환 확인함 (threshold 미도달)"""
    from server.services.proxy_client import ProxyHealthTracker

    tracker = ProxyHealthTracker(threshold_ms=3000)
    # 첫 번째 fail — threshold 미도달임
    assert tracker.record(False, 0.0) is False


def test_tracker_fail_below_threshold_returns_false() -> None:
    """fail 지속 시간이 threshold 미만이면 False 반환 확인함"""
    from server.services.proxy_client import ProxyHealthTracker

    tracker = ProxyHealthTracker(threshold_ms=3000)
    tracker.record(False, 0.0)  # _first_fail_sec = 0.0
    # 2999ms 경과 — threshold 미도달임
    assert tracker.record(False, 2.999) is False


def test_tracker_fail_at_threshold_returns_true() -> None:
    """fail 지속 시간이 threshold 정확히 도달하면 True 반환 확인함"""
    from server.services.proxy_client import ProxyHealthTracker

    tracker = ProxyHealthTracker(threshold_ms=3000)
    tracker.record(False, 0.0)  # _first_fail_sec = 0.0
    # 3000ms 정확히 도달 — True 반환해야 함
    assert tracker.record(False, 3.0) is True


def test_tracker_latched_true_on_continued_fail() -> None:
    """threshold 초과 후 계속 fail 시 True 유지(latch) 확인함"""
    from server.services.proxy_client import ProxyHealthTracker

    tracker = ProxyHealthTracker(threshold_ms=3000)
    tracker.record(False, 0.0)
    tracker.record(False, 3.0)  # trip
    # 이후 추가 fail 에서도 True 유지됨
    assert tracker.record(False, 4.0) is True
    assert tracker.record(False, 10.0) is True


def test_tracker_healthy_after_trip_resets_to_false() -> None:
    """trip 후 healthy=True 입력 시 상태 reset → False 반환 확인함"""
    from server.services.proxy_client import ProxyHealthTracker

    tracker = ProxyHealthTracker(threshold_ms=3000)
    tracker.record(False, 0.0)
    tracker.record(False, 3.0)  # trip
    # healthy 복구 시 reset됨
    assert tracker.record(True, 4.0) is False
    # 재차 fail 시 새로 카운트 시작함
    assert tracker.record(False, 5.0) is False  # 첫 fail — threshold 미도달
    assert tracker.record(False, 7.999) is False  # 2999ms 미도달
    assert tracker.record(False, 8.0) is True  # 3000ms 도달 — True
