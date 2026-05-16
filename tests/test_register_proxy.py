"""register_to_proxy webhook 단위 테스트 모음"""

import json

import httpx
import pytest
from pytest_httpx import HTTPXMock

from server.services.webhook import register_to_proxy

# ──────────────────────────────────────────────
# 테스트 상수
# ──────────────────────────────────────────────
MOCK_PROXY_URL = "http://mock-proxy:3100"
MOCK_ENGINE_URL = "http://192.168.0.10:5002"
MOCK_SUBJECT_INDEX = 2
MOCK_SECRET_KEY = "proxy-test-secret"
REGISTER_PROXY_URL = f"{MOCK_PROXY_URL}/register"


# ──────────────────────────────────────────────
# T-PROXY-1: 성공 시 snake_case body + X-Engine-Secret 헤더 확인
# ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_register_proxy_success(
    httpx_mock: HTTPXMock,
) -> None:
    """proxy /register 200 OK 시 올바른 URL + 헤더 + snake_case body 전송함"""
    httpx_mock.add_response(
        url=REGISTER_PROXY_URL,
        json={"ok": True},
        status_code=200,
    )

    await register_to_proxy(
        proxy_url=MOCK_PROXY_URL,
        subject_index=MOCK_SUBJECT_INDEX,
        public_url=MOCK_ENGINE_URL,
        secret_key=MOCK_SECRET_KEY,
    )

    request = httpx_mock.get_request()
    assert request is not None
    assert request.method == "POST"
    assert str(request.url) == REGISTER_PROXY_URL

    # X-Engine-Secret 헤더 검증 (secret은 body가 아닌 헤더로 전달됨)
    assert request.headers.get("x-engine-secret") == MOCK_SECRET_KEY

    # snake_case body 검증
    body = json.loads(request.content)
    assert body == {
        "subject_idx": MOCK_SUBJECT_INDEX,
        "de_url": MOCK_ENGINE_URL,
    }


# ──────────────────────────────────────────────
# T-PROXY-2: 401 응답 시 HTTPStatusError raise 확인 (secret 불일치 시나리오)
# ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_register_proxy_401_raises(
    httpx_mock: HTTPXMock,
) -> None:
    """proxy가 401 반환 시 httpx.HTTPStatusError raise됨 (secret 불일치 시나리오)"""
    httpx_mock.add_response(
        url=REGISTER_PROXY_URL,
        status_code=401,
        json={"error": "unauthorized"},
    )

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await register_to_proxy(
            proxy_url=MOCK_PROXY_URL,
            subject_index=MOCK_SUBJECT_INDEX,
            public_url=MOCK_ENGINE_URL,
            secret_key="wrong-secret",
        )

    assert exc_info.value.response.status_code == 401


# ──────────────────────────────────────────────
# T-PROXY-3: 503 단일 응답 시 HTTPStatusError raise + 1회 요청만 확인
# (webhook.py 자체 retry 없음 — lifespan 레이어가 retry 담당)
# ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_register_proxy_503_raises_no_retry(
    httpx_mock: HTTPXMock,
) -> None:
    """register_to_proxy 자체는 retry 없음 — 503 즉시 HTTPStatusError raise됨"""
    httpx_mock.add_response(
        url=REGISTER_PROXY_URL,
        status_code=503,
        json={"error": "Service Unavailable"},
    )

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await register_to_proxy(
            proxy_url=MOCK_PROXY_URL,
            subject_index=MOCK_SUBJECT_INDEX,
            public_url=MOCK_ENGINE_URL,
            secret_key=MOCK_SECRET_KEY,
        )

    assert exc_info.value.response.status_code == 503
    # 자체 retry 없음 — 요청 1회만 발생함
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
