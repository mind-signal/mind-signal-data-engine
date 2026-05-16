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
